# Copyright 2026 Biricon IT Services GmbH
# License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl).
"""Deterministic plausibility checks for a proposed text.

Hard errors exclude a candidate from the vote (markup or placeholders were
damaged). Warnings are shown to the reviewers and the human but do not
exclude anything.
"""
import re
from collections import Counter
from difflib import SequenceMatcher

TAG = re.compile(r"<[^>]*>")
ENTITY = re.compile(r"&[#\w]+;")
PY_PLACEHOLDER = re.compile(r"%\([^)]+\)[sdif]|%[sdif]")
BRACE_PLACEHOLDER = re.compile(r"\{\{.*?\}\}|\$\{[^}]*\}|#\{[^}]*\}|\{[A-Za-z_][\w.]*\}")
URL = re.compile(r"https?://[^\s\"'<>)]+|[\w.+-]+@[\w-]+\.[\w.-]+")
NUMBER = re.compile(r"\d+(?:[.,]\d+)*")
LETTER = re.compile(r"[^\W\d_]", re.UNICODE)
CHATTER = re.compile(
    r"^\s*(sure|certainly|here is|here's|of course|translation:|übersetzung:|"
    r"gerne|hier ist|```)", re.IGNORECASE)


def has_letters(text):
    return bool(LETTER.search(text or ""))


def _same(pattern, source, target):
    return Counter(pattern.findall(source)) == Counter(pattern.findall(target))


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


def check(source, target, kind="translate"):
    """Return ``(errors, warnings)`` as lists of short messages."""
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
    if not _same(NUMBER, source, target):
        # A proofread text keeps its numbers; in a translation they may be spelled out.
        (errors if kind == "proofread" else warnings).append("numbers differ")
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


def normalize(text):
    """Key under which two texts count as the same candidate."""
    return re.sub(r"\s+", " ", (text or "").replace("\xa0", " ")).strip()
