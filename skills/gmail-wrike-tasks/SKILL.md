---
name: gmail-wrike-tasks
description: >-
  Wrike task notifications in Gmail turned into Jira — a task with no issue gets one with
  a model-evaluated summary, and a task that has one gets its new comments copied onto it. This is
  the documentation of the skill and the guide for running one notification by hand. Use it to list or describe a notification, and write only when the
  operator asks for that e-mail.
---

# Gmail → Wrike task → Jira

## Read this first — this file does not run the flow

Whatever runs the flow — a person in a shell, or a duty a repository adds — calls the scripts
directly, in Python, one subprocess per e-mail, and **enqueues no goal for an agent**: a skill does
not bound an agent that has other doors, and this Jira cannot delete what it creates. This
repository ships no such duty.

So this file is two things, and only two:

- the documentation of the skill — what the scripts do, the labels, the variables, the limits;
- the guide for running **one** notification by hand.

It is not an orchestrator. An agent that has loaded it may list and describe; it writes only when
the operator asked, in that message, for that e-mail.

Loading it matters anyway. Mossy only loads a skill that has a `SKILL.md` — measured before this file
existed, `gmail-wrike-tasks` was not among the skills discovered — and once
loaded, every file in `scripts/` is offered as runnable. Two of them have a command line. The other
nine are modules that `process_wrike_notification` loads by path; run on their own they do nothing.

## The write rule

- **Nothing is written without `--apply`** — not in Jira, not in the local register, not a Gmail
  label. Without it the Jira write clients are not even built.
- **Nothing written to Jira can be deleted.** This account has no delete permission; a surplus
  issue is renamed, never removed.
- `--apply` labels the e-mail itself, last. Do not also run `mark-processed` or `mark-skipped` after
  it: those subcommands are for repairing a label by hand, and they are writes to the mailbox too.

## When to use

- `List the Wrike notifications nothing has been done with yet.` — an operator, in a shell (step 1).
- `Describe the Wrike notification <gmail message id> with the gmail-wrike-tasks skill.`
- `Process the Wrike notification <gmail message id> with --apply.` — only as an explicit instruction
  for that e-mail.

## Flow — one notification, by hand

Each script prints one JSON object. What travels between the steps is the Gmail message id.

1. **List what nothing has been done with yet.** In a shell, from the repository root, run
   `.venv/bin/python skills/gmail-wrike-tasks/scripts/fetch_wrike_notifications.py list --max-results 10`.
   It reads and labels nothing, and prints
   `{ok, query_sent, processed_label, skipped_label, count, messages, detail}`. Carry each
   `messages[].id`.
   - **This step needs a shell.** `list` is a subcommand — a positional argument — and Mossy's
     script runner passes named flags only: measured in its executor, which turns every argument
     into `--key value`. An agent cannot run it through the skill. **It is not broken**: the
     Freshdesk flow's `fetch_mention_emails.py list` has the same limit, for the same reason. Run it
     in a shell and hand the ids on.
   - `count: 0` is nothing to do. `ok: false` is never an empty mailbox: `gmail_token_refused` is
     the refresh token expired (see the limits below), `no_allowed_senders` means the mailbox was
     not even queried, and `gmail_not_configured`, `gmail_unreachable`, `gmail_listing_refused`
     and `gmail_message_refused` say what failed. Stop and tell the operator.
2. **Describe one notification — this writes nothing.** Run
   `scripts/process_wrike_notification.py --message-id <id from step 1>`.
   It prints one envelope with `ok`, `stage`, `verdict` and `action: describe`. Under
   `create.would_create` is the issue it would open — the model's summary, the description, the
   label; under `copy`, the comments it would post onto the issue that exists.
   - **It calls the model** when the task has no issue yet, so every describe of such a task is paid.
   - A message saved to a local file can be given with `--message-json <file>` instead. That spares
     only the Gmail read; Wrike and Jira are still read.
   - `ok: false` stops before any write, and `reason` says where. **`ok: true` does not promise that
     step 3 will pass**: the checks that stop a create — the model answering, the Jira credentials,
     the comments being readable, the link back to Wrike — run only with `--apply`. Read
     `create.would_create` (its `analysis`, `summary` and `permalink`) before writing.
