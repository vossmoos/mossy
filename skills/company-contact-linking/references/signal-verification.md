# Verification — proving a signal before it is used to search

Step 3 asks a reader for values the mechanical extractor cannot see: the company name in the
signature, a legal form, a department line. Step 4 refuses to take any of them on trust.

The rule is narrow and mechanical: **a supplied value must appear literally in the ticket's raw
text, in a part of it that belongs to this requester.** A value that fails either half is
discarded, and the search runs without it. Discarding is not an error — it is the normal way a
half-read value leaves the pipeline.

## Why two halves

The client's requirement has two failure modes and they need different answers.

**Invented** — a name that is not in the document at all. A literal search over the raw text
catches this: if `Muster Sanitär AG` cannot be found, it was not read, it was produced.

**Read from the wrong place** — a name that really is in the text, just not as a statement about
the sender. This is the one that looks correct and is not:

- a signature inside a **quoted or forwarded** block, which belongs to whoever wrote that
  message, not to the person who sent this ticket;
- a company named as a **subject** rather than a sender: „die Firma X hat bei mir montiert";
- the recipient's own footer, or a legal disclaimer naming a third party.

A check that only asks "is this string present" passes all three. So presence is necessary and
not sufficient, and the second half of the check is about *where* the string sits.

## How the comparison is made

Compare on a normalised key rather than raw characters, because a signature is not typed the
way a directory stores it:

- lowercase; strip accents (`ä` → `ae`, `ü` → `ue`, `ö` → `oe`);
- collapse whitespace, including the line breaks a signature block is full of;
- drop punctuation, so `Muster Sanitär AG.` and `Muster Sanitaer AG` meet;
- keep digits as they are — a UID or a house number must match exactly.

The haystack is the ticket's **first message only**: subject plus body, as plain text. A later
reply is not evidence about the contact who opened the ticket.

Then remove the quoted region before searching. The usual markers, any of which opens a quoted
block that runs to the end of the message:

```
Von:        From:       De:
Gesendet:   Sent:
-----Ursprüngliche Nachricht-----
-----Original Message-----
Am ... schrieb ...:      On ... wrote:
> at the start of a line
```

A value found **only** inside that region is reported as `wrong_region`, not as missing.

## What `verify_signals.py` returns

```json
{
  "ok": true,
  "verified": {
    "companyNameCandidates": ["Muster Sanitär AG"],
    "legalForm": "AG",
    "emailDomain": "muster-sanitaer.example.ch"
  },
  "discarded": [
    {"field": "companyNameCandidates", "value": "Fremd GmbH", "code": "wrong_region",
     "detail": "nur im zitierten Teil der Nachricht gefunden"},
    {"field": "legalForm", "value": "Sàrl", "code": "not_in_text",
     "detail": "kommt im Ticket nicht vor"}
  ],
  "notes": []
}
```

- `verified` is what the search may use. Everything else is gone.
- `discarded` carries the field, the value, a code (`not_in_text`, `wrong_region`) and a short
  German detail, so the private note can say what was ignored and why without inventing a
  rationale.
- Mechanical signals from step 3 pass through unchecked: the email domain is read from the
  ticket's own metadata rather than from prose, so there is nothing to prove.

## When everything is discarded

That is a legitimate outcome, not a failure. The search then runs on the mechanical signals
alone — often just the sender's domain — and if those produce nothing either, the decision is
`no_match` and the note says which values were ignored. An empty answer is acceptable; a value
that survived only because nobody checked it is not.

## What this check does not do

It does not judge whether a company is the *right* one. A name can be present, in the right
region, and still belong to an installer the requester is writing about rather than to the
requester's employer. That judgement lives in `references/matching-rules.md` and in the
counter-signals of `references/detection-rules.md`. Verification only removes values that
cannot be evidence at all.
