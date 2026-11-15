"""Subtitles in, YouTube multi-language audio tracks out.

For every language: read the translated subtitles, give each cue a cloned voice
(Miragea voice engine), fit each spoken line into its cue's time slot, lay the
lines on a silent timeline exactly as long as the video, and export an
upload-ready track plus re-timed captions. A checklist explains the YouTube
Studio upload, because YouTube's public API cannot add audio tracks.

Every voice job is saved (task id + idempotency key) in the output folder, so
running the same command again resumes and never submits a duplicate job.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

import httpx

from . import languages, media
from .client import MirageaClient, MirageaError, new_idempotency_key
from .fit import chars_per_second, slot_seconds, spoken_length, tempo_for
from .subtitles import Segment, detect_speakers, merge_cues, parse_any, renumber, to_srt

SAMPLE_HOST = "https://uguu.se/upload"  # temporary public host; files expire after a few hours
UPLOAD_MAX_AGE = 2 * 3600
STATE_FILE = ".ytmla-state.json"
DEFAULT_SPEAKER = "*"

Log = Callable[[str], None]


class TrackError(Exception):
    pass


def _quiet(_: str) -> None:
    return None


# ===================================================================== inputs
@dataclass
class Voice:
    """Where a speaker's voice comes from: a clip to clone (file or URL) or a saved Miragea voice."""

    speaker: str = DEFAULT_SPEAKER
    clip: Optional[str] = None  # local path or http(s) URL
    actor_id: Optional[str] = None
    reference_text: Optional[str] = None
    # resolved
    url: Optional[str] = None

    def payload(self) -> dict[str, Any]:
        if self.actor_id:
            return {"actor_id": self.actor_id}
        if not self.url:
            raise TrackError(f"Voice for {self.speaker!r} has no public clip URL yet.")
        out: dict[str, Any] = {"reference_audio_url": self.url}
        if self.reference_text:
            out["reference_text"] = self.reference_text
        return out


@dataclass
class LanguageInput:
    language: str
    subtitles: Path
    lines: list[Segment] = field(default_factory=list)
    speakers: list[str] = field(default_factory=list)


def parse_speaker_value(value: str) -> tuple[str, str]:
    """'Anna=clip.wav' -> ('Anna', 'clip.wav'); 'clip.wav' -> ('*', 'clip.wav'). URLs keep their '='."""
    if "=" in value and not value.lower().startswith(("http://", "https://")):
        name, _, rest = value.partition("=")
        if name and not Path(value).exists() and "/" not in name and "\\" not in name:
            return name.strip(), rest.strip()
    return DEFAULT_SPEAKER, value.strip()


def load_language(language: str, path: Path, *, merge: bool = True) -> LanguageInput:
    if not path.exists():
        raise TrackError(f"Subtitle file not found: {path}")
    text = path.read_text(encoding="utf-8-sig")
    cues = parse_any(text, path.suffix)
    cues = [c for c in cues if re.sub(r"\[[^\]]*\]|♪", "", c.text).strip(" .-")]
    if not cues:
        raise TrackError(f"{path.name} has no spoken lines (only empty cues or sound tags like [Music]).")
    info = detect_speakers(cues, default=DEFAULT_SPEAKER)
    if merge:
        cues = merge_cues(cues)
    renumber(cues)
    return LanguageInput(language, path, cues, info["speakers"])


def voice_for(voices: dict[str, Voice], speaker: Optional[str]) -> Voice:
    if speaker and speaker in voices:
        return voices[speaker]
    if DEFAULT_SPEAKER in voices:
        return voices[DEFAULT_SPEAKER]
    if (not speaker or speaker == DEFAULT_SPEAKER) and voices:
        return next(iter(voices.values()))  # unlabelled lines use the first voice given
    raise TrackError(
        f"No voice for speaker {speaker!r}. Pass --voice {speaker}=clip.wav, or --voice clip.wav for everyone."
    )


