# Copyright 2026 Biricon IT Services GmbH
# License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl).
"""Deterministic plausibility checks for a proposed text.

Hard errors exclude a candidate from the vote (markup or placeholders were
damaged). Warnings are shown to the reviewers and the human but do not
exclude anything.
"""
import re
import unicodedata
from collections import Counter
from difflib import SequenceMatcher

from .languages import base_code

TAG = re.compile(r"<[^>]*>")
ENTITY = re.compile(r"&[#\w]+;")
PY_PLACEHOLDER = re.compile(r"%\([^)]+\)[sdif]|%[sdif]")
BRACE_PLACEHOLDER = re.compile(r"\{\{.*?\}\}|\$\{[^}]*\}|#\{[^}]*\}|\{[A-Za-z_][\w.]*\}")
URL = re.compile(r"https?://[^\s\"'<>)]+|[\w.+-]+@[\w-]+\.[\w.-]+")
NUMBER = re.compile(r"\d+(?:[.,\u066b\u066c]\d+)*")
LETTER = re.compile(r"[^\W\d_]", re.UNICODE)
CHATTER = re.compile(
    r"^\s*(sure|certainly|here is|here's|of course|translation:|übersetzung:|"
    r"gerne|hier ist|```)", re.IGNORECASE)


def has_letters(text):
    return bool(LETTER.search(text or ""))


def _same(pattern, source, target):
    return Counter(pattern.findall(source)) == Counter(pattern.findall(target))


def numbers(text):
    """Numbers of a text as plain digit strings: Arabic-Indic or Devanagari digits and the
    thousands/decimal separators of every language compare equal ('1.000' == '1,000' == '١٬٠٠٠')."""
    return Counter("".join(str(unicodedata.decimal(ch, "")) for ch in match if ch.isdigit())
                   for match in NUMBER.findall(text or ""))


# ---------------------------------------------------------------- script of the target language
LATIN = re.compile(r"[A-Za-z\u00c0-\u024f\u1e00-\u1eff]")
_CYRILLIC = re.compile(r"[\u0400-\u052f]")
_ARABIC = re.compile(r"[\u0600-\u06ff\u0750-\u077f\u08a0-\u08ff\ufb50-\ufdff\ufe70-\ufeff]")
_HAN = r"\u3400-\u4dbf\u4e00-\u9fff"
SCRIPTS = {
    "ru": _CYRILLIC, "uk": _CYRILLIC, "bg": _CYRILLIC, "be": _CYRILLIC, "mk": _CYRILLIC,
    "ar": _ARABIC, "fa": _ARABIC, "ur": _ARABIC, "ckb": _ARABIC,
    "el": re.compile(r"[\u0370-\u03ff\u1f00-\u1fff]"),
    "he": re.compile(r"[\u0590-\u05ff]"),
    "th": re.compile(r"[\u0e00-\u0e7f]"),
    "hi": re.compile(r"[\u0900-\u097f]"),
    "bn": re.compile(r"[\u0980-\u09ff]"),
    "ka": re.compile(r"[\u10a0-\u10ff\u2d00-\u2d2f]"),
    "hy": re.compile(r"[\u0530-\u058f]"),
    "ko": re.compile(r"[\u1100-\u11ff\u3130-\u318f\uac00-\ud7af%s]" % _HAN),
    "ja": re.compile(r"[\u3040-\u30ff\u31f0-\u31ff%s]" % _HAN),
    "zh": re.compile(r"[%s]" % _HAN),
}
# several scripts in use: nothing to check
ANY_SCRIPT = {"sr", "kk", "mn", "tg", "ky", "tk", "pa", "sd"}
SCRIPT_MIN_LETTERS = 8
LATIN_MIN_SHARE = 0.5
NAME_MAX_WORDS = 5                # an unchanged text of this many words is a name or label, not prose
OTHER_SCRIPT_MIN_SHARE = 0.2      # brand names stay Latin inside a Chinese or Arabic text


def prose(text):
    """The words of a text without markup, placeholders, entities and addresses."""
    for pattern in (TAG, ENTITY, URL, BRACE_PLACEHOLDER, PY_PLACEHOLDER):
        text = pattern.sub(" ", text or "")
    return text


def check_script(text, lang):
    """Error message when ``text`` is clearly not written in the script of ``lang``."""
    base = base_code(lang)
    if not base or base in ANY_SCRIPT:
        return ""
    letters = LETTER.findall(prose(text))
    if len(letters) < SCRIPT_MIN_LETTERS:
        return ""
    pattern = SCRIPTS.get(base)
    share = len(pattern.findall("".join(letters)) if pattern else LATIN.findall("".join(letters))) / len(letters)
    if share < (OTHER_SCRIPT_MIN_SHARE if pattern else LATIN_MIN_SHARE):
        return "not written in the script of the target language"
    return ""


