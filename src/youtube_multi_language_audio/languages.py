"""Language codes: read them from subtitle file names and name them like YouTube Studio does."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

# Languages YouTube Studio offers for dubbed audio tracks (BCP-47 code -> English name).
LANGUAGE_NAMES: dict[str, str] = {
    "af": "Afrikaans", "am": "Amharic", "ar": "Arabic", "as": "Assamese", "az": "Azerbaijani",
    "be": "Belarusian", "bg": "Bulgarian", "bn": "Bengali", "bs": "Bosnian", "ca": "Catalan",
    "cs": "Czech", "cy": "Welsh", "da": "Danish", "de": "German", "el": "Greek", "en": "English",
    "en-GB": "English (United Kingdom)", "en-US": "English (United States)", "es": "Spanish",
    "es-419": "Spanish (Latin America)", "es-ES": "Spanish (Spain)", "es-MX": "Spanish (Mexico)",
    "et": "Estonian", "eu": "Basque", "fa": "Persian", "fi": "Finnish", "fil": "Filipino",
    "fr": "French", "fr-CA": "French (Canada)", "ga": "Irish", "gl": "Galician", "gu": "Gujarati",
    "he": "Hebrew", "hi": "Hindi", "hr": "Croatian", "hu": "Hungarian", "hy": "Armenian",
    "id": "Indonesian", "is": "Icelandic", "it": "Italian", "ja": "Japanese", "ka": "Georgian",
    "kk": "Kazakh", "km": "Khmer", "kn": "Kannada", "ko": "Korean", "ky": "Kyrgyz", "lo": "Lao",
    "lt": "Lithuanian", "lv": "Latvian", "mk": "Macedonian", "ml": "Malayalam", "mn": "Mongolian",
    "mr": "Marathi", "ms": "Malay", "my": "Burmese", "ne": "Nepali", "nl": "Dutch", "no": "Norwegian",
    "or": "Odia", "pa": "Punjabi", "pl": "Polish", "pt": "Portuguese", "pt-BR": "Portuguese (Brazil)",
    "pt-PT": "Portuguese (Portugal)", "ro": "Romanian", "ru": "Russian", "si": "Sinhala",
    "sk": "Slovak", "sl": "Slovenian", "sq": "Albanian", "sr": "Serbian", "sv": "Swedish",
    "sw": "Swahili", "ta": "Tamil", "te": "Telugu", "th": "Thai", "tl": "Filipino", "tr": "Turkish",
    "uk": "Ukrainian", "ur": "Urdu", "uz": "Uzbek", "vi": "Vietnamese", "zh": "Chinese",
    "zh-CN": "Chinese (China)", "zh-Hans": "Chinese (Simplified)", "zh-Hant": "Chinese (Traditional)",
    "zh-HK": "Chinese (Hong Kong)", "zh-TW": "Chinese (Taiwan)", "zu": "Zulu",
}

# ISO 639-2 codes for the language tag inside MKV/MP4 audio streams.
ISO639_2: dict[str, str] = {
    "af": "afr", "am": "amh", "ar": "ara", "az": "aze", "be": "bel", "bg": "bul", "bn": "ben", "bs": "bos",
    "ca": "cat", "cs": "ces", "cy": "cym", "da": "dan", "de": "deu", "el": "ell", "en": "eng", "es": "spa",
    "et": "est", "eu": "eus", "fa": "fas", "fi": "fin", "fil": "fil", "fr": "fra", "ga": "gle", "gl": "glg",
    "gu": "guj", "he": "heb", "hi": "hin", "hr": "hrv", "hu": "hun", "hy": "hye", "id": "ind", "is": "isl",
    "it": "ita", "ja": "jpn", "ka": "kat", "kk": "kaz", "km": "khm", "kn": "kan", "ko": "kor", "ky": "kir",
    "lo": "lao", "lt": "lit", "lv": "lav", "mk": "mkd", "ml": "mal", "mn": "mon", "mr": "mar", "ms": "msa",
    "my": "mya", "ne": "nep", "nl": "nld", "no": "nor", "or": "ori", "pa": "pan", "pl": "pol", "pt": "por",
    "ro": "ron", "ru": "rus", "si": "sin", "sk": "slk", "sl": "slv", "sq": "sqi", "sr": "srp", "sv": "swe",
    "sw": "swa", "ta": "tam", "te": "tel", "th": "tha", "tl": "tgl", "tr": "tur", "uk": "ukr", "ur": "urd",
    "uz": "uzb", "vi": "vie", "zh": "zho", "zu": "zul",
}

CODE_RE = re.compile(r"^[A-Za-z]{2,3}(?:-(?:[A-Za-z]{2}|[A-Za-z]{4}|\d{3}))?$")
_SUFFIX_RE = re.compile(r"(?:^|[._\s-])([A-Za-z]{2,3}(?:[-_](?:[A-Za-z]{2}|[A-Za-z]{4}|\d{3}))?)$")


class LanguageError(ValueError):
    pass


def normalize(code: str) -> str:
    """'PT_br' -> 'pt-BR', 'zh-hans' -> 'zh-Hans', 'ES-419' -> 'es-419'."""
    code = (code or "").strip().replace("_", "-")
    if not CODE_RE.match(code):
        raise LanguageError(f"{code!r} is not a language code like es, de, pt-BR or zh-Hans.")
    parts = code.split("-")
    out = [parts[0].lower()]
    for part in parts[1:]:
        if len(part) == 2:
            out.append(part.upper())
        elif len(part) == 4:
            out.append(part.title())
        else:
            out.append(part)
    return "-".join(out)


def is_known(code: str) -> bool:
    try:
        code = normalize(code)
    except LanguageError:
        return False
    return code in LANGUAGE_NAMES or code.split("-")[0] in LANGUAGE_NAMES


def name(code: str) -> str:
    code = normalize(code)
    return LANGUAGE_NAMES.get(code) or LANGUAGE_NAMES.get(code.split("-")[0], code)


def iso639_2(code: str) -> str:
    return ISO639_2.get(normalize(code).split("-")[0], "und")


def from_filename(path: Path) -> Optional[str]:
    """Language code at the end of a file name: video.es.srt, es.srt, talk_pt-BR.srt, clip-de.srt."""
    stem = path.stem
    m = _SUFFIX_RE.search(stem)
    candidates = []
    if m:
        candidates.append(m.group(1))
    candidates.append(stem)
    for cand in candidates:
        if is_known(cand):
            return normalize(cand)
    return None


def parse_subtitle_arg(arg: str) -> tuple[str, Path]:
    """'es=subs/spanish.srt' or 'subs/video.es.srt' -> ('es', Path)."""
    if "=" in arg and not Path(arg).exists():
        code, _, file = arg.partition("=")
        lang = normalize(code)
        return lang, Path(file).expanduser()
    path = Path(arg).expanduser()
    lang = from_filename(path)
    if not lang:
        raise LanguageError(
            f"Cannot tell the language of {path.name}. Name it like video.es.srt, or pass es={path}."
        )
    return lang, path
