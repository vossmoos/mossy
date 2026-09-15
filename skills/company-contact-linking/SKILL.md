---
name: company-contact-linking
description: >-
  When a Freshdesk ticket arrives from a contact with no company, look for company signals in
  the ticket body and signature, match them against the Freshdesk company directory, link the
  contact, append a German log line to a contact field, and post a German private note on the
  ticket. A contact that already has a company is left alone.
---

# Company / Contact Linking

Freshdesk contacts often arrive unlinked: a person writes in, a contact is created, and nobody
attaches it to the company it belongs to. Reporting, routing and wholesaler detection all
degrade from that point on. This skill closes the gap for one ticket at a time.

It is **self-contained**: everything it needs lives under this folder, and no script imports
anything from outside it. Copy the folder into another Mossy repository and it runs there.

## Configuration

Everything is an environment variable with a default. Nothing — group ids, field names, company
names, domain lists — is written into the code.

| Variable | Default | Meaning |
|---|---|---|
| `FRESHDESK_DOMAIN` | — | Freshdesk host, e.g. `example.freshdesk.com`. Required. |
| `FRESHDESK_API_KEY` | — | Basic-auth value sent verbatim as `Authorization: Basic <value>` (base64 of `<api_key>:X`). Required. |
| `LINKING_ENABLED` | `false` | Master switch. The skill ships inert. |
| `LINKING_GROUP_IDS` | *(empty)* | Comma-separated Freshdesk group ids to act on. **Empty means no group at all**: with nothing configured the skill does nothing, so a deployment that forgets to set it cannot start writing to contacts by accident. |
| `LINKING_CONTACT_LOG_FIELD` | *(none — must be set)* | Contact field the German log line is appended to. Any field is acceptable; the skill refuses to write rather than guess one, because appending to the wrong field corrupts data nobody is watching. |
| `LINKING_GENERIC_EMAIL_DOMAINS` | `gmail.com,gmx.ch,gmx.de,gmx.net,hotmail.com,outlook.com,bluewin.ch,icloud.com,yahoo.com,web.de` | Free-mail domains that say nothing about an employer. Override per market. |
| `LINKING_MIN_MATCH_SCORE` | `0.9` | Score a candidate needs before it is linked. Confirmed with the client. **Two candidates at or above it make the case ambiguous and nothing is linked.** The scale behind the scores is in `references/matching-rules.md` and is ours to review. |
| `LINKING_ALLOW_COMPANY_CREATION` | `true` | Create the company when the directory has no match, with the name plus whatever else the ticket carries. |
| `LINKING_COMPANY_FIELD_NAME` | `name` | Freshdesk field holding the company name. |
| `LINKING_COMPANY_FIELD_ADDRESS`, `LINKING_COMPANY_FIELD_PHONE`, `LINKING_COMPANY_FIELD_EMAIL` | *(empty)* | Custom fields holding address, phone and email on a company. Only the name is a first-class Freshdesk field; the rest live wherever the account keeps them. An empty value means the account has no such field and that detail is not written. |

