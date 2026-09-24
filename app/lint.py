"""Deterministic checks and fixes applied to every draft, on top of OpenAI's
own self-check against Section 17. These catch things a model can drift on
even when told not to: punctuation fingerprint, banned words, British
spelling, word count, and no links in the post body.

`autofix()` mechanically repairs what can be repaired without an LLM call
(dashes, banned words, obvious American spellings). `check()` returns the
warnings that remain after autofix, to show Meera rather than hide.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

MIN_WORDS = 250
MAX_WORDS = 450

# Em/en dash and similar -> her spaced hyphen. Order matters: do multi-char first.
_DASH_RE = re.compile(r"\s*[—–]\s*")  # em dash, en dash

_BOLD_RE = re.compile(r"\*\*([^*]+)\*\*")
_HASHTAG_RE = re.compile(r"(?<!\w)#\w+")
_URL_RE = re.compile(r"https?://\S+")
_EMOJI_RE = re.compile(
    "[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF]"
)

BANNED_WORDS = [
    "amazing", "love", "glow", "radiant", "transform", "transformative",
    "journey", "self-care", "toxins", "chemical-free", "game-changer",
    "hero ingredient", "skin-loving", "multi-peptide complex",
]

AMERICAN_TO_BRITISH = {
    "oxidize": "oxidise", "oxidizes": "oxidises", "oxidized": "oxidised",
    "oxidizing": "oxidising",
    "sensitization": "sensitisation", "sensitizes": "sensitises",
    "sensitized": "sensitised",
    "behavior": "behaviour", "behaviors": "behaviours",
    "color": "colour", "colors": "colours", "colored": "coloured",
    "moisturizer": "moisturiser", "moisturizers": "moisturisers",
    "moisturize": "moisturise", "moisturizing": "moisturising",
    "favor": "favour", "favorable": "favourable",
    "flavor": "flavour", "flavors": "flavours",
    "organization": "organisation", "organizations": "organisations",
    "analyze": "analyse", "analyzed": "analysed", "analyzing": "analysing",
    "characterize": "characterise", "characterized": "characterised",
    "utilize": "utilise", "utilizes": "utilises", "utilized": "utilised",
    "recognize": "recognise", "recognized": "recognised",
    "emphasize": "emphasise", "emphasized": "emphasised",
    "signaling": "signalling", "labeled": "labelled", "labeling": "labelling",
}

_SCIENCE_CLAIM_RE = re.compile(
    r"\bpH\b|\bconcentration\b|\bstudy\b|\bstudies\b|%|\bregulat\w*\b", re.I
)


def _case_preserving_sub(word_from: str, word_to: str, text: str) -> str:
    def repl(m: re.Match) -> str:
        s = m.group(0)
        if s.isupper():
            return word_to.upper()
        if s[0].isupper():
            return word_to.capitalize()
        return word_to

    return re.sub(rf"\b{re.escape(word_from)}\b", repl, text, flags=re.I)


@dataclass
class LintResult:
    text: str
    warnings: list[str] = field(default_factory=list)

    @property
    def word_count(self) -> int:
        return len(self.text.split())


def autofix(text: str) -> str:
    """Mechanical fixes that don't need judgement."""
    fixed = _DASH_RE.sub(" - ", text)
    fixed = _BOLD_RE.sub(r"\1", fixed)  # drop bold emphasis, keep the words
    fixed = _HASHTAG_RE.sub("", fixed)
    fixed = _EMOJI_RE.sub("", fixed)
    for us, gb in AMERICAN_TO_BRITISH.items():
        fixed = _case_preserving_sub(us, gb, fixed)
    # Banned words are flagged (see check()) rather than auto-stripped here,
    # since removing them safely needs judgement about the surrounding sentence.
    fixed = re.sub(r"[ \t]+\n", "\n", fixed)
    fixed = re.sub(r"[ \t]{2,}", " ", fixed)
    return fixed.strip()


def check(text: str) -> LintResult:
    """Runs autofix, then returns remaining warnings that need a human look
    or an OpenAI fix pass rather than a safe mechanical rewrite.
    """
    fixed = autofix(text)
    warnings: list[str] = []

    if "!" in fixed:
        warnings.append("contains an exclamation mark")
    if ";" in fixed:
        warnings.append("contains a semicolon")
    if _HASHTAG_RE.search(text):
        warnings.append("contained hashtag(s), removed")
    if _URL_RE.search(fixed):
        warnings.append("contains a URL in the post body - links should not appear in the post text")
    if _EMOJI_RE.search(text):
        warnings.append("contained emoji, removed")

    lower = fixed.lower()
    hit_banned = [w for w in BANNED_WORDS if w in lower]
    if hit_banned:
        warnings.append(f"contains banned word(s): {', '.join(hit_banned)}")

    first_line = fixed.strip().splitlines()[0] if fixed.strip() else ""
    if re.match(r"^(hi|hello|hey)[,!.]?\s*$", first_line.strip(), re.I):
        warnings.append("opens with a greeting")
    last_line = fixed.strip().splitlines()[-1] if fixed.strip() else ""
    if re.match(r"^(meera|thanks|best|regards)[,.]?\s*$", last_line.strip(), re.I):
        warnings.append("ends with a sign-off")

    stripped = fixed.rstrip()
    if stripped.endswith("?"):
        warnings.append("ends on a question")
    q_count = fixed.count("?")
    if q_count > 1:
        warnings.append(f"contains {q_count} question marks - questions should be rare")

    wc = len(fixed.split())
    if wc < MIN_WORDS:
        warnings.append(f"word count {wc} is below the {MIN_WORDS}-{MAX_WORDS} target")
    elif wc > MAX_WORDS:
        warnings.append(f"word count {wc} is above the {MIN_WORDS}-{MAX_WORDS} target")

    if _SCIENCE_CLAIM_RE.search(fixed) and "[VERIFY]" not in fixed:
        warnings.append(
            "mentions a scientific/technical detail (pH, %, concentration, study, "
            "regulation) with no [VERIFY] tag anywhere in the draft"
        )

    return LintResult(text=fixed, warnings=warnings)


def strip_verify_tags(text: str) -> str:
    """Removes [VERIFY]...[/VERIFY] wrapper tags but keeps the wrapped text,
    for the clean copy sent to Meera on approval.
    """
    text = re.sub(r"\[VERIFY\]", "", text)
    text = re.sub(r"\[/VERIFY\]", "", text)
    return text


def count_verify_tags(text: str) -> int:
    return len(re.findall(r"\[VERIFY\]", text))