_WORD = re.compile(r"[\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af]|\w+")
BACK_MIN_CHARS = 15


def back_similarity(source, back):
    """0-1 similarity of a source text and its translation back into the same language.

    Average of the word overlap (F1) and the character similarity; both ignore markup, case and
    punctuation. A faithful translation comes back as a paraphrase (about 0.4-0.9), a translation
    with the wrong meaning shares hardly anything with the source.
    """
    a = [w.casefold() for w in _WORD.findall(prose(source))]
    b = [w.casefold() for w in _WORD.findall(prose(back))]
    if not a or not b:
        return 0.0
    overlap = sum((Counter(a) & Counter(b)).values())
    f1 = 2.0 * overlap / (len(a) + len(b))
    chars = SequenceMatcher(None, " ".join(a), " ".join(b), autojunk=False).ratio()
    return (f1 + chars) / 2.0


def check_glossary(source, target, terms, strict_keep=True):
    """``terms``: ``[(term, translation or "")]``. Returns ``(errors, warnings)``.

    A term left unchanged on purpose (brand name) must survive in the target; a fixed translation
    may be inflected in the target language, so a missing one is only a warning.
    """
    errors, warnings = [], []
    low_source, low_target = source.casefold(), target.casefold()
    for term, fixed in terms:
        if term.casefold() not in low_source:
            continue
        if fixed:
            if fixed.casefold() not in low_target:
                warnings.append('glossary: "%s" should be "%s"' % (term, fixed))
        elif term.casefold() not in low_target:
            (errors if strict_keep else warnings).append('glossary: "%s" must stay unchanged' % term)
    return errors, warnings


PROOFREAD_MIN_SIMILARITY = 0.6


def similarity(a, b):
    """0-1; cheap upper bounds first so long texts stay fast."""
    a, b = normalize(a), normalize(b)
    if a == b:
        return 1.0
    matcher = SequenceMatcher(None, a, b, autojunk=False)
    if matcher.real_quick_ratio() < PROOFREAD_MIN_SIMILARITY or matcher.quick_ratio() < PROOFREAD_MIN_SIMILARITY:
        return matcher.quick_ratio()
    return matcher.ratio()


def check(source, target, kind="translate", lang="", terms=()):
    """Return ``(errors, warnings)`` as lists of short messages.

    ``lang``: language the target is written in (script check); ``terms``: glossary entries.
    """
    errors, warnings = [], []
    if target is None or not isinstance(target, str) or not target.strip():
        return ["empty text"], warnings
    if source.strip() == "":
        return errors, warnings
    for label, pattern in (
        ("HTML tags", TAG), ("entities", ENTITY), ("%-placeholders", PY_PLACEHOLDER),
        ("{}-placeholders", BRACE_PLACEHOLDER), ("URLs/e-mail addresses", URL),
    ):
        if not _same(pattern, source, target):
            errors.append("%s changed" % label)
    if CHATTER.match(target) and not CHATTER.match(source):
        errors.append("contains chatter around the text")
    # A name, brand or domain ("ExampleNet", "www.example.at") stays as it is in every script.
    keeps_name = source.strip() == target.strip() and len(prose(source).split()) <= NAME_MAX_WORDS
    wrong = check_script(target, lang) if lang and not keeps_name else ""
    if wrong:
        errors.append(wrong)
    if terms:
        gl_errors, gl_warnings = check_glossary(source, target, terms, strict_keep=not _non_latin(lang))
        errors += gl_errors
        warnings += gl_warnings
    src_numbers, tgt_numbers = numbers(source), numbers(target)
    if src_numbers != tgt_numbers:
        # A proofread text keeps its numbers. In a translation a single digit may be spelled out
        # ("0 Tage" -> "zero-day"); a changed or invented number, or a longer one that vanished, may not.
        if kind == "proofread":
            errors.append("numbers differ")
        elif (tgt_numbers - src_numbers) or any(len(n) > 1 for n in src_numbers - tgt_numbers):
            warnings.append("numbers differ")
    if kind == "proofread" and has_letters(source) and similarity(source, target) < PROOFREAD_MIN_SIMILARITY:
        errors.append("too different from the original for a proofread (wrong text returned?)")
    ls, lt = len(source), len(target)
    if ls > 20 and (lt > 3.5 * ls or lt < 0.2 * ls):
        warnings.append("suspicious length (%d -> %d characters)" % (ls, lt))
    if source.count("\n") != target.count("\n"):
        warnings.append("line breaks differ")
    if kind == "translate" and ls > 25 and has_letters(source) \
            and source.strip() == target.strip():
        warnings.append("identical to the source")
    return errors, warnings


def _non_latin(lang):
    return base_code(lang) in SCRIPTS


def normalize(text):
    """Key under which two texts count as the same candidate."""
    return re.sub(r"\s+", " ", (text or "").replace("\xa0", " ")).strip()