# ====================================================================== state
class State:
    """Resumable job ledger stored next to the outputs."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.data: dict[str, Any] = {"version": 1, "voices": {}, "jobs": {}}
        if path.exists():
            try:
                self.data = json.loads(path.read_text("utf-8"))
            except ValueError:
                pass
        self.data.setdefault("voices", {})
        self.data.setdefault("jobs", {})

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=2, ensure_ascii=False), "utf-8")
        tmp.replace(self.path)


def file_sha(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()[:16]


def fingerprint(payload: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:20]


# ===================================================================== voices
def cut_clip(video: Path, start: float, end: float, dest: Path) -> Path:
    if end - start < 3:
        raise TrackError("The voice clip should be at least 3 seconds (5-15 s of clean speech works best).")
    dest.parent.mkdir(parents=True, exist_ok=True)
    media.run_ffmpeg(["-ss", f"{start:.3f}", "-to", f"{end:.3f}", "-i", str(video), "-vn", "-ac", "1", "-ar", "24000", str(dest)])
    return dest


def parse_range(value: str) -> tuple[float, float]:
    """'12.5-22', '0:12-0:21.5', '1:02:03-1:02:12' -> seconds."""
    m = re.fullmatch(r"\s*([\d:.]+)\s*-\s*([\d:.]+)\s*", value or "")
    if not m:
        raise TrackError(f"Time range {value!r} must look like 12.5-22 or 0:12-0:21.")

    def secs(t: str) -> float:
        total = 0.0
        for part in t.split(":"):
            total = total * 60 + float(part)
        return total

    start, end = secs(m.group(1)), secs(m.group(2))
    if end <= start:
        raise TrackError(f"Time range {value!r} ends before it starts.")
    return start, end


async def upload_clip(path: Path, http: Optional[httpx.AsyncClient] = None) -> str:
    """Put a local clip on a temporary public host so the voice engine can fetch it."""
    own = http is None
    http = http or httpx.AsyncClient(timeout=120, headers={"User-Agent": "youtube-multi-language-audio"})
    try:
        with path.open("rb") as fh:
            resp = await http.post(SAMPLE_HOST, files={"files[]": (path.name, fh, "audio/wav")})
        resp.raise_for_status()
        data = resp.json()
        url = (data.get("files") or [{}])[0].get("url")
        if not data.get("success") or not url:
            raise ValueError("unexpected response")
        return str(url)
    except (httpx.HTTPError, ValueError) as exc:
        raise TrackError(
            f"Could not upload the voice clip {path.name} ({type(exc).__name__}). Host it yourself at any "
            "public https URL and pass that URL to --voice."
        ) from None
    finally:
        if own:
            await http.aclose()


async def transcribe_clip(client: MirageaClient, path: Path, state: State, sha: str) -> str:
    """Reference text for a clip (what is said in it), via Miragea speech recognition."""
    job = state.data["voices"].setdefault(sha, {})
    if job.get("reference_text"):
        return job["reference_text"]
    if not job.get("asr_task"):
        caps = await client.capabilities()
        rate = (caps.get("subtitles") or {}).get("points_per_second") or 3
        key = job.get("asr_key") or new_idempotency_key()
        job["asr_key"] = key
        state.save()
        task = await client.create_recognition(path, rate, key)
        job["asr_task"] = task["task_id"]
        state.save()
    task = await client.wait_for_task(job["asr_task"])
    if task.get("status") != "succeeded":
        job.pop("asr_task", None)
        state.save()
        raise TrackError(f"Could not transcribe the voice clip {path.name}. Pass --voice-text with what is said in it.")
    result = task.get("result") or {}
    text = str(result.get("text") or result.get("txt") or "").strip()
    if not text:
        text = " ".join(str(s.get("text", "")) for s in result.get("segments") or []).strip()
    if not text:
        raise TrackError(f"No speech found in the voice clip {path.name}.")
    job["reference_text"] = text
    state.save()
    return text


async def resolve_voices(
    voices: dict[str, Voice],
    client: MirageaClient,
    state: State,
    *,
    http: Optional[httpx.AsyncClient] = None,
    log: Log = _quiet,
) -> None:
    """Turn local clips into public URLs (cached) and fill missing reference text."""
    for voice in voices.values():
        if voice.actor_id:
            continue
        if not voice.clip:
            raise TrackError(f"Voice {voice.speaker!r} needs a clip or an actor id.")
        if voice.clip.lower().startswith(("http://", "https://")):
            voice.url = voice.clip
            if not voice.reference_text:
                log(f"note: no --voice-text for {voice.speaker}; cloning quality is better with it")
            continue
        path = Path(voice.clip).expanduser()
        if not path.exists():
            raise TrackError(f"Voice clip not found: {path}")
        sha = file_sha(path)
        entry = state.data["voices"].setdefault(sha, {})
        if not voice.reference_text:
            log(f"transcribing voice clip {path.name}")
            voice.reference_text = await transcribe_clip(client, path, state, sha)
        if entry.get("url") and time.time() - entry.get("uploaded_at", 0) < UPLOAD_MAX_AGE:
            voice.url = entry["url"]
        else:
            log(f"uploading voice clip {path.name} to a temporary host for cloning")
            voice.url = await upload_clip(path, http)
            entry.update(url=voice.url, uploaded_at=time.time())
            state.save()


# ================================================================ synthesis
class Synth:
    """Submits, resumes and downloads voice jobs. Same payload = same job (deduplicated)."""

    def __init__(self, client: MirageaClient, state: State, clips_dir: Path, concurrency: int = 4, log: Log = _quiet):
        self.client = client
        self.state = state
        self.dir = clips_dir
        self.sem = asyncio.Semaphore(max(1, concurrency))
        self.lock = asyncio.Lock()
        self.log = log
        self.submitted = 0

    async def clip(self, payload: dict[str, Any]) -> Path:
        key = fingerprint(payload)
        jobs = self.state.data["jobs"]
        dest = self.dir / f"{key}.wav"
        job = jobs.get(key) or {}
        if job.get("file") and dest.exists():
            return dest
        async with self.sem:
            if not job.get("idempotency_key"):
                job = {"idempotency_key": new_idempotency_key()}
            if not job.get("task_id"):
                async with self.lock:
                    jobs[key] = job
                    self.state.save()
                task = await self.client.create_audio(payload, job["idempotency_key"])
                self.submitted += 1
                job["task_id"] = task["task_id"]
                async with self.lock:
                    self.state.save()
            task = await self.client.wait_for_task(job["task_id"])
            if task.get("status") != "succeeded":
                err = task.get("error") or {}
                async with self.lock:
                    jobs.pop(key, None)
                    self.state.save()
                raise MirageaError(
                    str(err.get("code") or "task_failed"),
                    f"{err.get('message') or 'voice generation failed'} (line: {payload.get('text', '')[:60]!r})",
                    retryable=bool(err.get("retryable")),
                    hint="Run the command again to retry this line. If it keeps failing, check the voice clip URL.",
                )
            url = (task.get("result") or {}).get("audio_url")
            if not url:
                raise MirageaError("invalid_result", "The voice task returned no audio_url.")
            self.dir.mkdir(parents=True, exist_ok=True)
            part = self.dir / f"{key}.part.wav"
            part.unlink(missing_ok=True)
            saved = await self.client.download(url, part)
            saved.replace(dest)
            job["file"] = dest.name
            async with self.lock:
                self.state.save()
            return dest


def line_payload(voice: Voice, text: str, model: Optional[str], speed: Optional[float] = None) -> dict[str, Any]:
    payload: dict[str, Any] = {"text": text, "format": "wav", **voice.payload()}
    if model:
        payload["model"] = model
    if speed and abs(speed - 1.0) > 1e-3:
        payload["speed"] = round(speed, 2)
    return payload


# ================================================================== fitting
def speech_seconds(path: Path) -> float:
    return media.pcm_seconds(media.decode_pcm(path))


async def voice_language(
    lang: LanguageInput,
    voices: dict[str, Voice],
    synth: Synth,
    *,
    duration: Optional[float],
    model: Optional[str],
    max_speedup: float,
    two_pass: str,
    log: Log = _quiet,
) -> list[dict[str, Any]]:
    """Voice every line; re-voice lines that do not fit at a faster speaking speed (two-pass)."""
    lines = lang.lines
    payloads = [line_payload(voice_for(voices, s.speaker), s.text, model) for s in lines]
    clips = await asyncio.gather(*(synth.clip(p) for p in payloads))
    log(f"[{lang.language}] voiced {len(clips)} lines")
    rows: list[dict[str, Any]] = []
    for i, (seg, clip) in enumerate(zip(lines, clips)):
        slot = slot_seconds(lines, i, duration)
        seconds = await asyncio.to_thread(speech_seconds, clip)
        rows.append({"segment": seg, "clip": clip, "slot": slot, "seconds": seconds, "speed": 1.0})
    if two_pass != "off":
        threshold = max_speedup if two_pass == "overflow" else 1.05
        redo = [r for r in rows if r["seconds"] / max(r["slot"], 0.1) > threshold]
        if redo:
            log(f"[{lang.language}] re-voicing {len(redo)} long lines at a faster speaking speed")

            async def again(row: dict[str, Any]) -> None:
                seg = row["segment"]
                speed = min(2.0, row["seconds"] / max(row["slot"], 0.1) * 1.03)
                clip = await synth.clip(line_payload(voice_for(voices, seg.speaker), seg.text, model, speed))
                seconds = await asyncio.to_thread(speech_seconds, clip)
                if seconds < row["seconds"]:
                    row.update(clip=clip, seconds=seconds, speed=round(speed, 2))

            await asyncio.gather(*(again(r) for r in redo))
    return rows


def assemble(
    rows: list[dict[str, Any]],
    wav: Path,
    *,
    duration: Optional[float],
    max_speedup: float,
) -> tuple[list[Segment], list[dict[str, Any]]]:
    """Lay the clips on one timeline. Speeds up (pitch kept) only what would overlap the next line."""
    writer = media.WavWriter(wav)
    timed: list[Segment] = []
    report: list[dict[str, Any]] = []
    cursor = 0.0
    for row in rows:
        seg: Segment = row["segment"]
        start = max(seg.start, cursor)
        available = max(0.3, row["slot"] - (start - seg.start))
        if duration is not None and start >= duration - 0.05:
            break
        tempo, overflow = tempo_for(row["seconds"], available, max_speedup)
        pcm = media.decode_pcm(row["clip"], tempo=tempo)
        if duration is not None:
            room = int((duration - start) * media.SAMPLE_RATE) * 2
            pcm = pcm[:room]
        writer.pad_to(start)
        writer.pcm(pcm)
        placed = media.pcm_seconds(pcm)
        cursor = writer.seconds + 0.08
        timed.append(Segment(seg.id, start, start + placed, seg.text, seg.speaker))
        report.append({
            "id": seg.id, "speaker": None if seg.speaker == DEFAULT_SPEAKER else seg.speaker,
            "cue_start": round(seg.start, 2), "start": round(start, 2), "seconds": round(placed, 2),
            "slot": round(row["slot"], 2), "voice_speed": row["speed"], "tempo": tempo,
            "overflow": overflow, "drift": round(start - seg.start, 2), "text": seg.text,
        })
    if duration is not None:
        writer.pad_to(duration)
    writer.close()
    return timed, report


# =================================================================== export
CHECKLIST_HEAD = """\
# Upload checklist: YouTube multi-language audio