**Custom field names carry no `cf_` prefix.** Confirmed against a live account: contact and
company custom fields are exposed and written under their bare name — `custom_fields:
{"mossy_phone": …}` — and a `cf_`-prefixed name is rejected with HTTP 400,
`Unexpected/invalid field in request`. The `cf_` form belongs to ticket fields, which this
skill does not touch. `GET /api/v2/company_fields` and `GET /api/v2/contact_fields` list the
names an account actually has.
| `LINKING_PROCESSED_TAG` | `mossy-company-linked` | Durable marker on the ticket once a contact has been linked. |
| `LINKING_TAG_NO_SIGNALS`, `LINKING_TAG_NO_MATCH`, `LINKING_TAG_AMBIGUOUS` | `mossy-company-no-signals`, `mossy-company-no-match`, `mossy-company-ambiguous` | Markers for the outcomes that write nothing. One per outcome rather than a single "seen" tag: the tag is what stops the duty looking at a ticket again, so it decides what may still be revisited, and that differs — a ticket naming no company never becomes linkable, while one whose company is simply not in the directory becomes linkable the moment somebody adds it. All four count as processed. |
| `LINKING_RETRY_WINDOW_SECONDS` | `300` | How long a queued check's dedupe key is remembered. A ticket whose attempt ended before the writer ran is queued again in the next window. It is not what stops a finished ticket coming back — the tags above are, and they are durable. |
| `LINKING_DUTY_INTERVAL_SECONDS` | `60` | For a duty that sweeps the configured groups: how often it sweeps. Not read by these scripts, and no duty in this repository reads it. |
| `LINKING_DUTY_MAX_TICKETS` | `10` | For the same duty: most tickets one sweep will look at. Not read by these scripts either. |
| `LINKING_DECLINED_NOTE_ENABLED` | `false` | May a private note be posted on a ticket the skill declined to act on? **Off, and it stays off** until the client approves the German wording, which is marked as not approved in `references/log-and-note-texts.md`. A note on a live ticket cannot be recalled. |
| `LINKING_LEDGER_DIR` | *(the skill's `logs/`)* | Where the invocation ledger is written. Set by the test suite to a temporary directory so a test run cannot append to the deployment's record of what really executed. Not something a deployment needs to set. |
| `LINKING_MAX_CANDIDATES` | `5` | How many company candidates a search may return for consideration. |
| `LINKING_TIMEZONE` | `Europe/Zurich` | Zone the contact log line is stamped in. Freshdesk speaks UTC; the people reading the log do not. |
| `LINKING_DIRECTORY_MAX_PAGES` / `LINKING_DIRECTORY_PAGE_SIZE` | `10` / `100` | How much of the company directory a listing sweep reads. A sweep that stops early says so in its notes. |
| `LINKING_SCORE_DOMAIN`, `LINKING_SCORE_NAME_EXACT`, `LINKING_SCORE_NAME_CONTAINS`, `LINKING_SCORE_UID` | `1.0`, `0.9`, `0.6`, `1.0` | What each kind of evidence is worth. Weights, not thresholds — the bar a score has to clear is `LINKING_MIN_MATCH_SCORE`. |
| `LINKING_SIGNATURE_MARKERS` | German, English, French and Italian closings | Lines that announce a signature block. Per market. |
| `LINKING_QUOTE_MARKERS` | `Von:`, `From:`, `Gesendet:`, `-----Ursprüngliche Nachricht-----`, … | Lines that open a quoted or forwarded block. Everything after one belongs to another message. |

The defaults are for the Swiss market — `Europe/Zurich` and a Swiss/German free-mail list; change them with `LINKING_TIMEZONE` and `LINKING_GENERIC_EMAIL_DOMAINS`.

A required variable that is unset is an error, never a default: `LINKING_CONTACT_LOG_FIELD`
has no safe fallback, and writing a log line into a guessed field would corrupt contact data.

The note and the log line are **always German** — confirmed with the client, and there is no
second translation. There is deliberately no variable for it: a setting that changes nothing
is an invitation to change it.

## When to use

- **Queued:** a task like `Link the company for Freshdesk ticket <id> using the
  company-contact-linking skill.`
- **Manual:** the same scripts, run with `--ticket-id`, for one ticket.

Do not use it to change a link that already exists. This skill only fills a gap.

## What it must not do (invariants)

- **Never re-link a contact that already has a company.** An existing link is somebody's
  decision; step 2 stops there.
- **Never invent a link.** A candidate that does not clear the threshold, or two that both
  do, means no link — not the better-looking one. A wrong link is worse than no link, because
  everything downstream trusts it and nothing afterwards can tell it from a correct one.
- **A company may be created, but only from what the ticket says.** With
  `LINKING_ALLOW_COMPANY_CREATION` on, a missing company is created from the read signals: the
  name, and address, phone and email when they are there. Nothing is filled in from elsewhere,
  and the same normalisation that matches names is what keeps the directory from collecting
  three spellings of one company.
- **The writer does not trust the decision it is handed.** `apply_link.py` re-derives the
  decision from the ticket before writing: it re-reads the contact's current company, re-runs
  the signal verification against the raw text, re-runs the match and the decision, and writes
  only if that agrees with what it was given. A decision that arrives without evidence behind
  it, or that no longer holds, is refused. The reason is not distrust of the model in the
  abstract: whatever assembles that argument can pass a stale, half-filled or hand-edited
  decision, and by the time it reaches the writer the contact record is one API call away.
- **Never overwrite the contact log field.** The German line is *appended*; what is already
  there stays.
- **A ticket the skill declines is tagged and reported, never left silent.** `no_signals`,
  `no_match` and `ambiguous` each write their own tag and return an explicit outcome with a
  German reason. "Decided to do nothing" and "never ran" must be distinguishable from outside.
- **Freshdesk writes are the contact link, the contact log field, and one private note.** No
  public reply, ever — the requester is not the audience here.
- **Report what actually happened.** A link is only real once Freshdesk shows it on a read-back.
- **The read-back is the only source of truth about what was written, and its JSON travels
  verbatim.** Pass what `apply_link.py` returned to `record_task_result` exactly as it came:
  do not rebuild it, do not merge in what you expected, do not fill a field it left out. You
  may never state that a contact was linked, a log line appended or a note posted unless that
  claim is in the read-back. A report assembled by hand can say "linked" while Freshdesk shows
  no link — the same way a hand-edited decision can reach the writer — and everything
  downstream reads the report, not the ticket.

## Flow

Run scripts with the skill tool:
`run_skill_script(skill_name="company-contact-linking", script_name="scripts/<script>.py", args={...})`.

**Argument keys carry no dashes.** The tool builds each flag as `--` plus the key it is given,
so `args={"ticket-id": 51}` becomes `--ticket-id 51`; writing the key as `"--ticket-id"`
produces `----ticket-id` and the run fails. Below, flags are shown as they would appear on a
command line — drop the leading dashes when filling in `args`. Give file paths as absolute
paths: scripts run from the script directory, not the repo root.

1. **Read the ticket.** `get_ticket_intake.py --ticket-id <id>` → subject, plain-text body, the
   signature block, the requester contact and its current `company_id`, and the ticket's group.
2. **Stop if it is already linked.** `check_contact_link.py --ticket-id <id>` → `{linked,
   company_id, company_name, in_scope}`. When `linked` is true, or the ticket's group is outside
   `LINKING_GROUP_IDS`, the run ends here: report that and stop. Nothing is written.
3. **Find the signals.** `extract_company_signals.py --ticket-id <id>` returns what can be read
   mechanically — the sender's email domain (minus the free-mail list), URLs, a Swiss UID/CHE
   number, phones with a country hint — each with the region of the text it came from. It
   verifies nothing. Then read `references/detection-rules.md` and fill the read fields on the
   same object: the company name in the signature, a legal form, a department line, addresses,
   a company mailbox, and the counter-signals. Keep the provenance convention: a value you add
   is `origin: "read"` and `verification: "required"`.

   **This step is yours and nothing else does it.** The script matches patterns; it never reads
   prose, so every company *name* in the flow comes from you reading the signature. Skipping it
   does not fail — it produces a run that can never link by name and can never create a
   company, and reports `no_match` on tickets whose signature names the company in full.

   When you have read it, set `readSignalsAttempted: true` on the signals object. The next step
   refuses a payload without it. Set it even when the ticket has no signature and you found
   nothing — an empty result you looked for is an answer, and the flag is what tells it apart
   from a step nobody took.
4. **Verify the signals before searching.** `verify_signals.py --ticket-id <id> --signals
   '<json>'` — hand it the whole object from step 3. It also takes `--regions '<json>'` and
   `--provenance '<json>'` if you have them as separate blocks, so it
   verifies against the text the values were read from. It returns `{verified, discarded[],
   unverified{}, signals}`. A value that cannot be found literally is dropped as `not_in_text`;
   one found only inside a quoted or forwarded block is dropped as `wrong_region`, because it
   belongs to somebody else's signature. `confidence`, `counterSignals`, `readSignals` and
   `reason` come back under `unverified`: they are your judgement, not claims about the text,
   and nothing downstream may treat them as evidence. **Search with `verified` only.** See
   `references/signal-verification.md`.
5. **Look for the company.** `match_company.py --ticket-id <id> --signals '<verified json>'`
   searches the Freshdesk company directory and returns candidates, each with the signal that
   produced it and a score. It does not decide.
6. **Apply — one call, and it decides for itself.**
   `apply_link.py --ticket-id <id> --signals '<the whole match_company output, with the
   verified signals in it>'` is the only writer, and the only step left. Hand it what you have
   and it does the rest: it verifies the signals against the ticket again, searches the
   directory again, works out the decision, and then links, creates, logs, notes and tags —
   reading everything back before it reports.

   **There is no separate "decide" step, on purpose.** There used to be, and it was a place to
   stop: an agent that had reached a decision writing nothing saw no reason to call the writer,
   so the ticket was never tagged and came back on every sweep of the duty for the rest of its
   life. That happened on five tickets in one run. The step is gone rather than better
   explained, because three rounds of better explaining moved the agent three steps further
   along and never to the end.

   `link_decision.py` is still there and still runs on its own if you want to see a decision
   without making one happen. It is not part of this flow. It never was a source of truth
   either — the writer has always recomputed everything from the ticket and refused a decision
   that no longer held, so nothing is lost by not calling it.

   What comes back carries the whole trace: `decision`, the German `reason`, the `evidence` it
   rested on, the `candidates` and the `threshold`, plus what was written and confirmed.

7. **Report.** Summarise in a sentence or two which company was linked (or why none was), and
   whether the note was posted — every claim taken from the read-back, none from memory of what
   the earlier steps intended. Then call `record_task_result` with the JSON `apply_link.py`
   returned, passed through unchanged.

Steps 3 to 7 are skipped entirely when step 2 says the contact is already linked.

## Scripts

| Script | Returns |
|---|---|
| `get_ticket_intake.py` | Subject, body text, signature block, requester contact, group id. |
| `check_contact_link.py` | `{linked, company_id, company_name, in_scope}` — the stop condition. |
| `extract_company_signals.py` | Mechanical signals — email domain, URLs, UID/CHE number, phones — each with its region and whether it needs verifying. Deterministic, no model. |
| `verify_signals.py` | Proves every value against the ticket's text and its region: drops what is not there (`not_in_text`) and what belongs to a quoted message (`wrong_region`). Carries the reader's judgement separately under `unverified`. |
| `match_company.py` | Candidate companies from the Freshdesk directory, each with a score and the signal behind it, plus how many clear the threshold. |
| `link_decision.py` | One decision plus a German reason. Deterministic; owns the thresholds. |
| `apply_link.py` | The single write path: re-derives the decision, then links (creating the company when needed), appends the German log line to the contact, tags the ticket, and reads it all back. |
| `plan_company_check.py` | The pre-check: which tickets still need a company check, and why the others do not. Read-only. |

The scripts folder is empty for now: this is the scaffold, and the open questions in
`references/matching-rules.md` decide part of what `link_decision.py` and `match_company.py`
have to do.

## Being queued from a duty

`queue_hook.py` at the root of this folder is the whole integration surface. A duty in any
repository calls one function with the ticket ids it already has:

```python
plan = company_linking.plan_company_checks(ticket_ids, already_queued=keys_used_this_run)
requests += [EnqueueRequest(kind="goal", **r) for r in plan["requests"]]
```

Load it by file path — `skills/` is not a package and this folder's name has a hyphen — with
`importlib.util.spec_from_file_location`, not `sys.path.insert`: this skill's private modules
are called `_config`, `_audit` and so on, and putting them on the path would shadow whatever a
host repository happens to call the same.

Three things the caller does not have to know:

- **The pre-check runs inside that call**, which means inside the caller's `evaluate()`. It
  reads Freshdesk to see whether the contact already has a company, so it can never live in a
  duty's `check()` — the sentinel calls that once a second.
- **A linked contact produces no task.** So does a ticket outside `LINKING_GROUP_IDS`, one
  already carrying the processed tag, and one whose contact cannot be read. Each comes back
  under `skipped` with a reason, because "no task" and "never checked" look identical
  otherwise.
- **It never raises.** A failure returns an empty request list plus a note; the calling duty
  logs it and gets on with its own work.

**Dedupe is per ticket and window** (`company_check:ticket:<id>:<window>`, the window being
`LINKING_RETRY_WINDOW_SECONDS` — 300s by default). `already_queued` lets a caller pass the keys
it has used so far in the same pass.

The window is the point. A key that never changes is remembered for the life of the process, so
a ticket queued once could never be queued again: if the agent's turn ended before the writer
ran, that ticket was dead until a restart. Measured through the duty — four tickets each got
one attempt, each ended somewhere different, and twenty-seven sweeps later none had been
retried. With the window, three of five tickets in the validation completed on a second attempt
that would otherwise never have happened.

**This layer is not what stops a finished ticket coming back.** That is the outcome tag in
Freshdesk, which is durable and survives restarts; the pre-check reads every one of them. The
window only stops the same ticket being queued twice inside one window.

### The three lines a duty adds

```python
# once, near the top: the hook is loaded by path, not by sys.path.insert
_HOOK = Path(__file__).resolve().parents[1] / "skills" / "company-contact-linking" / "queue_hook.py"
_spec = importlib.util.spec_from_file_location("company_linking_hook", _HOOK)
company_linking = importlib.util.module_from_spec(_spec); _spec.loader.exec_module(company_linking)

# inside evaluate(), after the duty has built its own requests:
plan = company_linking.plan_company_checks(ticket_ids, already_queued=keys_used_this_run)
requests += [EnqueueRequest(kind="goal", **r) for r in plan["requests"]]
```

Wrap the second part in a `try`/`except` and log the failure: this errand is secondary, and a
duty must never lose its own work because a side check went wrong.

**No duty in this repository calls the hook yet.** A duty that wants company checks adds the
lines above — a hook call and its `try`/`except`, nothing else, since the pre-check and the
dedupe are already on this side of the line.

## References (read on demand, not upfront)

- `references/detection-rules.md` — what counts as a company signal in a ticket body and a
  signature, what does not, and the signals JSON.
- `references/signal-verification.md` — how a supplied value is proved against the ticket, why
  "present in the text" is not enough on its own, and what happens to a value that fails.
- `references/matching-rules.md` — how a signal becomes a candidate, how candidates are scored
  and compared, and the three questions still open with the client.
- `references/log-and-note-texts.md` — the exact German wording of the contact log line and of
  the private note, which branches get a note, and why the idempotency marker cannot be a
  hidden HTML comment.

## Running the tests

Offline, no Freshdesk account and no model. From `tests/`:

```
python -m pytest -q                                   # the suite
python -m coverage run --source=../scripts -m pytest  # measured
python -m coverage report
python mutation_audit.py                              # do the tests bite?
```

`mutation_audit.py` breaks one guarantee at a time and checks the suite notices. A passing
suite proves the tests run; it does not prove they would fail if the code were wrong, and in
this project four tests passed for the wrong reason before anyone noticed. **When you add a
guarantee — a refusal, a read-back, an idempotency check — add the mutation that removes it.**
If the suite stays green without it, the guarantee is decorative.

## Notes for agents

The private note is read by service staff, not developers: business wording, German, no script
names, no decision keys, no field names. You never write it yourself — `apply_link.py` does,
in the client's fixed wording, and it posts once per ticket whatever brings the flow past it
again.

Notes for the declining outcomes are **not approved by the client** and are off behind
`LINKING_DECLINED_NOTE_ENABLED`. Do not turn that on, and do not write such a note by another
route: a note on a live ticket cannot be recalled.

When you report back — in the note, in the task result, in chat — carry the reason the decision
came with. "Linked to Muster AG because the signature names it and the email domain matches"
is useful; "linked" on its own is not.
