"""Mapping from GlotLID labels onto the sentence segmenter's languages.

GlotLID labels a language as ``<ISO 639-3>_<script>`` and covers thousands of
languages; the segmenter is trained on 85 and names them by ISO 639-1 code.
The segmenter is script-agnostic at inference, so the script half of a GlotLID
label is deliberately ignored and only the language subtag is resolved.

Alternative ISO 639-3 subtags for the same language are listed together, which
is why macrolanguages such as Malay and Chinese, and the individual codes
GlotLID prefers for them, both resolve to one segmenter code.
"""

from __future__ import annotations

# ISO 639-1 code -> the ISO 639-3 subtags GlotLID may emit for that language.
_SAT_CODE_TO_GLOTLID_SUBTAGS: dict[str, tuple[str, ...]] = {
    "af": ("afr",),
    "am": ("amh",),
    "ar": ("ara", "arb"),
    "az": ("aze", "azj"),
    "be": ("bel",),
    "bg": ("bul",),
    "bn": ("ben",),
    "ca": ("cat",),
    "ceb": ("ceb",),
    "cs": ("ces",),
    "cy": ("cym",),
    "da": ("dan",),
    "de": ("deu",),
    "el": ("ell",),
    "en": ("eng",),
    "eo": ("epo",),
    "es": ("spa",),
    "et": ("est", "ekk"),
    "eu": ("eus",),
    "fa": ("fas", "pes"),
    "fi": ("fin",),
    "fr": ("fra",),
    "fy": ("fry",),
    "ga": ("gle",),
    "gd": ("gla",),
    "gl": ("glg",),
    "gu": ("guj",),
    "ha": ("hau",),
    "he": ("heb",),
    "hi": ("hin",),
    "hu": ("hun",),
    "hy": ("hye",),
    "id": ("ind",),
    "ig": ("ibo",),
    "is": ("isl",),
    "it": ("ita",),
    "ja": ("jpn",),
    "jv": ("jav",),
    "ka": ("kat",),
    "kk": ("kaz",),
    "km": ("khm",),
    "kn": ("kan",),
    "ko": ("kor",),
    "ku": ("kur", "kmr", "ckb"),
    "ky": ("kir",),
    "la": ("lat",),
    "lt": ("lit",),
    "lv": ("lav", "lvs"),
    "mg": ("mlg", "plt"),
    "mk": ("mkd",),
    "ml": ("mal",),
    "mn": ("mon", "khk"),
    "mr": ("mar",),
    "ms": ("msa", "zsm"),
    "mt": ("mlt",),
    "my": ("mya",),
    "ne": ("nep", "npi"),
    "nl": ("nld",),
    "no": ("nor", "nob", "nno"),
    "pa": ("pan",),
    "pl": ("pol",),
    "ps": ("pus", "pbt"),
    "pt": ("por",),
    "ro": ("ron",),
    "ru": ("rus",),
    "si": ("sin",),
    "sk": ("slk",),
    "sl": ("slv",),
    "sq": ("sqi", "als"),
    "sr": ("srp",),
    "sv": ("swe",),
    "ta": ("tam",),
    "te": ("tel",),
    "tg": ("tgk",),
    "th": ("tha",),
    "tr": ("tur",),
    "uk": ("ukr",),
    "ur": ("urd",),
    "uz": ("uzb", "uzn"),
    "vi": ("vie",),
    "xh": ("xho",),
    "yi": ("yid", "ydd"),
    "yo": ("yor",),
    "zh": ("zho", "cmn", "yue"),
    "zu": ("zul",),
}

SAT_LANGUAGE_CODES = frozenset(_SAT_CODE_TO_GLOTLID_SUBTAGS)

_SUBTAG_TO_SAT_CODE: dict[str, str] = {
    subtag: code for code, subtags in _SAT_CODE_TO_GLOTLID_SUBTAGS.items() for subtag in subtags
}

SUPPORTED_GLOTLID_SUBTAGS = frozenset(_SUBTAG_TO_SAT_CODE)


def sat_code_for_glotlid_label(label: object) -> str | None:
    """Return the segmenter code for a GlotLID label, or ``None`` if uncovered."""
    if not isinstance(label, str):
        return None
    subtag = label.partition("_")[0]
    return _SUBTAG_TO_SAT_CODE.get(subtag)


__all__ = [
    "SAT_LANGUAGE_CODES",
    "SUPPORTED_GLOTLID_SUBTAGS",
    "sat_code_for_glotlid_label",
]
