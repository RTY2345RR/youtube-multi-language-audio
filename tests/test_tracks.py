import json
from pathlib import Path

import pytest

from youtube_multi_language_audio import cli, media
from youtube_multi_language_audio.fit import tempo_for
from youtube_multi_language_audio.tracks import load_language, parse_range, parse_speaker_value

from .conftest import API_KEY

ANNA = "https://voices.test/anna.wav"
BEN = "https://voices.test/ben.wav"


def run(args):
    return cli.main([str(a) for a in args])


def test_parse_helpers():
    assert parse_range("12.5-22") == (12.5, 22.0)
    assert parse_range("1:02-1:10.5") == (62.0, 70.5)
    with pytest.raises(Exception):
        parse_range("10-5")
    assert parse_speaker_value("Anna=clip.wav") == ("Anna", "clip.wav")
    assert parse_speaker_value("https://x.test/a.wav?x=1") == ("*", "https://x.test/a.wav?x=1")
    assert parse_speaker_value("clip.wav") == ("*", "clip.wav")
    assert tempo_for(3.0, 2.0, 1.3) == (1.3, round(3.0 / 1.3 - 2.0, 3))


def test_speakers_and_merge(subs):
    lang = load_language("es", subs["es"])
    assert lang.speakers == ["Anna", "Ben"]
    assert [s.text for s in lang.lines] == ["Hola a todos.", "Hoy probamos las pistas de audio.", "Hasta pronto."]
    assert [s.speaker for s in lang.lines] == ["Anna", "Ben", "Anna"]


