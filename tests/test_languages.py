from pathlib import Path

import pytest

from youtube_multi_language_audio import languages


@pytest.mark.parametrize("name,code", [
    ("video.es.srt", "es"), ("es.srt", "es"), ("talk_pt-BR.srt", "pt-BR"), ("clip-de.srt", "de"),
    ("my.video.zh-Hans.srt", "zh-Hans"), ("ep1.es-419.vtt", "es-419"), ("talk.PT_br.srt", "pt-BR"),
])
def test_language_from_filename(name, code):
    assert languages.from_filename(Path(name)) == code


def test_unknown_language_name():
    assert languages.from_filename(Path("final-cut.srt")) is None
    with pytest.raises(languages.LanguageError):
        languages.parse_subtitle_arg("final-cut.srt")


def test_explicit_language_arg():
    assert languages.parse_subtitle_arg("fr=subs/french.srt") == ("fr", Path("subs/french.srt"))


def test_names_and_iso():
    assert languages.name("pt-BR") == "Portuguese (Brazil)"
    assert languages.name("de-AT") == "German"
    assert languages.iso639_2("de") == "deu"
    assert languages.iso639_2("xx") == "und"
