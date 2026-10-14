"""Text handling: HTML to plain text, the signature block, and normalisation for comparing.

Kept apart from the Freshdesk client because every one of these is a judgement about text that
tests can pin down without a network. Standard library only.
"""

from __future__ import annotations

import html
import os
import re
import unicodedata

# Closings that usually sit directly above a signature block. Overridable per market: a Swiss
# helpdesk sees different wording from a German or French one.
DEFAULT_SIGNATURE_MARKERS = (
    "freundliche grüsse,freundliche grüße,mit freundlichen grüssen,mit freundlichen grüßen,"
    "beste grüsse,beste grüße,liebe grüsse,liebe grüße,herzliche grüsse,freundlichen gruss,"
    "best regards,kind regards,regards,cordialement,salutations distinguées,saludos cordiales,"
    "atentamente,cordiali saluti"
)

# Markers that open a quoted or forwarded block. Everything from here to the end of the message
# was written by somebody else, so it is not evidence about this requester.
DEFAULT_QUOTE_MARKERS = (
    "von:,from:,de:,gesendet:,sent:,-----ursprüngliche nachricht-----,"
    "-----original message-----,-----weitergeleitete nachricht-----,am  schrieb,on  wrote"
)

# Block-level markup becomes a line break; everything else becomes a space. A tag that is
# simply deleted glues the words on either side of it together — "SAXAG Test AG" followed by
# "Luzernstrasse 12" becomes one token and no company name will ever match it again.
_BLOCK_RE = re.compile(
    r"(?i)<br\s*/?>|</?(?:p|div|tr|li|h[1-6]|table|blockquote|section|article)\b[^>]*>"
)
_TAG_RE = re.compile(r"<[^>]+>")
_SPACE_RE = re.compile(r"[ \t]+")
_BLANKS_RE = re.compile(r"\n{3,}")


def _list_env(name: str, default: str) -> list[str]:
    raw = (os.environ.get(name) or default).strip()
    return [part.strip().lower() for part in raw.split(",") if part.strip()]


def html_to_text(value: str | None) -> str:
    """Freshdesk bodies are HTML; this keeps the line structure a signature depends on."""
    if not value:
        return ""
    text = _BLOCK_RE.sub("\n", value)
    text = _TAG_RE.sub(" ", text)  # a space, never nothing: see _BLOCK_RE above
    text = html.unescape(text)
    text = text.replace("\xa0", " ").replace("\r\n", "\n").replace("\r", "\n")
    text = "\n".join(_SPACE_RE.sub(" ", line).strip() for line in text.split("\n"))
    return _BLANKS_RE.sub("\n\n", text).strip()


def _marker_position(haystack: str, markers: list[str]) -> int:
    """Where a quote marker starts in this text, or -1.

    Markers are matched **anywhere**, not only at the start of a line. Freshdesk hands back
    ``description_text`` with every newline flattened to a space for tickets written in its own
    UI, so "… Freundliche Grüsse … Von: Dritte Firma AG …" arrives as one line. A start-of-line
    rule finds no quoted part there and hands the other company's signature to the sender —
    measured on two tickets, where the quoted firm's phone came back attributed to the writer.
    """
    lowered = haystack.lower()
    best = -1
    for marker in markers:
        # "am  schrieb" stands for "Am <date> schrieb <name>:" — both halves, in order.
        parts = [p for p in marker.split("  ") if p]
        start = lowered.find(parts[0])
        while start != -1:
            rest = lowered[start:]
            if all(part in rest for part in parts[1:]):
                if best == -1 or start < best:
                    best = start
                break
            start = lowered.find(parts[0], start + 1)
    return best


def quoted_split(text: str) -> tuple[str, str]:
    """Split into (the sender's own message, the quoted remainder).

    The earliest quote marker wins: everything from there on belongs to another message. A
    ``>`` at the start of a line counts as one too.
    """
    markers = _list_env("LINKING_QUOTE_MARKERS", DEFAULT_QUOTE_MARKERS)
    lines = text.split("\n")
    for index, line in enumerate(lines):
        if line.strip().startswith(">"):
            return "\n".join(lines[:index]).strip(), "\n".join(lines[index:]).strip()
    cut = _marker_position(text, markers)
    if cut == -1:
        return text.strip(), ""
    return text[:cut].strip(), text[cut:].strip()


def signature_block(text: str) -> str:
    """The sender's own signature: what follows the last closing before any quoted part.

    Returns an empty string when no closing is found, which is a normal outcome — plenty of
    messages have no signature, and inventing one would hand the matcher a phantom.
    """
    own, _quoted = quoted_split(text)
    markers = _list_env("LINKING_SIGNATURE_MARKERS", DEFAULT_SIGNATURE_MARKERS)
    lines = own.split("\n")
    last_hit = -1
    for index, line in enumerate(lines):
        probe = line.strip().lower()
        if any(probe.startswith(marker) for marker in markers):
            last_hit = index
    if last_hit != -1:
        block = [line for line in lines[last_hit + 1 :] if line.strip()]
        return "\n".join(block).strip()

    # Nothing at the start of a line: the body may be flattened into one. Take what follows the
    # last closing found anywhere, which is the sender's own block when there is one.
    lowered = own.lower()
    cut = -1
    for marker in markers:
        found = lowered.rfind(marker)
        if found > cut:
            cut = found + len(marker)
    if cut == -1:
        return ""
    return own[cut:].strip(" ,\n\t")


