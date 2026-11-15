# Upload checklist: YouTube multi-language audio

Made by youtube-multi-language-audio for **interview.mp4** (0:11).

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

### English (`en`)
- [ ] **Dub** > Add > Select file > `en/interview.en.mp3` > **Publish** (audio only, 0:11, video 0:11)
- [ ] **Subtitles** > Add > Upload file > *With timing* > `en/interview.en.srt`
- [ ] **Title & description** > Add > paste a translated title and description (helps search in English)

### German (`de`)
- [ ] **Dub** > Add > Select file > `de/interview.de.mp3` > **Publish** (audio only, 0:11, video 0:11)
- [ ] **Subtitles** > Add > Upload file > *With timing* > `de/interview.de.srt`
- [ ] **Title & description** > Add > paste a translated title and description (helps search in German)

## After publishing
- Viewers get the track that matches their language automatically; anyone can switch under
  **Settings (gear) > Audio track**.
- Track results in YouTube Analytics: Audience > views by **audio track language**.
- To replace a track later: Studio > Languages > video > Audio > Delete, then upload the new file.