3. **Write it — only when the operator asked for this e-mail.** Run
   `scripts/process_wrike_notification.py --message-id <id from step 1> --apply`.
   In one pass it creates the issue or copies the new comments, confirms each Jira write by
   read-back, records it in the register, and labels the e-mail. What comes back:

   | `action` or `reason` | What happened | What to do |
   |---|---|---|
   | `created` | Issue created and confirmed, its link and the comments it summarised recorded, e-mail labelled processed. | Nothing. The comments read for the summary are recorded as copied and never posted: they reach Jira only through the model's summary, not as their own text. |
   | `copied` | New comments posted and confirmed, e-mail labelled processed. With `comments_certain: false`, Wrike returned no comments at all — copied nothing, labelled anyway. | Nothing. See limit 9. |
   | `skip`, with `reason` `not_a_wrike_notification` or `not_responsible` | Not a Wrike task notification, or a task with no issue that does not concern me. Labelled skipped. | Nothing. |
   | `write_stopped` | A Jira-side step stopped; `write_reason` says which. The e-mail stays unlabelled and comes back. | Read `write_reason`. After `read_back_failed` the issue exists and the next run finds it by its label. |
   | `write_stopped` with `summarised_comments_not_recorded` | The issue exists; the comments it summarised could not be recorded. | **Do not run it again before a person looks**: the next run would post those comments as duplicates. |
   | `label_failed` | Jira is done; the label did not land. The label's own `reason` is under `label`. | The next run finds the issue and labels it — unless the label does not exist in Gmail (`label_not_found`): a person creates it; until then every run stops at that label. |
   | `me_unresolved`, `responsibility_unknown`, `wrike_unreadable` | Stopped before any write: who I am, or the task, could not be read. | The Wrike token or the licence. |
   | `plan_stopped` | The decision leads to no write — for example two issues carry the task's label. | Read `plan`; a person decides. |

## Inside one pass — what `process_wrike_notification` does, not steps to run

- **Parse** — `parse_wrike_notification`. The task id comes from the `X-Wrike-Metadata` header
  (`taskApiId`), never from the body, and only from an allowed sender.
- **Decide** — `decide_wrike_action`, read-only. The local register (`wrike_jira_links`) and the Jira
  label `wrike-<taskApiId>` (`jira_label_search`) say whether the task already has a live issue.
- **The gate, before creating only** — `wrike_api`. Who I am comes from `/contacts?me=true` (one row,
  marked `me`), and the task must list my id in `responsibleIds`. It never stops a copy onto an
  issue that exists.
- **Create** — `create_jira_for_task` with `analyze_wrike_task`. The summary is evaluated by the
  model from the task and all its comments; then the Jira credentials are checked — after the model,
  so a bad Jira token has already paid for the summary when it stops; one POST carries the label;
  the issue is read back (its key, not its label); the link and the summarised comment ids are
  recorded. If the model fails, nothing is created — there is no fallback title.
- **Copy** — `copy_wrike_comments`. The anchors `[wrike-comment:<id>]` already on the issue are read
  before anything is written; only what is missing is posted, and each one is recorded.
- **Lock** — `task_lock`, taken inside create and copy, per task.
- **Label** — `fetch_wrike_notifications`, processed or skipped, confirmed by read-back.

## Gmail labels

| Label | Variable | Meaning |
|---|---|---|
| `mossy-wrike-processed` | `WRIKE_TASKS_PROCESSED_LABEL` | The task is up to date in Jira: its issue was created, or its new comments were copied. |
| `mossy-wrike-skipped` | `WRIKE_TASKS_SKIPPED_LABEL` | The e-mail was never work: not a Wrike task notification, or a task with no issue that does not concern me. |