def compare_key(value: object) -> str:
    """Letters and digits only, lowercased, accents folded.

    Signatures are not typed the way a directory stores a name: ``Muster Sanitär AG.`` and
    ``muster sanitaer ag`` are the same company, and a comparison that says otherwise is
    useless. Digits survive untouched — a UID or a house number must match exactly.
    """
    text = str(value or "").lower()
    text = text.replace("ä", "ae").replace("ö", "oe").replace("ü", "ue").replace("ß", "ss")
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return re.sub(r"[^0-9a-z]+", "", text)


# Legal forms as they are actually capitalised, used to find one that lost the space in front
# of it. Only these — the capitalisation IS the evidence, so a form with no settled spelling
# cannot take part.
GLUED_FORMS = (
    "GmbH", "AG", "SA", "Sàrl", "SARL", "SAGL", "SRL", "SpA", "KG", "OHG", "UG", "KGaA",
    "GbR", "BV", "NV", "SL", "Cie",
)
# A lowercase letter, then a legal form, then no more letters. The lookbehind is the whole
# safety of this: it fires on "TestGmbH" and never on "Tesco", "Montag", "Zug" or "SAXAG",
# where the letters are there but no missing space is.
_GLUED_FORM_RE = re.compile(
    r"(?<=[a-zà-öø-ÿ])(" + "|".join(GLUED_FORMS) + r")(?![A-Za-zÀ-ÿ])"
)

LEGAL_FORMS = (
    "ag", "gmbh", "sa", "sarl", "sagl", "kg", "ohg", "ek", "eK", "bv", "nv", "sl", "srl", "spa",
    "co", "cie", "ug", "kgaa", "gbr",
)


def name_key(value: object) -> str:
    """A company name reduced for comparison: no legal form, no article, no punctuation.

    ``Muster AG`` and ``Muster GmbH`` both reduce to ``muster``. That is deliberate — it makes
    them *candidates*, and telling them apart is the decision's job, not this function's.

    A form glued to the word before it counts as a form. ``Muster TestGmbH`` is what a signature
    looks like when a space went missing, and it is the case this key exists to survive: the
    verification gate cannot catch it, because ``compare_key`` drops spaces and so reads the
    glued and the spaced spelling as identical. Left alone, the glued name keeps its ``gmbh``,
    misses the directory entry that dropped it, and a second company is created beside the
    first — the exact outcome the duplicate check is here to prevent.

    Only capitalisation may be used as evidence of the missing space, never the letters alone.
    ``Tesco``, ``Montag``, ``Zug`` and ``SAXAG Test AG`` all end in a legal form's letters
    without containing one, and collapsing them would merge companies that are not the same.
    """
    text = _GLUED_FORM_RE.sub(r" \1", str(value or "")).lower()
    text = re.sub(r"^(die|der|das|la|le|el)\s+", "", text.strip())
    tokens = [t for t in re.split(r"[^0-9a-zà-ÿäöüß]+", text) if t]
    # Compared folded, so an accented ``Sàrl`` is recognised as the form it is. Matching the
    # raw token missed it entirely, which meant a Sàrl kept its legal form while every AG and
    # GmbH lost theirs — the two spellings of the same company never met.
    folded = {compare_key(form) for form in LEGAL_FORMS}
    kept = [t for t in tokens if compare_key(t) not in folded] or tokens
    return compare_key(" ".join(kept))


_MARKDOWN_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")


def url_host(value: str | None) -> str:
    """The bare host of a URL: no scheme, no path, no ``www.``, no markdown around it.

    Freshdesk's plain-text rendering turns an anchor into ``[www.x.example.ch](https://www.x.example.ch)``, and
    a value shaped like that can never equal a company's ``domains`` entry — which is the whole
    point of collecting URLs. What is comparable is the host.
    """
    text = (value or "").strip()
    if not text:
        return ""
    markdown = _MARKDOWN_LINK_RE.search(text)
    if markdown:
        text = markdown.group(2).strip() or markdown.group(1).strip()
    text = text.strip("<>\"'").rstrip(".,;:)")
    text = re.sub(r"^[a-z][a-z0-9+.-]*://", "", text, flags=re.IGNORECASE)
    text = text.split("/")[0].split("?")[0].split("#")[0]
    text = text.split("@")[-1]  # a URL with credentials in it
    text = text.split(":")[0]  # strip a port
    text = text.lower().lstrip(".")
    return text[4:] if text.startswith("www.") else text


def email_domain(email: str | None) -> str:
    if not email or "@" not in email:
        return ""
    return email.strip().lower().rsplit("@", 1)[-1]


__all__ = [
    "compare_key",
    "email_domain",
    "html_to_text",
    "name_key",
    "quoted_split",
    "signature_block",
    "url_host",
]
