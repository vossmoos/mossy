"""Configuration for the company-contact-linking skill.

Everything is an environment variable. Nothing about a deployment — group ids, field names,
company names, domain lists — is written into the code, so the folder can move between
repositories unchanged.

``LINKING_CONTACT_LOG_FIELD`` deliberately has **no** default and raises when asked for while
unset, the same way missing credentials do: writing a log line into a guessed contact field
corrupts data nobody is watching. Any field is acceptable as long as a deployment names one.
"""

from __future__ import annotations

import os

from _env import load_dotenv_if_present

# Fills only what the environment does not already carry; under Mossy it finds everything set.
load_dotenv_if_present()

DEFAULT_GENERIC_EMAIL_DOMAINS = (
    "gmail.com,gmx.ch,gmx.de,gmx.net,hotmail.com,outlook.com,bluewin.ch,icloud.com,"
    "yahoo.com,web.de"
)
DEFAULT_PROCESSED_TAG = "mossy-company-linked"
# One tag per outcome, not one tag for "seen". A tag is what stops the duty looking at a ticket
# again, so it decides what may still be revisited — and that answer differs per outcome:
# a ticket with nothing in it never becomes linkable, while one whose company was simply not in
# the directory yet becomes linkable the moment somebody adds it. A single tag would flatten
# both into "done" and there would be no way to tell them apart later without re-reading every
# ticket. Separate tags keep the choice open and read honestly in a Freshdesk filter.
DEFAULT_TAG_NO_SIGNALS = "mossy-company-no-signals"
DEFAULT_TAG_NO_MATCH = "mossy-company-no-match"
DEFAULT_TAG_AMBIGUOUS = "mossy-company-ambiguous"
# Confirmed with the client. A candidate at or above this links; two candidates at or above it
# are ambiguous and nothing is linked.
DEFAULT_MIN_MATCH_SCORE = 0.9
DEFAULT_MAX_CANDIDATES = 5
# How much of the company directory a listing sweep may read when the search index is lagging.
DEFAULT_DIRECTORY_MAX_PAGES = 10
DEFAULT_DIRECTORY_PAGE_SIZE = 100

# Score contributed by each way a candidate can be found. Weights, not thresholds: they say how
# much stronger a domain match is than a partial name, and the threshold that turns a score into
# a link is the client's call (LINKING_MIN_MATCH_SCORE).
DEFAULT_SCORE_DOMAIN = 1.0
DEFAULT_SCORE_NAME_EXACT = 0.9
DEFAULT_SCORE_NAME_CONTAINS = 0.6
DEFAULT_SCORE_UID = 1.0