def test_check_is_offline(video, subs, capsys, monkeypatch):
    monkeypatch.delenv("MIRAGEA_API_KEY", raising=False)
    assert run(["check", video, subs["es"], subs["de"], "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert [r["language"] for r in report["languages"]] == ["es", "de"]
    assert report["languages"][0]["speakers"] == ["Anna", "Ben"]


def test_dub_two_languages_two_speakers(engine, video, subs, tmp_path, capsys):
    out = tmp_path / "out"
    code = run(["dub", video, subs["es"], subs["de"], "-o", out, "--voice", f"Anna={ANNA}", "--voice-text",
                "Anna=Hello from Anna", "--voice", f"Ben={BEN}", "--multitrack", "--json"])
    assert code == 0, capsys.readouterr().err
    manifest = json.loads(capsys.readouterr().out)
    langs = {r["language"]: r for r in manifest["languages"]}
    assert set(langs) == {"es", "de"}
    for r in langs.values():
        assert Path(r["track"]).exists() and r["track"].endswith(".m4a")
        assert r["length_matches_video"], r
        assert Path(r["captions"]).read_text("utf-8").startswith("1\n00:00:00,500")
        assert Path(r["preview"]).exists()
    # Anna gets her clone and reference text; Ben's lines use Ben's clip.
    by_text = {b["text"]: b for b in engine.created}
    assert by_text["Hola a todos."]["reference_audio_url"] == ANNA
    assert by_text["Hola a todos."]["reference_text"] == "Hello from Anna"
    assert by_text["Hoy probamos las pistas de audio."]["reference_audio_url"] == BEN
    assert "[Anna]" not in Path(langs["es"]["captions"]).read_text("utf-8")
    assert "[Anna]" in (out / "es" / "talk.es.speakers.srt").read_text("utf-8")
    checklist = (out / "UPLOAD-CHECKLIST.md").read_text("utf-8")
    assert "Spanish (`es`)" in checklist and "German (`de`)" in checklist and "automatic dub" in checklist
    assert Path(manifest["multitrack_preview"]).exists()
    assert API_KEY not in json.dumps(manifest)


def test_rerun_resumes_without_new_jobs(engine, video, subs, tmp_path, capsys):
    out = tmp_path / "out"
    args = ["dub", video, subs["de"], "-o", out, "--voice", ANNA, "--no-preview", "--json"]
    assert run(args) == 0
    first = len(engine.created)
    capsys.readouterr()
    assert run(args) == 0
    assert len(engine.created) == first
    assert json.loads(capsys.readouterr().out)["new_voice_jobs"] == 0


def test_same_line_same_voice_is_one_job(engine, video, tmp_path, capsys):
    srt = tmp_path / "rep.fr.srt"
    srt.write_text("1\n00:00:01,000 --> 00:00:02,500\nMerci.\n\n2\n00:00:05,000 --> 00:00:06,500\nMerci.\n", "utf-8")
    assert run(["dub", video, srt, "-o", tmp_path / "o", "--voice", ANNA, "--no-preview"]) == 0
    assert len(engine.created) == 1


def test_two_pass_revoices_long_line_faster(engine, video, tmp_path, capsys):
    srt = tmp_path / "long.en.srt"
    srt.write_text("1\n00:00:00,500 --> 00:00:02,500\n" + "This sentence is far too long for a two second slot." + "\n\n"
                   "2\n00:00:02,600 --> 00:00:04,000\nOk.\n", "utf-8")
    assert run(["dub", video, srt, "-o", tmp_path / "o", "--voice", ANNA, "--no-preview", "--json"]) == 0
    speeds = [b.get("speed") for b in engine.created]
    assert any(s and s > 1.3 for s in speeds)
    timing = json.loads((tmp_path / "o" / "en" / "timing.json").read_text("utf-8"))
    assert timing[0]["voice_speed"] > 1.3
    assert timing[1]["start"] >= 2.6


def test_two_pass_off_stretches_and_reports(engine, video, tmp_path, capsys):
    srt = tmp_path / "long.en.srt"
    srt.write_text("1\n00:00:00,500 --> 00:00:02,500\n" + "This sentence is far too long for a two second slot." + "\n\n"
                   "2\n00:00:02,600 --> 00:00:04,000\nOk.\n", "utf-8")
    assert run(["dub", video, srt, "-o", tmp_path / "o", "--voice", ANNA, "--no-preview", "--two-pass", "off", "--json"]) == 0
    manifest = json.loads(capsys.readouterr().out)
    lang = manifest["languages"][0]
    assert lang["max_tempo"] == 1.3
    assert lang["overflowing_lines"] and lang["overflowing_lines"][0]["id"] == 1
    assert all("speed" not in b for b in engine.created)


def test_clone_from_video_uploads_and_transcribes(engine, uploads, video, subs, tmp_path, capsys):
    out = tmp_path / "out"
    assert run(["dub", video, subs["de"], "-o", out, "--clone-from-video", "1-6", "--no-preview"]) == 0
    assert uploads == ["uguu.se"]
    assert engine.recognitions == 1
    body = engine.created[0]
    assert body["reference_audio_url"] == "https://d.host.test/1.wav"
    assert body["reference_text"] == "This is my reference clip."
    # second run: cached upload and transcript
    assert run(["dub", video, subs["de"], "-o", out, "--clone-from-video", "1-6", "--no-preview"]) == 0
    assert uploads == ["uguu.se"] and engine.recognitions == 1


def test_failed_line_keeps_finished_ones(engine, video, subs, tmp_path, capsys):
    engine.fail_text = "Audiospuren"
    out = tmp_path / "out"
    args = ["dub", video, subs["de"], "-o", out, "--voice", ANNA, "--no-preview"]
    assert run(args) == 3
    assert "synthesis_failed" in capsys.readouterr().err
    done = len(engine.created)
    engine.fail_text = None
    assert run(args) == 0
    assert len(engine.created) == done + 1  # only the failed line is submitted again


def test_formats_and_duration(engine, video, subs, tmp_path, capsys):
    for fmt in ("wav", "mp3", "flac"):
        out = tmp_path / fmt
        assert run(["dub", video, subs["de"], "-o", out, "--voice", ANNA, "--no-preview", "--format", fmt]) == 0
        track = out / "de" / f"talk.de.{fmt}"
        assert abs(media.probe_duration(track) - 10.0) < 0.2


def test_errors_are_friendly(engine, video, subs, tmp_path, capsys, monkeypatch):
    assert run(["dub", video, subs["es"], "-o", tmp_path / "o"]) == 2
    assert "--voice" in capsys.readouterr().err
    assert run(["dub", video, subs["es"], "-o", tmp_path / "o", "--voice", f"Anna={ANNA}"]) == 2
    assert "Ben" in capsys.readouterr().err
    monkeypatch.delenv("MIRAGEA_API_KEY")
    assert run(["dub", video, subs["de"], "-o", tmp_path / "o", "--voice", ANNA]) == 3
    assert "miragea.ai/workspace/api" in capsys.readouterr().err


def test_folder_of_subtitles(engine, video, subs, tmp_path, capsys):
    assert run(["check", video, subs["es"].parent, "--languages", "de", "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert [r["language"] for r in report["languages"]] == ["de"]
