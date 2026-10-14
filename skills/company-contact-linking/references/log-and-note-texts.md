# German texts — the contact log line and the private note

Both texts are written by `apply_link.py`, deterministically: the same decision produces the
same wording, with no model in the loop. That is on purpose — a log line that is phrased
differently every time cannot be scanned, and a note that improvises can claim something that
did not happen.

The audience is Brausewerk service staff. **Always German**, whatever language the ticket is in,
because both texts are internal. This is settled with the client and there is no second
translation, which is why no variable controls it.

## The contact log line

One line, appended to the contact field named by `LINKING_CONTACT_LOG_FIELD`. The wording is
the client's, to the character:

```
Durch KI am [21.08.2026 / 09:48] mit der Firma verknüpft.
```

The bracketed stamp is `DD.MM.YYYY / HH:MM` **in Europe/Zurich**, not UTC. Freshdesk returns
UTC and the people reading a contact record are in Switzerland, so a UTC stamp is an hour or
two wrong all year round and looks perfectly plausible while being so. The zone is
`LINKING_TIMEZONE`, defaulting to `Europe/Zurich`; when the machine has no tz database the line
is stamped in UTC **and says so in the notes** rather than quietly drifting.

### Appending, not writing

Read the field, add the line underneath what is there, write the whole thing back. A contact
record collects notes from people, and a skill that overwrites the field destroys them:

```
Handnotiz vom Kundendienst: Kunde bevorzugt Telefon.
Durch KI am [21.08.2026 / 09:48] mit der Firma verknüpft.
```

### Once per contact

A contact that already carries such a line does not get a second one, whatever brings the flow
past it again — a reprocessed ticket, a contact unlinked by hand and re-linked, a second ticket
from the same person. The marker is the sentence itself, not the timestamp, which changes on
every run and so cannot be compared.

### When it cannot be written

`LINKING_CONTACT_LOG_FIELD` unset means the line is not written and the run says so. The link
still stands: it is the job, and the log is the record of it. Reporting `logged: false` with
the reason is honest; refusing the link because the record could not be kept would be worse,
and silently claiming the record exists would be worse still.

## The private note

One private note per ticket, and only after a contact was actually attached to a company —
the `link` and `create` branches. The wording is the client's, to the character, with the
company name in place of the placeholder:

```
Die KI hat erkannt, dass der Kontakt zur Firma [Company Name] gehört, und den
Ticket-Ersteller entsprechend verknüpft.
```

Sent as one `<p>`; Freshdesk returns it wrapped in its own `<div>`, which is why nothing
downstream may depend on the markup. The name written is the company's name **as Freshdesk
now holds it**, not as the ticket spelled it: the reader should see the record they will find
in the directory. Where the ticket said „Muster Sanitär" and the directory holds
„Muster Sanitär AG", the note says the latter.

### Private, never a reply

`private: true` is sent explicitly rather than left to the endpoint's default. The note says an
automation read someone's signature and changed their record; it is written for the service
desk, and the sender is the one reader it must never reach. A note that posts publicly is
treated as a failed write, not a successful one — louder than posting nothing, because the
customer has already received it.

### Once per ticket

Two guards, because they fail in different circumstances:

1. **The ticket tag.** It stops the whole run long before the note, and covers the ordinary
   reprocess.
2. **The note's own opening sentence** — everything up to the company name, compared with
   accents folded and punctuation dropped, so Freshdesk's own markup and entities do not
   defeat it. This is the one that still holds when the tag was never written or an agent
   removed it, and it is why a contact unlinked by hand and re-linked collects a second link
   but not a second note.

The marker **cannot be a hidden HTML comment**. Freshdesk strips comments out of note bodies:
the marker survives the POST and is gone by the time anything reads it back. That once put two
identical notes on a live ticket before anyone understood why.

### Not written

`already_linked` and `out_of_scope` get no note: nothing happened, and a note saying so is
noise on a ticket somebody else is working. They are not tagged either — `out_of_scope`
especially, because the group configuration can change and a tag would keep the ticket out of
scope after it came into it.

## The notes for the declining outcomes — NOT APPROVED

> **None of the wording below may reach a live ticket.** It is implemented behind
> `LINKING_DECLINED_NOTE_ENABLED`, which is **off** by default, and stays off until the client
> approves the text. These sentences tell a customer's file what an automation concluded about
> them, and a note on a live ticket cannot be recalled. The switch exists so the path is real
> and testable, not so it can be turned on quietly.

The shape is deliberately minimal: a bold opening naming the outcome, then the German reason
the decision already carries. Nothing is composed here that the decision did not establish.

| Outcome | Opening line | Body |
|---|---|---|
| `no_signals` | **Keine Firmenangaben im Ticket** | the decision's `reason` |
| `no_match` | **Keine passende Firma gefunden** | the decision's `reason` |
| `ambiguous` | **Firma nicht eindeutig** | the decision's `reason`, which names the candidates |

Each opening line is also that note's idempotency marker, and the three are distinct from each
other and from the linking note's. A ticket declined today and linked tomorrow collects both,
and neither blocks the other.

### What the client still has to decide

1. **Whether these notes are wanted at all.** When the skill declines, nothing on the ticket
   currently says it looked — which is defensible for `no_signals`, and much less so for
   `ambiguous`, where a person could resolve in seconds what the skill would not guess at.
2. **Whether `ambiguous` should say more.** An earlier draft proposed a four-section note
   naming every close candidate so a person could pick one without repeating the search. That
   is more useful and more to approve.
3. **Whether a note on a ticket nobody will act on is noise.** `no_signals` is the common case
   in a busy queue, and a note on each is a note on most tickets.

## Never in either text

- Script names, decision keys (`no_match`, `link`), cue names (`signature_company_name`), field
  names, or the tag.
- A company name that was not read from the ticket or found in the directory.
- Anything about queues, models, scores or thresholds. „Mehrere mögliche Treffer" is the
  business fact; a score of 0.72 is not.
