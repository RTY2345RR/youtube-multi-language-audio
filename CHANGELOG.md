# Changelog

## 0.1.0 (2026-11-15)

First release.

- `dub`: translated SRT/VTT/json3 files + the original video → one dubbed audio track per language for YouTube multi-language audio (M4A/WAV/MP3/FLAC, exact video length), re-timed SRT, preview MP4, `timing.json`, `UPLOAD-CHECKLIST.md`, `manifest.json`, optional multi-language MKV.
- Voice cloning from a local clip, a URL or a time range of the video (`--clone-from-video`), with automatic reference-text transcription; saved voices with `--actor`.
- Multi-speaker dubbing from `[Name]` / `Name:` labels.
- Cue fitting: two-pass voicing at the needed speaking speed, capped pitch-preserving stretch, no overlaps, overflow report.
- `check`: offline estimate of lines that are too long for their slot.
- `doctor`: ffmpeg, API key and voice-engine check.
- Resumable jobs with idempotency keys: re-running never submits a duplicate job.