- **The operator creates both labels by hand, in Gmail. The code never creates a label**: a missing
  one is `label_not_found`, and nothing is labelled.
- The listing excludes both, so a labelled e-mail never comes back. Skipped is only ever about what
  the e-mail is: a failure of ours leaves it unlabelled, and it comes back.
- They are not the Freshdesk flow's labels, on purpose: two flows must not read one marker.

## Scripts

| Script | Command line | Purpose |
|---|---|---|
| `scripts/fetch_wrike_notifications.py` | `list`, `mark-processed`, `mark-skipped` | Gmail: list the unprocessed notifications (headers only), and label one. Its own OAuth client. |
| `scripts/process_wrike_notification.py` | `--message-id` or `--message-json`, and `--apply` | The one entry point: one notification, end to end, one JSON out. |
| `scripts/parse_wrike_notification.py` | none | What the e-mail is: sender, task id from the header, permalink id. |
| `scripts/decide_wrike_action.py` | none | The eleven verdicts, from the register and the label search. Reads only. |
| `scripts/create_jira_for_task.py` | none | Creates the issue: model summary, credentials check, one POST with the label, read-back, records. |
| `scripts/analyze_wrike_task.py` | none | The model call that evaluates the summary from the task and all its comments. |
| `scripts/copy_wrike_comments.py` | none | Copies the comments not yet on the issue, anchored, read back before writing. |
| `scripts/wrike_api.py` | none | Wrike: three reads — the task, its comments, and who the token is. Never a write. |
| `scripts/jira_label_search.py` | none | Finds a task's issue by its label; an empty page is never taken as "no issue". |
| `scripts/wrike_jira_links.py` | none | The local register (DuckDB): links and copied comment ids. A read never creates the file. |
| `scripts/task_lock.py` | none | One lock per task, taken inside create and copy; a busy task is refused, not waited for. |

All of them are self-contained: no `import mossy`, no `python-dotenv`.

## Configuration

Every variable the scripts read, and no other. A test holds this table to the code in both
directions.

**How the variables reach a script.** No script loads `.env`. A subprocess started under Mossy inherits
Mossy's environment, where the repository's `.env` overrides the shell. Run by hand, a script
sees only the shell it was started from, so export what it needs there first. That includes
`WRIKE_TOKEN`: if it lives only in the operator's shell, Mossy has to be started from that shell for
anything it starts to read Wrike.

| Variable | Read by | Meaning |
|---|---|---|
| `GMAIL_CLIENT_ID`, `GMAIL_CLIENT_SECRET`, `GMAIL_REFRESH_TOKEN` | fetch_wrike_notifications | OAuth2 for the mailbox, all three required. The same mailbox and client as the Freshdesk flow. |
| `GMAIL_USER_ID` | fetch_wrike_notifications | Mailbox, default `me`. |
| `WRIKE_TASKS_ALLOWED_SENDERS` | fetch_wrike_notifications, parse_wrike_notification | Comma-separated senders. The listing refuses to query the mailbox when it is empty; the parser falls back to `wrike@wrike.com`. Different on purpose: the listing decides what is read. |
| `WRIKE_TASKS_PROCESSED_LABEL` | fetch_wrike_notifications | Default `mossy-wrike-processed`. |
| `WRIKE_TASKS_SKIPPED_LABEL` | fetch_wrike_notifications | Default `mossy-wrike-skipped`. |
| `WRIKE_TOKEN` | wrike_api | Wrike permanent access token. |
| `OPENAI_API_KEY` | analyze_wrike_task | The model. Without it the summary cannot be evaluated, and no issue is created. |
| `WRIKE_TASKS_ANALYSIS_MODEL` | analyze_wrike_task | Default `gpt-5.4-mini`. Its own variable, not the Freshdesk flow's. |
| `JIRA_SITE`, `JIRA_USER_EMAIL`, `JIRA_API_KEY` | jira_label_search, create_jira_for_task, copy_wrike_comments | Jira Cloud authentication. |
| `JIRA_SPACE_NAME` | jira_label_search, create_jira_for_task | The project issues are created in and searched. |
| `WRIKE_JIRA_LINKS_DB` | wrike_jira_links | The register, default `~/.local/share/mossy/wrike-jira-links.duckdb`. |
| `WRIKE_TASK_LOCK_DIR` | task_lock | Default `~/.local/share/mossy/task-locks`. |

