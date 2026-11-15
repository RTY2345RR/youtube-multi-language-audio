"""Command line: `youtube-multi-language-audio dub|check|doctor` (short alias: `ytmla`)."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any, Optional

from . import __version__, languages, media
from .client import MirageaClient, MirageaError
from .config import API_KEY_PAGE, INSTALL_GUIDE, ConfigError, load_settings
from .tracks import (
    DEFAULT_SPEAKER,
    LanguageInput,
    Options,
    TrackError,
    Voice,
    build_tracks,
    check_language,
    cut_clip,
    load_language,
    parse_range,
    parse_speaker_value,
)

SUB_EXTS = {".srt", ".vtt", ".json3"}

# Test hooks: an httpx transport for the API, a sleep function, an http client for clip uploads.
_TRANSPORT = None
_SLEEP = None
_UPLOAD_HTTP = None


def _client(settings: Any) -> MirageaClient:
    kwargs: dict[str, Any] = {}
    if _TRANSPORT is not None:
        kwargs["transport"] = _TRANSPORT
    if _SLEEP is not None:
        kwargs["sleep"] = _SLEEP
    return MirageaClient(settings, **kwargs)

EPILOG = f"""\
examples:
  # one cloned voice (yours) for every language
  ytmla dub video.mp4 video.es.srt video.de.srt video.pt-BR.srt --voice my-voice.wav

  # clone the voice straight from 10-25 s of the original video
  ytmla dub video.mp4 subs/ --clone-from-video 10-25

  # two speakers, labelled [Anna] / [Ben] in the subtitles
  ytmla dub talk.mp4 talk.fr.srt --voice Anna=anna.wav --voice Ben=ben.wav

  # offline: which translated lines are too long for their time slot?
  ytmla check video.mp4 video.es.srt

