# Copyright 2026 Biricon IT Services GmbH
# License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl).
"""Language names for prompts and ISO codes for machine translation engines."""

BASE_NAMES = {
    "af": "Afrikaans", "ar": "Arabic", "az": "Azerbaijani", "be": "Belarusian",
    "bg": "Bulgarian", "bn": "Bengali", "bs": "Bosnian", "ca": "Catalan",
    "ckb": "Central Kurdish (Sorani, Arabic script)", "cs": "Czech", "cy": "Welsh",
    "da": "Danish", "de": "German", "el": "Greek", "en": "English",
    "es": "Spanish", "et": "Estonian", "eu": "Basque", "fa": "Persian (Farsi)",
    "fi": "Finnish", "fr": "French", "ga": "Irish", "gl": "Galician",
    "he": "Hebrew", "hi": "Hindi", "hr": "Croatian", "hu": "Hungarian",
    "hy": "Armenian", "id": "Indonesian", "is": "Icelandic", "it": "Italian",
    "ja": "Japanese", "ka": "Georgian", "kk": "Kazakh", "ko": "Korean",
    "ku": "Kurdish (Kurmanji, Latin script)", "lb": "Luxembourgish",
    "lt": "Lithuanian", "lv": "Latvian", "mk": "Macedonian", "ms": "Malay",
    "mt": "Maltese", "nb": "Norwegian (Bokmål)", "nl": "Dutch",
    "nn": "Norwegian (Nynorsk)", "pl": "Polish", "pt": "Portuguese",
    "ro": "Romanian", "ru": "Russian", "sk": "Slovak", "sl": "Slovenian",
    "sq": "Albanian", "sr": "Serbian", "sv": "Swedish", "sw": "Swahili",
    "th": "Thai", "tr": "Turkish", "uk": "Ukrainian", "ur": "Urdu",
    "uz": "Uzbek", "vi": "Vietnamese", "zh": "Chinese",
}

# regional variants worth naming explicitly in a prompt
VARIANT_NAMES = {
    "de_AT": "German (Austria; use Austrian vocabulary and spelling)",
    "de_CH": "German (Switzerland; no ß)",
    "de_DE": "German (Germany)",
    "en_GB": "English (British spelling)",
    "en_US": "English (American spelling)",
    "en_AU": "English (Australian spelling)",
    "es_MX": "Spanish (Mexico)",
    "es_AR": "Spanish (Argentina)",
    "fr_CA": "French (Canada)",
    "fr_BE": "French (Belgium)",
    "fr_CH": "French (Switzerland)",
    "pt_BR": "Portuguese (Brazil)",
    "pt_PT": "Portuguese (Portugal)",
    "zh_CN": "Chinese (Simplified)",
    "zh_TW": "Chinese (Traditional, Taiwan)",
    "zh_HK": "Chinese (Traditional, Hong Kong)",
    "sr@latin": "Serbian (Latin script)",
    "sr_RS": "Serbian (Cyrillic script)",
    "ar_001": "Arabic (Modern Standard)",
    "nl_BE": "Dutch (Belgium, Flemish)",
    "it_CH": "Italian (Switzerland)",
}


def base_code(code):
    """'pt_BR' -> 'pt', 'sr@latin' -> 'sr'."""
    return (code or "").split("@")[0].split("_")[0].lower()


def lang_name(code, fallback=None):
    """English name of a language for use in prompts."""
    if code in VARIANT_NAMES:
        return VARIANT_NAMES[code]
    base = base_code(code)
    if base in BASE_NAMES:
        return BASE_NAMES[base]
    return (fallback or code or "").split("/")[0].strip() or code


def iso_code(code):
    """ISO 639 code understood by most engines ('pt_BR' -> 'pt')."""
    base = base_code(code)
    if code in ("zh_TW", "zh_HK"):
        return "zh-TW"
    return base