## Before switching it on — the limits that change what you will see

**First, and above the rest: nothing but this file stops an agent from writing.** Measured by
reading this repository's core and the skills library — nothing was run:

- **Every agent loads this skill.** The worker, which runs every goal including the ones duties
  enqueue, AG-UI and MCP load every skill; the CLI chat and Slack exclude only `filesystem`. This
  core has no switch that excludes a skill, and no tool call needs approval.
- **Nothing is filtered by script.** The library can leave out a whole skill or a whole tool, never
  one script. An agent that asks for `process_wrike_notification.py` with `message-id` and
  `apply: true` gets exactly `--message-id <id> --apply` — measured on its executor — run with
  Mossy's own environment, credentials included.

So "an agent writes only when the operator asks" is an instruction, not a lock: a skill does not
bound an agent that has other doors. Until there is a lock, one agent's decision on any channel, with the credentials
in place, is enough to create a Jira issue that cannot be deleted or to label an e-mail out of the
flow for good.

1. **The Gmail refresh token expires every 7 days** while the Google Cloud app behind it stays in
   Testing, and changing that is not this repository's to do. A dead token is `gmail_token_refused`
   in the log — never an empty mailbox — and nothing is processed until it is renewed.
2. **The write path has run once for real — the create, and nothing else.** One `--apply` by hand on
   one e-mail created one issue. Copying comments onto an issue that exists and the repair branch
   have never run for real: run each by hand, on one e-mail, before anything runs this unattended.
   (The create's shape was proven before that with curl, on a test issue that stays in the project
   because it cannot be deleted.)
3. **The task id rests on one header, measured on one message.** One real notification
   read as `format=metadata` came back with `X-Wrike-Metadata` among its 25
   headers and no body, and the parser read its task id. If Wrike ever drops that header, every
   notification parses as `no_wrike_metadata_header`, and with `--apply` every one is labelled
   skipped and never comes back — so a describe (step 2) comes before the first real write.
4. **An issue that exists without the label is invisible.** A task's issue is found by the local
   register or by the label `wrike-<taskApiId>`. An issue opened by hand for a Wrike task carries
   neither, and the flow opens a second one.
5. **A describe calls the model.** Every describe of a task with no issue pays for the summary,
   every time it is run.
6. **Creating is for tasks that concern me; copying is for every issue that exists.** A task that
   does not list my id in `responsibleIds` gets no issue, and "concerns me" is not "mine": my id is in
   four of the six live tasks measured. A task that already has an issue keeps getting its comments
   after I am taken off it.
7. **The per-task lock covers this machine only.** Two machines running the flow against the same
   Jira can still duplicate an issue or a comment.
8. **Some results wait for a person.** When Jira refused the create, when a pass paid for the model
   and created nothing, or when the summarised comments could not be recorded, the envelope says
   so and the e-mail stays unlabelled. A Gmail label that does not exist is `label_not_found` until
   a person creates it.
9. **An empty comment list is labelled processed.** When Wrike returns no comments for a task that
   has an issue, nothing is copied and the e-mail is labelled anyway; the envelope carries
   `comments_certain: false`. On a Collaborator licence an empty list could in principle be a
   restricted read, and nothing here can tell. Accepted: the 29 comments of one real task were read
   with this same licence, and two of the six live tasks measured really have none.

## Notes for agents

Jira is written in English. Wrike is only ever read. Never pass `--apply`, and never run
`mark-processed` or `mark-skipped`, on your own initiative.
