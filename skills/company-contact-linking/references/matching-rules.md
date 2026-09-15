# Matching — from a signal to a company, and when to link

`match_company.py` searches the Freshdesk company directory and returns candidates. The
decision logic in `link_decision.py` applies the thresholds to them. Neither the agent nor the
matcher decides on its own: the matcher proposes with a score and the evidence behind it, and
the thresholds do the rest.

**Deciding is not a step of the flow.** `apply_link.py` takes the matcher's output and decides
for itself before writing — there is no separate call, on purpose, because a decision reached
without a write was a place the flow stopped. `link_decision.py` is still a script and still
runs on its own if you want to see a decision without making one happen; everything below
describes what it does, wherever it is called from.

Candidates are built from **verified** signals only. A company name that failed
`verify_signals.py` never reaches a search, so it can never produce a candidate — see
`references/signal-verification.md`.

## How a candidate is found

Search paths, strongest first:

1. **Company domain.** Freshdesk companies carry a `domains` list. A sender domain that appears
   there is the strongest evidence available — it is Freshdesk's own statement about who
   belongs to whom.
2. **Exact name.** A read company name equal to a company's name after normalising case,
   punctuation, legal form and whitespace (`Muster Sanitär AG` = `muster sanitaer ag`).
3. **Name contains / is contained.** `Muster Sanitär` against `Muster Sanitär AG`.
4. **Custom field.** A UID/CHE number stored on the company record, when the account keeps one.

Every candidate carries the signal that produced it, so a decision can be explained in German
without inventing a rationale.

## Normalisation before comparing

- lowercase; strip accents (`ä` → `ae`, `ü` → `ue`, `ö` → `oe`, `é` → `e`);
- collapse whitespace and punctuation;
- drop the legal form (`ag`, `gmbh`, `sa`, `sarl`, `kg`, `ohg`, `ek`) when comparing names, but
  keep it in what is displayed;
- drop a leading `die`/`der`/`la`/`le`.

Two different companies can normalise to the same string (`Muster AG` and `Muster GmbH` both
become `muster`). That is not a match; it is an ambiguity, and it belongs in the decision.

## The decisions

| Decision | When | What is written |
|---|---|---|
| `already_linked` | The contact has a company. | Nothing. |
| `out_of_scope` | The ticket's group is not in `LINKING_GROUP_IDS`, **including when that variable is empty** — no configured scope means no work. | Nothing. |
| `link` | Exactly one candidate clears the threshold. | The link, the log line, the note, the tag. |
| `ambiguous` | Two or more candidates clear the threshold. | Tagged, no link. Never a tie-break. |
| `no_match` | Evidence was found, but no candidate clears the threshold and none can be created. | Tagged, no link. |
| `no_signals` | Nothing survived verification that could name a company. | Tagged, ignored. Nothing is created. |

`ambiguous`, `no_match` and `no_signals` all leave the contact untouched. Only `link` and
`create` write to it.

`no_signals` is kept apart from `no_match` deliberately. One says the ticket contains nothing
that names a company — the client's instruction there is to ignore it outright — and the other
says it does name one and the directory has no answer. They read the same from a distance and
call for opposite things: the first is finished forever, the second becomes linkable the moment
somebody adds that company. A verified phone number or address does not lift a ticket out of
`no_signals`: neither identifies a company on its own, though the reason names them so the
outcome does not read as an empty ticket when it was not.

### Each of them ends explicitly

None of these paths ends in silence. Each returns its decision, its German reason, the tag it
wrote and whether the tag was confirmed. "The skill looked and decided to do nothing" and "the
skill never ran on this ticket" are different facts, and a run that reported nothing would make
them indistinguishable — which is how missing coverage hides.

The decision reaching `apply_link.py` is re-derived there before anything is written: the
contact's company is read again, the signals are verified again against the raw text, and the
match and decision are recomputed. A decision that no longer holds — because the contact was
linked in the meantime, because a value fails verification on the second pass, or because it
arrived without the evidence behind it — is refused rather than applied.

## The scoring scale

A candidate's score is the value of its **strongest** piece of evidence, not a sum: three weak
name matches do not add up to a domain match, and adding them would let noise cross a threshold.
Every other hit is still reported alongside, because the note has to name the evidence.

| Evidence | Score | Variable | Why that value |
|---|---|---|---|
| Company domain matches the sender's domain | **1.0** | `LINKING_SCORE_DOMAIN` | Freshdesk's own statement about who belongs to whom, and it is verifiable rather than read. |
| UID / VAT number matches a company field | **1.0** | `LINKING_SCORE_UID` | A registration number identifies exactly one company; there is no near-miss. |
| Company name equal after normalisation | **0.9** | `LINKING_SCORE_NAME_EXACT` | Strong, but a name is read from prose and two firms can share one. |
| One name contains the other | **0.6** | `LINKING_SCORE_NAME_CONTAINS` | Real signal, weak evidence: `Muster` sits inside `Muster AG` and inside `Mustermann GmbH`. |

**The client gave the threshold (0.9), not this scale.** The values above are ours and are meant
to be reviewed: they are what make a domain enough on its own, an exact name just enough, and a
partial name never enough. Each is an environment variable, so changing one's mind costs a
setting rather than a release.

What the scale means against a threshold of 0.9:

- a domain match links, with no name needed;
- an exact name match links, from free mail as much as from a corporate domain;
- a partial name never links on its own — it can only ever appear in a note.

## What the client has settled

- **Threshold: 0.9** (`LINKING_MIN_MATCH_SCORE`, now the default rather than an open question).
- **Two candidates at or above the threshold: ambiguous, and nothing is linked.** Not "take the
  higher one" — when two companies both clear the bar, the evidence does not distinguish them,
  and a link chosen by a tie-break would be indistinguishable from a correct one afterwards.
- **A missing company may be created** (`LINKING_ALLOW_COMPANY_CREATION`, now `true`) with the
  name, plus address, phone and email when the ticket carries them. The Freshdesk field names
  are environment variables — `LINKING_COMPANY_FIELD_NAME`, `_ADDRESS`, `_PHONE`, `_EMAIL` —
  because only the name is a first-class Freshdesk field and the rest live wherever an account
  keeps them. A field left empty in the configuration is simply not written.
- **The contact log field stays configurable** (`LINKING_CONTACT_LOG_FIELD`) and has no default.
  Any field is acceptable; naming one is not optional, because appending text to a guessed field
  corrupts data nobody is watching.

The duplicate risk that made creation an open question has not disappeared — `Muster AG`,
`Muster Sanitär AG` and `Muster Sanitaer AG` can still end up as three records. What changed is
who carries it: creation now happens, so the matcher's normalisation is the thing standing
between the directory and three near-identical entries. Keep it in mind when touching
`name_key`.