Voices are rendered by the Miragea voice engine. Get a key at {API_KEY_PAGE}
and export MIRAGEA_API_KEY. Agent setup: Install {INSTALL_GUIDE}
"""


def _err(msg: str) -> None:
    print(f"error: {msg}", file=sys.stderr)


def _log(msg: str) -> None:
    print(f"  {msg}", file=sys.stderr, flush=True)


def collect_subtitles(args: list[str], only: Optional[list[str]] = None) -> list[tuple[str, Path]]:
    found: list[tuple[str, Path]] = []
    for arg in args:
        path = Path(arg).expanduser()
        if path.is_dir():
            for f in sorted(path.iterdir()):
                if f.suffix.lower() in SUB_EXTS and languages.from_filename(f):
                    found.append((languages.from_filename(f), f))  # type: ignore[arg-type]
            continue
        found.append(languages.parse_subtitle_arg(arg))
    if only:
        wanted = {languages.normalize(x) for x in only}
        found = [(lang, p) for lang, p in found if lang in wanted]
    seen: dict[str, Path] = {}
    for lang, p in found:
        if lang in seen and seen[lang] != p:
            raise TrackError(f"Two subtitle files for {lang}: {seen[lang].name} and {p.name}. Keep one.")
        seen[lang] = p
    if not seen:
        raise TrackError("No subtitle files found. Pass files like video.es.srt, es=spanish.srt, or a folder.")
    return list(seen.items())


def build_voices(args: argparse.Namespace, video: Path, out: Path) -> dict[str, Voice]:
    voices: dict[str, Voice] = {}
    for value in args.voice or []:
        name, clip = parse_speaker_value(value)
        voices[name] = Voice(name, clip=clip)
    for value in args.actor or []:
        name, actor = parse_speaker_value(value)
        voices[name] = Voice(name, actor_id=actor)
    for value in args.clone_from_video or []:
        name, span = parse_speaker_value(value)
        start, end = parse_range(span)
        slug = "everyone" if name == DEFAULT_SPEAKER else "".join(c if c.isalnum() else "-" for c in name)
        clip = cut_clip(video, start, end, out / ".voices" / f"{slug}-{int(start * 1000)}-{int(end * 1000)}.wav")
        voices[name] = Voice(name, clip=str(clip))
    for value in args.voice_text or []:
        name, text = parse_speaker_value(value)
        if name not in voices:
            raise TrackError(f"--voice-text for {name!r} but no --voice/--clone-from-video for it.")
        voices[name].reference_text = text
    if not voices:
        raise TrackError(
            "Give a voice to clone: --voice my-voice.wav (5-15 s of clean speech), --voice https://..., "
            "--clone-from-video 12-24 (cut from the video), or --actor <saved voice id>."
        )
    return voices


def load_inputs(args: argparse.Namespace) -> tuple[Path, list[LanguageInput]]:
    video = Path(args.video).expanduser()
    if not video.exists():
        raise TrackError(f"Video not found: {video}")
    if not media.is_media(video):
        raise TrackError(f"{video.name} is not a video or audio file.")
    only = [x for x in (args.languages or "").split(",") if x.strip()] or None
    subs = collect_subtitles(args.subtitles, only)
    return video, [load_language(lang, path, merge=not args.no_merge) for lang, path in subs]


def cmd_check(args: argparse.Namespace) -> int:
    video, inputs = load_inputs(args)
    duration = media.probe_duration(video)
    reports = [check_language(i, duration, args.max_speedup) for i in inputs]
    if args.json:
        print(json.dumps({"video_seconds": round(duration, 2), "languages": reports}, indent=2, ensure_ascii=False))
        return 0
    print(f"{video.name}: {duration:.1f} s")
    bad = 0
    for r in reports:
        spk = f", speakers: {', '.join(r['speakers'])}" if r["speakers"] else ""
        print(f"\n{r['language_name']} ({r['language']}): {r['lines']} lines{spk}")
        print(f"  {r['lines'] - r['tight'] - len(r['too_long'])} fit, {r['tight']} need a small speed-up, {len(r['too_long'])} too long")
        for row in r["too_long"]:
            bad += 1
            print(f"  #{row['id']} @ {row['start']}s needs {row['needed_speed']}x: {row['text'][:90]}")
    if bad:
        print(f"\nShorten the {bad} too-long lines (or allow more with --max-speedup) for the cleanest sync.")
    return 0


def cmd_dub(args: argparse.Namespace) -> int:
    video, inputs = load_inputs(args)
    out = Path(args.out).expanduser() if args.out else Path.cwd() / f"{video.stem}-audio-tracks"
    voices = build_voices(args, video, out)
    settings = load_settings()
    opts = Options(
        out_dir=out, audio_format=args.format, max_speedup=args.max_speedup, two_pass=args.two_pass,
        model=args.model, merge=not args.no_merge,
        background=Path(args.background).expanduser() if args.background else None,
        background_volume=args.background_volume, preview=not args.no_preview,
        multitrack=args.multitrack, concurrency=args.concurrency or settings.concurrency,
    )
    langs = ", ".join(i.language for i in inputs)
    print(f"Dubbing {video.name} into {langs} -> {out}", file=sys.stderr)

    async def run() -> dict[str, Any]:
        async with _client(settings) as client:
            return await build_tracks(video, inputs, voices, client, opts, http=_UPLOAD_HTTP, log=_log)

    manifest = asyncio.run(run())
    if args.json:
        print(json.dumps(manifest, indent=2, ensure_ascii=False))
        return 0
    print()
    for r in manifest["languages"]:
        ok = "length OK" if r["length_matches_video"] else "LENGTH DIFFERS"
        print(f"{r['language_name']:<24} {r['track']}  ({r['track_seconds']:.1f} s, {ok}, max speed-up {r['max_tempo']}x)")
        for line in r["overflowing_lines"][:5]:
            print(f"    line {line['id']} still runs {line['overflow']} s long: shorten it and re-run")
    if manifest.get("multitrack_preview"):
        print(f"Multi-language preview: {manifest['multitrack_preview']}")
    print(f"\nNext: follow {manifest['checklist']} to upload in YouTube Studio > Languages.")
    return 0


def cmd_doctor(_: argparse.Namespace) -> int:
    ok = True
    try:
        print(f"ffmpeg: {media.ffmpeg_exe()}")
    except media.MediaError as exc:
        ok = False
        print(f"ffmpeg: missing ({exc})")
    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"config: {exc}")
        return 1
    if not settings.has_key:
        print(f"MIRAGEA_API_KEY: not set (create one at {API_KEY_PAGE})")
        return 1
    print("MIRAGEA_API_KEY: set")

    async def ping() -> dict[str, Any]:
        async with _client(settings) as client:
            return await client.capabilities()

    try:
        caps = asyncio.run(ping())
        models = [m.get("id") for m in (caps.get("audio") or {}).get("models") or []]
        print(f"voice engine: reachable, models: {', '.join(models) or '?'}")
    except MirageaError as exc:
        ok = False
        print(f"voice engine: {exc}")
    return 0 if ok else 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="youtube-multi-language-audio",
        description="Turn translated subtitles into dubbed, time-synced audio tracks for YouTube multi-language audio "
        "(one track per language, in a cloned voice).",
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    def common(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("video", help="the original video (its length sets every track's length)")
        sp.add_argument("subtitles", nargs="+", help="translated subtitles: video.es.srt, es=spanish.srt, or a folder")
        sp.add_argument("--languages", help="only these codes, comma separated (es,de,pt-BR)")
        sp.add_argument("--max-speedup", type=float, default=1.3, help="max time-stretch per line (default 1.3)")
        sp.add_argument("--no-merge", action="store_true", help="voice each cue as is (default: join cue fragments into sentences)")
        sp.add_argument("--json", action="store_true", help="print a JSON report")

    d = sub.add_parser("dub", help="generate the dubbed audio tracks", formatter_class=argparse.RawDescriptionHelpFormatter, epilog=EPILOG)
    common(d)
    d.add_argument("--voice", action="append", metavar="[NAME=]CLIP", help="voice to clone: local clip or https URL; NAME= for one speaker")
    d.add_argument("--voice-text", action="append", metavar="[NAME=]TEXT", help="what is said in the clip (auto-transcribed if omitted)")
    d.add_argument("--clone-from-video", action="append", metavar="[NAME=]START-END", help="cut the voice clip from the video, e.g. 12-24")
    d.add_argument("--actor", action="append", metavar="[NAME=]ID", help="use a saved Miragea voice instead of cloning")
    d.add_argument("-o", "--out", help="output folder (default: ./<video>-audio-tracks)")
    d.add_argument("--format", default="m4a", choices=["m4a", "wav", "mp3", "flac"], help="track format (default m4a: AAC 48 kHz)")
    d.add_argument("--two-pass", default="overflow", choices=["overflow", "always", "off"],
                   help="re-voice lines that do not fit at a faster speaking speed (default: only lines that would still overflow)")
    d.add_argument("--model", help="voice model id (see `doctor`); default: the engine's default")
    d.add_argument("--background", help="music & effects stem to mix under the voices")
    d.add_argument("--background-volume", type=float, default=0.12)
    d.add_argument("--no-preview", action="store_true", help="skip the per-language preview MP4")
    d.add_argument("--multitrack", action="store_true", help="also write one MKV with every audio track, language-tagged")
    d.add_argument("--concurrency", type=int, help="parallel voice jobs (default 4)")
    d.set_defaults(func=cmd_dub)

    c = sub.add_parser("check", help="offline: estimate which translated lines are too long for their slot")
    common(c)
    c.set_defaults(func=cmd_check)

    doc = sub.add_parser("doctor", help="check ffmpeg, the API key and the voice engine")
    doc.set_defaults(func=cmd_doctor)
    return p


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (TrackError, languages.LanguageError, ConfigError, media.MediaError) as exc:
        _err(str(exc))
        return 2
    except MirageaError as exc:
        _err(str(exc))
        return 3
    except KeyboardInterrupt:
        _err("interrupted. Run the same command again to resume; finished lines are kept.")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