class ConfigError(RuntimeError):
    """A setting is missing or unusable, and there is no safe value to assume."""


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def _float_env(name: str, default: float) -> float:
    raw = _env(name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        raise ConfigError(f"{name}={raw!r} is not a number.") from None


def _int_env(name: str, default: int) -> int:
    raw = _env(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        raise ConfigError(f"{name}={raw!r} is not a whole number.") from None


def enabled() -> bool:
    """Master switch. The skill ships inert."""
    return _env("LINKING_ENABLED", "false").lower() == "true"


def group_ids() -> set[str]:
    """Groups the skill may act on.

    **Empty means no group.** A deployment that has not configured a scope does nothing, rather
    than treating every ticket in the helpdesk as fair game.
    """
    raw = _env("LINKING_GROUP_IDS")
    return {part.strip() for part in raw.split(",") if part.strip()}


def is_in_scope(group_id: object) -> bool:
    return str(group_id or "").strip() in group_ids()


def generic_email_domains() -> set[str]:
    """Free-mail domains that say nothing about an employer."""
    raw = _env("LINKING_GENERIC_EMAIL_DOMAINS", DEFAULT_GENERIC_EMAIL_DOMAINS)
    return {part.strip().lower() for part in raw.split(",") if part.strip()}


def min_match_score() -> float:
    """Score a candidate needs before a link is justified. 0.9, confirmed with the client."""
    value = _float_env("LINKING_MIN_MATCH_SCORE", DEFAULT_MIN_MATCH_SCORE)
    if not 0 < value <= 1:
        raise ConfigError(f"LINKING_MIN_MATCH_SCORE={value} must be greater than 0 and at most 1.")
    return value


def contact_log_field() -> str:
    """Contact field the German log line is appended to. No default: it must be named.

    Any field is acceptable — the client's only requirement is that it stays configurable —
    but guessing one would append text to whatever happened to be there.
    """
    raw = _env("LINKING_CONTACT_LOG_FIELD")
    if not raw:
        raise ConfigError(
            "LINKING_CONTACT_LOG_FIELD is not set. Name the contact field the German log line "
            "is appended to; there is no safe field to assume."
        )
    return raw


def allow_company_creation() -> bool:
    """Whether a company may be created when the directory has no match. True per the client."""
    return _env("LINKING_ALLOW_COMPANY_CREATION", "true").lower() == "true"


def company_field_names() -> dict[str, str]:
    """Freshdesk field names used when creating a company.

    Only the name is a first-class Freshdesk field; address, phone and email live wherever the
    account keeps them, which differs per deployment — hence one variable each rather than a
    guess baked into the code. An empty value means "this account has no such field": that
    detail is simply not written.
    """
    return {
        "name": _env("LINKING_COMPANY_FIELD_NAME", "name"),
        "address": _env("LINKING_COMPANY_FIELD_ADDRESS"),
        "phone": _env("LINKING_COMPANY_FIELD_PHONE"),
        "email": _env("LINKING_COMPANY_FIELD_EMAIL"),
    }


def processed_tag() -> str:
    return _env("LINKING_PROCESSED_TAG", DEFAULT_PROCESSED_TAG)


def outcome_tags() -> dict[str, str]:
    """The tag written for each decision that does not link."""
    return {
        "no_signals": _env("LINKING_TAG_NO_SIGNALS", DEFAULT_TAG_NO_SIGNALS),
        "no_match": _env("LINKING_TAG_NO_MATCH", DEFAULT_TAG_NO_MATCH),
        "ambiguous": _env("LINKING_TAG_AMBIGUOUS", DEFAULT_TAG_AMBIGUOUS),
    }


def all_processed_tags() -> set[str]:
    """Every tag that means this ticket has been through the skill, whatever the outcome.

    The idempotency check reads all of them: a ticket that was declined must not be evaluated
    again on the next pass of the duty just because it was not linked.
    """
    return {processed_tag(), *outcome_tags().values()} - {""}


def declined_note_enabled() -> bool:
    """May a note be posted on a ticket the skill declined to act on?

    Off by default, and it stays off until the client approves the wording. The texts exist in
    references/log-and-note-texts.md marked as not approved; publishing them early would put
    sentences nobody signed off on onto live customer tickets, where they cannot be recalled.
    """
    return _env("LINKING_DECLINED_NOTE_ENABLED", "false").lower() == "true"


def max_candidates() -> int:
    return max(1, _int_env("LINKING_MAX_CANDIDATES", DEFAULT_MAX_CANDIDATES))


def directory_limits() -> tuple[int, int]:
    """(pages, page size) for a directory sweep."""
    return (
        max(1, _int_env("LINKING_DIRECTORY_MAX_PAGES", DEFAULT_DIRECTORY_MAX_PAGES)),
        max(1, min(100, _int_env("LINKING_DIRECTORY_PAGE_SIZE", DEFAULT_DIRECTORY_PAGE_SIZE))),
    )


def scores() -> dict[str, float]:
    return {
        "domain": _float_env("LINKING_SCORE_DOMAIN", DEFAULT_SCORE_DOMAIN),
        "name_exact": _float_env("LINKING_SCORE_NAME_EXACT", DEFAULT_SCORE_NAME_EXACT),
        "name_contains": _float_env("LINKING_SCORE_NAME_CONTAINS", DEFAULT_SCORE_NAME_CONTAINS),
        "uid": _float_env("LINKING_SCORE_UID", DEFAULT_SCORE_UID),
    }


def freshdesk_credentials() -> tuple[str, str]:
    domain = _env("FRESHDESK_DOMAIN")
    api_key = _env("FRESHDESK_API_KEY")
    if not domain or not api_key:
        raise ConfigError(
            "Freshdesk is not configured. Set FRESHDESK_DOMAIN and FRESHDESK_API_KEY "
            "(the API key is the base64 of '<api_key>:X', sent verbatim as Basic auth)."
        )
    return domain, api_key


__all__ = [
    "ConfigError",
    "allow_company_creation",
    "company_field_names",
    "contact_log_field",
    "directory_limits",
    "enabled",
    "freshdesk_credentials",
    "generic_email_domains",
    "group_ids",
    "is_in_scope",
    "max_candidates",
    "min_match_score",
    "processed_tag",
    "scores",
]
