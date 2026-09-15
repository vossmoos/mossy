# Detection — what counts as a company signal

Two kinds of signal reach this skill, and keeping them apart matters.

**Mechanical signals** are extracted by `extract_company_signals.py` and are not a matter of
judgement: the sender's email domain, URLs in the body or signature, a Swiss UID/CHE number, a
phone country prefix. A script finds them the same way every time.

**Read signals** are the ones only a reader sees: the company name printed in a signature
block, a legal form attached to a name, a department line, a sentence like „ich schreibe im
Auftrag der …". That is your job, following this file.

Never fold one into the other. A company name you *inferred* from an email domain is not a read
signal — the domain is the signal, and the matching step decides what it is worth.

## The two layers, and the one JSON they share

`extract_company_signals.py` runs first and fills the **mechanical** fields. You then read the
ticket, following this file, and fill the **read** fields on the same object. One shape, two
authors, and every value carrying where it came from — because the next step
(`verify_signals.py`) treats those two kinds of value very differently.

```json
{
  "ok": true,
  "ticket_id": 63,
  "signals": {
    "emailDomain": "muster-sanitaer.example.ch",
    "emailDomainIsGeneric": false,
    "urls": ["www.muster-sanitaer.example.ch", "www.dritte-firma.example.ch"],
    "uid": "CHE-123.456.789",
    "phones": ["+41 41 000 00 01", "079 000 00 03"],

    "companyNameCandidates": ["Muster Sanitär AG"],
    "addresses": ["Musterstrasse 12, 6000 Luzern"],
    "companyEmails": ["info@muster-sanitaer.example.ch"],
    "legalForm": "AG",
    "departmentLine": "Abteilung Einkauf",
    "readSignals": ["signature_company_name", "legal_form", "department_line", "signature_address"],
    "counterSignals": ["third_party_mention"],
    "confidence": "high",
    "reason": "Die Signatur nennt „Muster Sanitär AG“; die Absenderdomain passt dazu."
  },
  "provenance": {
    "emailDomain":  {"origin": "mechanical", "source": "freshdesk_contact", "verification": "not_required"},
    "urls":         [{"origin": "mechanical", "source": "signature", "verification": "required"},
                     {"origin": "mechanical", "source": "quoted",    "verification": "required"}],
    "uid":          {"origin": "mechanical", "source": "signature", "verification": "required"},
    "phones":       [{"origin": "mechanical", "source": "signature", "verification": "required", "country": "CH"}],
    "companyNameCandidates": [{"origin": "read", "source": "signature", "verification": "required"}],
    "addresses":    [{"origin": "read", "source": "signature", "verification": "required"}],
    "companyEmails": [{"origin": "read", "source": "signature", "verification": "required"}],
    "legalForm":    {"origin": "read", "source": "signature", "verification": "required"},
    "departmentLine": {"origin": "read", "source": "signature", "verification": "required"}
  },
  "regions": {
    "own_text": "…the sender's own message…",
    "signature": "Anna Muster\nAbteilung Einkauf\nMuster Sanitär AG\n…",
    "quoted": "Von: Dritte Firma AG …"
  },
  "notes": []
}
```

`signals` is flat on purpose: the matcher consumes it directly, and a value's history belongs
beside it rather than inside it. `provenance` mirrors the field names — one object for a scalar,
a list aligned by index for a list — and `regions` is the text those decisions were made
against, so the next step can re-check them without fetching the ticket again.

### The three fields of a provenance entry

- **`origin`** — `mechanical` when a script found it, `read` when you did. Nothing else.
- **`source`** — where it came from: `freshdesk_contact`, `config`, `signature`, `own_text`, or
  `quoted`. A value whose source is `quoted` was written by somebody else and will be dropped;
  report it anyway rather than hiding it, so the note can say what was ignored.
- **`verification`** — `required` for anything read out of the ticket text, `not_required` for
  a value that never came from prose. Today only two are `not_required`: the sender's email
  domain, which is the Freshdesk contact record, and the generic-domain flag, which is
  configuration. **Marking these matters**: they cannot be found in the body, so a gate looking
  for them there would discard the strongest signal the skill has.

Everything else is `required` and will be proved against the raw text. That includes every
value you add: `verify_signals.py` does not care that a name looked obvious.

### The read fields

- `companyNameCandidates` — every name you actually read, best first. Empty is a valid answer.
- `addresses` — the company's postal address as it stands in the signature, street and number,
  postcode and town in one string. Not the private address of someone writing about their own
  bathroom, and never the recipient's.
- `companyEmails` — a general company mailbox in the signature (`info@`, `kontakt@`), not the
  sender's personal address, which Freshdesk already holds on the contact.
- `legalForm` — `AG`, `GmbH`, `SA`, `Sàrl`, and so on, when one is attached to a name.
- `departmentLine` — „Abteilung Einkauf", „Technischer Dienst", when the signature carries one.
- `readSignals` / `counterSignals` — the cue names from the tables below, so the trace shows what
  was actually seen.
