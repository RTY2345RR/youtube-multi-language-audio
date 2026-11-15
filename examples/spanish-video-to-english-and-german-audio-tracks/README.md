# Example: a Spanish video → English and German YouTube audio tracks (two cloned voices)

Real output of `youtube-multi-language-audio` (no edits). The 11.3 s source video is a Spanish two-person clip. Translated subtitles label each line with its speaker, and each speaker gets a cloned voice: Marco (male) and Lucia (female).

```bash
ytmla dub input/interview.mp4 input/interview.en.srt input/interview.de.srt -o output --format mp3 --multitrack \
  --voice Marco=http://festvox.org/cmu_arctic/cmu_arctic/cmu_us_bdl_arctic/wav/arctic_a0003.wav \
  --voice-text "Marco=For the twentieth time that evening the two men shook hands." \
  --voice Lucia=http://festvox.org/cmu_arctic/cmu_arctic/cmu_us_slt_arctic/wav/arctic_a0003.wav \
  --voice-text "Lucia=For the twentieth time that evening the two men shook hands."
```

| File | What it is |
| --- | --- |
| [`input/interview.mp4`](input/interview.mp4) | original video (Spanish) |
| [`input/interview.en.srt`](input/interview.en.srt), [`input/interview.de.srt`](input/interview.de.srt) | translated subtitles with `[Marco]` / `[Lucia]` labels |
| [`output/en/interview.en.mp3`](output/en/interview.en.mp3) | **English dub track**, 11.3 s like the video |
| [`output/de/interview.de.mp3`](output/de/interview.de.mp3) | **German dub track** |
| [`output/en/interview.en.srt`](output/en/interview.en.srt) | English captions re-timed to the dub (`.speakers.srt` keeps labels) |
| [`output/en/interview.en.preview.mp4`](output/en/interview.en.preview.mp4) | video + English dub, to check before publishing |
| [`output/interview.multi-language.mkv`](output/interview.multi-language.mkv) | one file with original, English and German audio, language-tagged |
| [`output/en/timing.json`](output/en/timing.json) | per line: slot, speed, stretch, drift |
| [`output/UPLOAD-CHECKLIST.md`](output/UPLOAD-CHECKLIST.md) | YouTube Studio steps |

Speech recognition of the English track returns: *"Hi everyone! Today we're dubbing this video into another language in one step, and everyone keeps their own voice with synced subtitles."* Median pitch: Marco's lines ≈114 Hz, Lucia's ≈174 Hz (male / female clone).

Reference voices: CMU ARCTIC `bdl` and `slt` (see [CREDITS.md](../../CREDITS.md)).