Made by youtube-multi-language-audio for **{video}** ({duration}).

YouTube's public Data API cannot upload extra audio tracks, so this last step
happens in YouTube Studio on a computer. It takes about a minute per language.

## Before you start
- [ ] Your channel has **Advanced features** (needed for multi-language audio).
- [ ] The video's own language is set (Studio > Content > video > Details > Language).
- [ ] If YouTube already made an **automatic dub** in a language below, delete that auto dub first
      (Studio > Languages > video > Audio > Delete). You cannot upload your own track until it is gone.
- [ ] Listen to each track once (or open the preview MP4) before publishing.

## Upload each language
Studio > **Languages** > choose the video > **Add language**, then for each row:
"""

CHECKLIST_LANG = """
### {name} (`{code}`)
- [ ] **Dub** > Add > Select file > `{track}` > **Publish** (audio only, {track_len}, video {video_len})
- [ ] **Subtitles** > Add > Upload file > *With timing* > `{srt}`
- [ ] **Title & description** > Add > paste a translated title and description (helps search in {name})
"""

CHECKLIST_TAIL = """
## After publishing
- Viewers get the track that matches their language automatically; anyone can switch under
  **Settings (gear) > Audio track**.
- Track results in YouTube Analytics: Audience > views by **audio track language**.
- To replace a track later: Studio > Languages > video > Audio > Delete, then upload the new file.
"""


def fmt_len(seconds: Optional[float]) -> str:
    if seconds is None:
        return "?"
    m, s = divmod(int(round(seconds)), 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def safe_stem(path: Path) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "-", path.stem).strip("-")[:60] or "video"


def write_checklist(out: Path, video: Path, duration: Optional[float], rows: list[dict[str, Any]]) -> Path:
    parts = [CHECKLIST_HEAD.format(video=video.name, duration=fmt_len(duration))]
    for r in rows:
        parts.append(CHECKLIST_LANG.format(
            name=r["language_name"], code=r["language"],
            track=f"{r['language']}/{Path(r['track']).name}", srt=f"{r['language']}/{Path(r['captions']).name}",
            track_len=fmt_len(r["track_seconds"]), video_len=fmt_len(duration),
        ))
    parts.append(CHECKLIST_TAIL)
    path = out / "UPLOAD-CHECKLIST.md"
    path.write_text("".join(parts), "utf-8")
    return path


def has_audio_stream(path: Path) -> bool:
    import subprocess

    proc = subprocess.run([media.ffmpeg_exe(), "-hide_banner", "-nostdin", "-i", str(path)], capture_output=True)
    return "Audio:" in proc.stderr.decode("utf-8", "replace")


def mux_multitrack(video: Path, tracks: list[tuple[str, Path, Path]], dest: Path) -> Path:
    """One MKV with the original audio plus every dub (language-tagged) and subtitles, for review."""
    original = has_audio_stream(video)
    args = ["-i", str(video)]
    for _, track, srt in tracks:
        args += ["-i", str(track), "-i", str(srt)]
    args += ["-map", "0:v:0"]
    if original:
        args += ["-map", "0:a:0"]
    for n in range(len(tracks)):
        args += ["-map", f"{1 + 2 * n}:a:0"]
    for n in range(len(tracks)):
        args += ["-map", f"{2 + 2 * n}:0"]
    args += ["-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-c:s", "srt", "-disposition:a", "0", "-disposition:s", "0",
             "-disposition:a:0", "default"]
    first = 0
    if original:
        args += ["-metadata:s:a:0", "title=Original"]
        first = 1
    for n, (lang, _, _) in enumerate(tracks):
        code = languages.iso639_2(lang)
        args += [f"-metadata:s:a:{n + first}", f"language={code}",
                 f"-metadata:s:a:{n + first}", f"title={languages.name(lang)} (dub)",
                 f"-metadata:s:s:{n}", f"language={code}"]
    media.run_ffmpeg([*args, str(dest)])
    return dest


@dataclass
class Options:
    out_dir: Path
    audio_format: str = "m4a"
    max_speedup: float = 1.3
    two_pass: str = "overflow"
    model: Optional[str] = None
    merge: bool = True
    background: Optional[Path] = None
    background_volume: float = 0.12
    preview: bool = True
    multitrack: bool = False
    concurrency: int = 4


async def build_tracks(
    video: Path,
    inputs: list[LanguageInput],
    voices: dict[str, Voice],
    client: MirageaClient,
    opts: Options,
    *,
    http: Optional[httpx.AsyncClient] = None,
    log: Log = _quiet,
) -> dict[str, Any]:
    if not video.exists():
        raise TrackError(f"Video not found: {video}")
    if opts.audio_format not in ("m4a", "wav", "mp3", "flac"):
        raise TrackError("--format must be m4a, wav, mp3 or flac.")
    for lang in inputs:  # fail before any voice job is submitted
        for seg in lang.lines:
            voice_for(voices, seg.speaker)
    out = opts.out_dir
    out.mkdir(parents=True, exist_ok=True)
    duration = await asyncio.to_thread(media.probe_duration, video)
    state = State(out / STATE_FILE)
    await resolve_voices(voices, client, state, http=http, log=log)
    synth = Synth(client, state, out / ".clips", opts.concurrency, log)
    stem = safe_stem(video)
    has_video = await asyncio.to_thread(media.has_video_stream, video)

    async def one(lang: LanguageInput) -> dict[str, Any]:
        rows = await voice_language(lang, voices, synth, duration=duration, model=opts.model,
                                    max_speedup=opts.max_speedup, two_pass=opts.two_pass, log=log)
        folder = out / lang.language
        folder.mkdir(parents=True, exist_ok=True)
        wav = folder / f".voice-{lang.language}.wav"
        timed, report = await asyncio.to_thread(assemble, rows, wav, duration=duration, max_speedup=opts.max_speedup)
        track = await asyncio.to_thread(
            media.encode_track, wav, folder / f"{stem}.{lang.language}.{opts.audio_format}",
            background=opts.background, background_volume=opts.background_volume,
        ) if opts.audio_format != "flac" else await asyncio.to_thread(_encode_flac, wav, folder / f"{stem}.{lang.language}.flac")
        wav.unlink(missing_ok=True)
        srt = folder / f"{stem}.{lang.language}.srt"
        srt.write_text(to_srt(timed), "utf-8")
        named = any(s.speaker and s.speaker != DEFAULT_SPEAKER for s in timed)
        if named:
            (folder / f"{stem}.{lang.language}.speakers.srt").write_text(to_srt(timed, with_speaker=True), "utf-8")
        (folder / "timing.json").write_text(json.dumps(report, indent=2, ensure_ascii=False), "utf-8")
        preview = None
        if opts.preview and has_video:
            preview = await asyncio.to_thread(media.mux_preview, video, track, folder / f"{stem}.{lang.language}.preview.mp4", srt)
        track_seconds = await asyncio.to_thread(media.probe_duration, track)
        overflowing = [r for r in report if r["overflow"] > 0.05]
        log(f"[{lang.language}] track ready: {track}")
        return {
            "language": lang.language,
            "language_name": languages.name(lang.language),
            "subtitles": str(lang.subtitles),
            "track": str(track),
            "captions": str(srt),
            "preview": str(preview) if preview else None,
            "track_seconds": round(track_seconds, 2),
            "length_matches_video": abs(track_seconds - duration) < 0.5,
            "lines": len(report),
            "speakers": [s for s in lang.speakers if s != DEFAULT_SPEAKER],
            "revoiced_faster": sum(1 for r in report if r["voice_speed"] != 1.0),
            "stretched": sum(1 for r in report if r["tempo"] > 1.0),
            "max_tempo": max([r["tempo"] for r in report] or [1.0]),
            "max_drift": max([r["drift"] for r in report] or [0.0]),
            "overflowing_lines": [{"id": r["id"], "overflow": r["overflow"], "text": r["text"]} for r in overflowing],
        }

    results = await asyncio.gather(*(one(lang) for lang in inputs), return_exceptions=True)
    errors = [r for r in results if isinstance(r, BaseException)]
    if errors:
        raise errors[0]
    rows: list[dict[str, Any]] = list(results)  # type: ignore[arg-type]
    multitrack = None
    if opts.multitrack and has_video:
        multitrack = await asyncio.to_thread(
            mux_multitrack, video, [(r["language"], Path(r["track"]), Path(r["captions"])) for r in rows],
            out / f"{stem}.multi-language.mkv",
        )
    checklist = write_checklist(out, video, duration, rows)
    manifest = {
        "tool": "youtube-multi-language-audio",
        "video": str(video),
        "video_seconds": round(duration, 2),
        "languages": rows,
        "multitrack_preview": str(multitrack) if multitrack else None,
        "checklist": str(checklist),
        "studio_url": "https://studio.youtube.com",
        "new_voice_jobs": synth.submitted,
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False), "utf-8")
    return manifest


def _encode_flac(wav: Path, dest: Path) -> Path:
    media.run_ffmpeg(["-i", str(wav), "-ac", "2", "-ar", str(media.SAMPLE_RATE), "-c:a", "flac", str(dest)])
    return dest


# ==================================================================== check
def check_language(lang: LanguageInput, duration: Optional[float], max_speedup: float) -> dict[str, Any]:
    """Offline fit estimate: which lines are likely too long for their time slot (no API calls)."""
    cps = chars_per_second(lang.language)
    rows = []
    for i, seg in enumerate(lang.lines):
        slot = slot_seconds(lang.lines, i, duration)
        est = spoken_length(seg.text) / cps
        need = est / slot if slot else 9.9
        rows.append({"id": seg.id, "start": round(seg.start, 2), "slot": round(slot, 2), "estimated_seconds": round(est, 2),
                     "needed_speed": round(need, 2), "status": "ok" if need <= 1.0 else ("tight" if need <= max_speedup else "too_long"),
                     "speaker": None if seg.speaker == DEFAULT_SPEAKER else seg.speaker, "text": seg.text})
    return {
        "language": lang.language, "language_name": languages.name(lang.language), "lines": len(rows),
        "speakers": [s for s in lang.speakers if s != DEFAULT_SPEAKER],
        "tight": sum(1 for r in rows if r["status"] == "tight"),
        "too_long": [r for r in rows if r["status"] == "too_long"],
        "rows": rows,
    }


__all__ = [
    "LanguageInput", "Options", "State", "TrackError", "Voice", "assemble", "build_tracks", "check_language",
    "cut_clip", "load_language", "parse_range", "parse_speaker_value", "write_checklist",
]
