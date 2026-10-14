---
name: gmail-freshdesk-bugs
description: >-
  Turn a Freshdesk thread we were mentioned on into a Jira bug — analyse what is being asked of
  us (not the end customer's incident), check the semantic-duplicate cache, and either update the
  existing issue or open a new one. It reads the mailbox, parses the notification, and marks
  the e-mail processed only after the issue exists.
---

# Gmail → Freshdesk → Jira bug triage

When a colleague mentions our triage agent on a Freshdesk thread, this skill reads that thread,
works out **what we are being asked to do** (the request about the quality of our triage — never
the end customer's incident), and files or updates a Jira bug for it.

The problem to solve is always the mentioned thread's request about **us**. The end customer's
incident is background only — the analysis step enforces this in its prompt.

## When to use

- `Triage the Freshdesk mentions with the gmail-freshdesk-bugs skill.`
- `Triage the Freshdesk mention on ticket <id> with the gmail-freshdesk-bugs skill.`

**Nothing needs to be supplied.** Step 1 finds the notifications itself and every later step
gets its inputs from the step before. In particular there is no conversation id to pass: the
mention is usually not a conversation at all (see step 3), so asking for one would only invite
the thread anchor note to be passed as the text to analyse.

The flow is complete end to end: it fetches from Gmail, parses, analyses, files or updates the
Jira issue, and marks the e-mail processed. A caller may also start at step 2 with a `.eml`
already on disk.

## Handoff rule (do not improvise)

Each script prints a JSON object to stdout and takes **named flags only** (no positionals). A
step never guesses its inputs: it takes the **exact named field** the previous step printed and
passes it to the next step's flag. The bug this prevents is a dropped handoff — step N produced
a value and step N+1 did not receive it, so the agent improvised. Every arrow below is explicit:
the field on the left is copied verbatim into the flag on the right. Do not summarise, re-type, or
reconstruct a value that a previous step already produced.

These scripts hand off **by field, not by file** — they write nothing to disk; the value travels
in the printed JSON. So "pass the output path" here means "pass the named output field".

## Flow

1. **Fetch the unprocessed notifications.** Run
   `scripts/fetch_mention_emails.py list --out-dir <a working directory>`.
   It searches the mailbox restricted to `GMAIL_ALLOWED_SENDERS`, **excludes anything already
   carrying either label**, and downloads each hit as a `.eml`. It prints
   `{ok, query_sent, processed_label, skipped_label, count, emails: [{message_id, eml_path}],
   detail}`.
   - **`GMAIL_ALLOWED_SENDERS` is required.** Unset or empty, this step returns `ok: false`
     with `reason: no_allowed_senders` and **does not query the mailbox at all** — stop and
     tell the operator to configure it. Empty does not mean "no restriction": without a sender
     the query would match the whole mailbox and turn unrelated personal e-mail into Jira
     issues this account cannot delete.
   - **It labels nothing.** Marking happens at the end, in step 7.
   - `count: 0` means there is nothing to do — stop, it is not an error.
   - Carry forward, per e-mail: `eml_path` and `message_id`.

   **Two labels, and they mean different things. Never use one for the other:**

   | Label | Meaning |
   |---|---|
   | `mossy-processed` | A Jira issue was created or updated for this e-mail. |
   | `mossy-skipped` | This e-mail was not processable — there is no issue and there will not be one. |

   Both stop the e-mail coming back; only one says work got done. Marking an unparseable
   notification as processed would claim an issue that does not exist, and nobody reading the
   mailbox afterwards could tell which e-mails actually produced work.
2. **Parse the notification email.** Run
   `scripts/parse_mention_email.py --eml-path <path to the .eml>` (add `--require-type any`
   only if replies are to be processed too — by default they are not).
   It reads the ids from the **headers**, not from the link — the link only ever carries
   `?thread=N`, an index that no API accepts. It prints
   `{ok, ticket_id, thread_id, thread_anchor_note_id, notification_type, mention_text,
   freshdesk_url, subject}`.
   - **Only `mention` notifications (`X-FD-Type: 31`) are processed. A `reply` (`30`) is logged
     and dropped — it does not go on to step 3.** A reply belongs to a thread that already
     produced its own mention, so acting on it files the same work a second time: measured, the
     reply on thread 2 produced a `problem` almost identical to the mention that opened that
     thread, because both draw their context from the same ticket thread. In production that is
     a duplicate Jira issue on every round of the conversation.
   - The script enforces this itself, with `--require-type` (default `mention`). A dropped
     reply comes back as `ok: false`, `reason: notification_type_skipped`, carrying its
     `notification_type`, the `required` value and the `ticket_id` — **log that line and stop;
     it is a deliberate skip, not a failure.** If the client ever wants replies processed, run
     it with `--require-type any`; nothing else changes.
   - **If `ok` is false with `notification_type_skipped`, `unknown_notification_type`, or
     `missing_header:*`, this e-mail is not for us.** Run
     `scripts/fetch_mention_emails.py mark-skipped --message-id <message_id from step 1>
     --reason <the reason>` and move on to the next e-mail. **This is not a flow error.** The
     mailbox carries notifications that will never produce an issue — replies (type 30), "New
     Thread created" (type 29), and "Note Added" messages with no `X-FD-*` headers at all — and
     without the skip label they come back on every pass forever.
   - If `ok` is false with `mention_text_not_found`, **stop and say so** — that one means
     Freshdesk changed the template. Do not skip it and do not improvise a text from the
     subject: it is a real mention whose text we failed to read, and it needs a human.
   - **`thread_anchor_note_id` is NOT a conversation id.** It is the note the thread hangs
     from, and it repeats across every notification of that thread while the text changes.
     Measured: of four thread messages, only one existed as a conversation at all. Never pass
     it as the text to analyse.
   - Carry forward, by name: `mention_text`, `ticket_id`, `thread_anchor_note_id`,
     `freshdesk_url`.
3. **Analyse the mention.** Run
   `scripts/analyze_context.py --ticket-id <ticket_id from step 2> --mention-text "<mention_text
   from step 2>" --thread-anchor-note-id <thread_anchor_note_id from step 2> --freshdesk-url
   "<freshdesk_url from step 2>"`.
   The `--mention-text` value is **exactly** step 2's `mention_text` — copied, not re-typed. It
   reads the ticket's whole thread (private notes included) as **context**, and asks the model
   what we are being asked to do about the mention. It prints
   `{ok, summary, problem, ticket_id, thread_anchor_note_id, anchor_note_in_thread,
   ticket_subject, freshdesk_url}`.
   - There is **no `--conversation-id`**, and that is deliberate: the mention is usually not a
     conversation, so there is nothing to select by id. Passing the anchor note as the text to
     analyse produces an issue about the wrong content.
   - Pass `--freshdesk-url` **from step 2**, verbatim. That url carries `?thread=N` and points
     at the thread we were mentioned in; it is returned untouched so the Jira issue links
     there. Omitting it falls back to a rebuilt ticket url, which lands on the ticket and
     leaves the reader to find the thread.
   - If `ok` is false (`reason: empty_mention_text`), **stop** — the model is never called and
     an empty mention must not become an issue with no description.
   - `anchor_note_in_thread: false` is normal, not an error: the anchor often is not among the
     ticket's conversations.
   - Carry forward, by name: `problem`, `summary`, `freshdesk_url`, `ticket_id`.
4. **Look for a duplicate.** Run
   `scripts/dedupe_cache.py find-similar --problem-text "<the `problem` field from step 3>"`.
   The `--problem-text` value is **exactly** step 3's `problem` — copied, not re-typed. It prints
   `{ok, match, score, threshold, cutoff}`, where `match` is null when nothing in the working-day
   window clears the threshold.
5. **If `match` is not null — update the existing issue.** Run
   `scripts/handle_duplicate.py --jira-key <match.jira_key from step 4> --ticket-id <ticket_id from
   step 3> --thread-anchor-note-id <thread_anchor_note_id from step 3> --freshdesk-url
   "<freshdesk_url from step 3>" --problem-text "<problem from step 3>"`.
   `--thread-anchor-note-id` is optional and goes into the Freshdesk reference as what it is —
   the note the thread hangs from, not a conversation. Omit it if step 3 did not carry one.
   It comments the new context on the existing issue, **confirms the comment by read-back**, and
   only then transitions the issue to its target status. Read its `{ok, transitioned, warnings}`;
   a comment that lands but a transition that fails is `ok: true` with a warning, not a failure.
6. **If `match` is null — open a new Jira issue and remember it.** Run
   `scripts/create_jira_issue.py --summary "<summary from step 3>" --problem "<problem from step
   3>" --ticket-id <ticket_id from step 3> --thread-anchor-note-id <thread_anchor_note_id from
   step 3> --freshdesk-url "<freshdesk_url from step 3>"`.
   `--thread-anchor-note-id` is optional and is written into the issue description as "Thread
   anchor note", never as a conversation id. Omit it if step 3 did not carry one.
   It creates the issue (assignee best-effort: an unresolved `JIRA_DEFAULT_ASSIGNEE` yields an
   unassigned issue plus a warning), puts the Freshdesk references into the description,
   **confirms the issue by read-back**, and only then stores it in the duplicate cache (so a later
   report matches it). It prints `{ok, jira_key, jira_url, assignee_set, cached, warnings}`. If the
   issue is not confirmed on read-back, `ok` is false and nothing is cached — a phantom key must
   never enter the cache, because this Jira cannot delete issues.
7. **Mark the e-mail processed — last, and only now.** Run
   `scripts/fetch_mention_emails.py mark-processed --message-id <message_id from step 1>`.
   Use `mark-processed` **only** when an issue exists; an e-mail that never got that far is
   marked with `mark-skipped` in step 2 instead.
   It resolves the label by name, adds it, and **confirms it by read-back** before reporting
   success; an unconfirmed label is `ok: false` with `reason: label_not_confirmed`.
   - **Only run this once the Jira issue exists** — step 5 or step 6 returned `ok: true`. The
     label is what stops the next run from picking the e-mail up again, so setting it before
     the work is finished loses the work silently. Same rule as the read-back gates on the Jira
     side: a marker must never outrun what it describes.
   - If it comes back `ok: false`, say so: the issue was filed but the e-mail will be seen
     again, and a human should know that before it produces a duplicate.

## Scripts

| Script | Purpose |
|---|---|
| `scripts/fetch_mention_emails.py` | Find notifications nothing has been done with yet and download them as `.eml`, and — separately — mark one processed (an issue exists) or skipped (it never could). Its own OAuth client: the script runtime has no mossy, so the core's Gmail capability is not reachable from here. |
| `scripts/parse_mention_email.py` | Read one Freshdesk thread notification `.eml` into its ids and the mention text. The ids come from `X-FD-*` headers; the text from the HTML body, because a thread message is usually not a conversation. Skips replies by default (`--require-type`). |
| `scripts/analyze_context.py` | Take the mention text, read the ticket's whole thread as context, and produce a one-line `summary` and a multi-paragraph `problem` describing what we are asked to do. No conversation is selected. |
| `scripts/dedupe_cache.py` | Semantic-duplicate cache in DuckDB. `find-similar` returns the best in-window match by cosine similarity; `remember` stores a bug's embedding. |
| `scripts/handle_duplicate.py` | Comment the new context on an existing Jira issue, confirm it by read-back, then transition — the read-back gates the transition. |
| `scripts/create_jira_issue.py` | Create a new Jira issue (assignee best-effort, Freshdesk references in the description), confirm it by read-back, then remember it in the cache — the read-back gates the caching. Reuses `dedupe_cache.py`. |

All of them are **self-contained**: no `import mossy`, no `python-dotenv`. The skill script runtime
does not have mossy on its path, so every step that produces data is a script in this folder — no
step delegates to a core capability.

## Configuration

| Variable | Used by | Meaning |
|---|---|---|
| `GMAIL_CLIENT_ID`, `GMAIL_CLIENT_SECRET`, `GMAIL_REFRESH_TOKEN` | fetch | OAuth2 for the mailbox. |
| `GMAIL_USER_ID` | fetch | Mailbox, default `me`. |
| `GMAIL_ALLOWED_SENDERS` | fetch | Senders the listing is restricted to. |
| `FRESHDESK_BUGS_PROCESSED_LABEL` | fetch | Label for e-mails that produced an issue, default `mossy-processed`. |
| `FRESHDESK_BUGS_SKIPPED_LABEL` | fetch | Label for e-mails that were not processable, default `mossy-skipped`. |
| `OPENAI_API_KEY` | analyze, dedupe | Model + embeddings API key. |
| `ANALYSIS_MODEL` | analyze | Chat model, default `gpt-5.4-mini`. |
| `FRESHDESK_DOMAIN`, `FRESHDESK_API_KEY` | analyze | Freshdesk reads (ticket + conversations). |
| `DEDUPE_DB_PATH` | dedupe | DuckDB file, default `~/.local/share/mossy/dedupe.duckdb`. |
| `DEDUPE_SIMILARITY_THRESHOLD` | dedupe | Cosine threshold, default `0.85`. |
| `DEDUPE_LOOKBACK_WORKING_DAYS` | dedupe | Window in working days (weekends skipped), default `3`. |
| `DEDUPE_EMBEDDING_MODEL` | dedupe | Embedding model, default `text-embedding-3-small`. |
| `JIRA_SITE`, `JIRA_SPACE_NAME`, `JIRA_USER_EMAIL`, `JIRA_API_KEY` | handle_duplicate, create_jira_issue | Jira Cloud auth + project. |
| `DUPLICATE_TARGET_STATUS` | handle_duplicate | Status to move a duplicate to, default `To Do`; a project whose statuses have other names sets its own. |
| `JIRA_DEFAULT_ASSIGNEE` | create_jira_issue | Best-effort assignee for a new issue. No default: unset, the issue is created unassigned; a value that does not resolve also leaves it unassigned. |

## Notes for agents

Jira is internal and written in English; the analysis output is English. Freshdesk writes are not
part of this skill — it only reads Freshdesk. Never turn the end customer's incident into the Jira
issue; the problem is what the mentioned thread asks of us.