- `confidence` is `high`, `medium` or `low`. Hesitate towards `low`: a low-confidence signal set
  can still produce a link when a mechanical signal is strong, but a name you half-read must not
  be presented as if you were sure.
- `reason` is one German sentence naming the evidence, for the note and the trace.

**Every value field is a plural list**, even where a ticket almost always carries just one:
`urls`, `phones`, `addresses`, `companyEmails`, `companyNameCandidates`. A signature can hold a
head office and a branch, a landline and a mobile. Only what is singular by nature stays scalar:
`emailDomain`, `uid`, `legalForm`, `departmentLine`.

Keep to these names exactly. `addresses` and `companyEmails` are written to the company record
when `LINKING_COMPANY_FIELD_ADDRESS` / `_EMAIL` are configured (see `matching-rules.md`), and a
value emitted under a name nobody reads is lost in silence — no error, no note, just a company
created without the address that was sitting in the ticket. That bug has shipped once already.

## Positive signals

| Cue | What it looks like |
|---|---|
| `signature_company_name` | A company name in the signature block, usually above or below the address. |
| `signature_address` | A postal address in the signature block: street and number, postcode, town. |
| `legal_form` | `AG`, `GmbH`, `SA`, `Sàrl`, `SARL`, `KG`, `OHG`, `e.K.`, `& Co.`, `S.L.`, `S.A.` attached to a name. |
| `corporate_email_domain` | A sender domain that is not on `LINKING_GENERIC_EMAIL_DOMAINS`. |
| `website_url` | A URL whose host matches or resembles the sender domain or a read name. |
| `uid_number` | A Swiss `CHE-###.###.###` (or a German `DE` VAT id) anywhere in the text. |
| `letterhead_line` | A line that reads like a letterhead: name, street, postcode and town together. |
| `on_behalf_phrase` | „im Auftrag der …", „für die Firma …", „unsere Firma …". |
| `department_line` | „Abteilung Einkauf", „Technischer Dienst" — weak alone, useful as support. |

## Counter-signals

| Cue | What it looks like |
|---|---|
| `generic_email_domain` | The sender writes from free mail; the domain says nothing about an employer. |
| `private_person_wording` | „als Privatperson", „mein Bad zu Hause", a private address with no company line. |
| `third_party_mention` | A company named as a subject rather than a sender: „die Firma X hat bei mir montiert". This is the trap — the ticket mentions a company that is **not** the requester's. |
| `forwarded_thread` | The signature belongs to a quoted, forwarded message, not to the sender. |
| `service_provider_mention` | An installer or architect named as a participant in the project, not as the requester's employer. |

A counter-signal does not cancel a positive one by itself. Report both and let the decision
weigh them: a wholesaler's clerk writing from a corporate domain about an installer's project
has both, and the right link is still the wholesaler.

## What is a company signal, and what only looks like one

A signal is a statement about **who the sender is**. Everything below fails that test even
though a company name is right there in the text, and each one has cost somebody a wrong link
in some system:

| Not a signal | What it looks like | Why it is not one |
|---|---|---|
| A company as the **subject** of the message | „die Firma Fremd Montage GmbH hat bei uns montiert" | The sender is talking *about* that firm. Naming it links the contact to their supplier. |
| A **third-party footer** | A legal disclaimer or a mail-scanner banner naming a service provider | It was added by software, not written by the sender. |
| A **quoted signature** | The block under „Von: …" or „-----Ursprüngliche Nachricht-----" | It belongs to whoever wrote that message. The extractor marks it `source: "quoted"`. |
| The **recipient's** own details | Brausewerk's address, an agent's signature on a reply | Reading it back would link every contact to Brausewerk. |
| An **installer or architect** named as a project participant | „Ausführung durch Muster Sanitär" | A participant in the work, not the sender's employer — unless the sender *is* them, which the domain usually settles. |

When one of these is present, record it in `counterSignals` rather than dropping it silently: a
note that says „die genannte Firma ist der Monteur, nicht der Absender" is worth reading, and an
empty answer with no explanation is not.

## Where to look, and where not to

Read the **first message only** — subject, body, signature. A later reply from a colleague, a
quoted thread, or a Brausewerk agent's own signature are not evidence about this contact.

When a body carries several signature blocks (a forwarded chain), the sender's own block is the
last one before the quoted part, not the first one in the text.

## Everything here is checked afterwards

Whatever you report in `companyNameCandidates`, `legalForm` and the other read signals goes
through `verify_signals.py` before it can reach a search: a value that is not in the ticket's
raw text, or that only appears inside a quoted block, is discarded. See
`references/signal-verification.md`.

That is not a reason to be careless — a discarded value costs a search that could have found
the right company — but it does mean an honest `low` is always better than a confident guess.

## When there is nothing

Emit the JSON with empty lists, `confidence: "low"` and a German `reason` saying so. An absent
signal is an answer; an invented company name is not.
