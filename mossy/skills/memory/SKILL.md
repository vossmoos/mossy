---
name: memory
description: Use this skill to recall what the company already knows (customers, decisions, past outcomes) before answering or acting, and to remember new facts, decisions and outcomes afterwards. Uses the memory tools (memory_recall, memory_know, memory_remember).
---

# Company memory

## When To Use This Skill

- **Before** answering about a customer, product, project or past decision: check what is
  already known. The memory holds consolidated beliefs — one current value per fact, with
  confidence, sources and history — not raw documents.
- **After** work that produced something worth keeping: a decision and why, a fact learned,
  an outcome, a customer preference, a mistake not to repeat.

## Configuration

The tools come from an MCP server (see the `mcp` skill), usually Myco with an etchmem source
named `memory`, so they are called `memory_recall`, `memory_know`, `memory_remember`, ... With
another source name the prefix differs; connected to etchmem directly they are `recall`,
`know`, `remember` (or carry the server's `TOOL_PREFIX`). If none are available, memory is
not configured.

## Instructions

Reading:

- `memory_know(entity)` — **everything** known about one subject. Use it for "what do we know
  about Acme". Exhaustive.
- `memory_recall(query)` — beliefs by meaning, ranked. Use it when you do not know the subject
  yet. Its ranking is by similarity, not by truth: the top hit can be an old or side fact.
  **Before you state a fact from recall, confirm it with `memory_know` on its subject**
  (e.g. recall finds "Lisa Brandt — account manager for Acme": ask `memory_know("Acme Corp")`
  and answer from Acme's own `account_manager` belief).
- `memory_list_beliefs(property=…)` — every belief of one kind, e.g. all `incorrect_assumption`.
- `memory_dossier(etch_id)` — the full trail behind a belief: every claim and source.
  Rarely needed: each belief already says where it comes from (see below).
- `scope` is a topic, not a permission: pass it when your current skill names its memory topic,
  to get focused knowledge; omit it to read everything. What you may read is decided for you.

How much to read — choose `quotes` before you call `memory_know` or `memory_recall`:

- **Brief** (`quotes="ref"`): an overview, a summary, a status check, a list of what is known,
  or a subject with many beliefs. Each belief comes with its label, wording, source record
  and date, without the original text. Use this whenever a short answer is enough.
- **Full** (default, `quotes="full"`): you will quote the source, check exact wording, or
  answer a question that depends on what precisely was written. Each belief also carries an
  excerpt of the original text.
- Start brief when unsure; call again with `quotes="full"` only for the beliefs you need in
  detail. Every excerpt you pull in stays in your context for the rest of the conversation.

Using what you read:

- A belief's `current_value` is a short label; `details` holds its wording. Its `quote`
  says where it comes from: who deposited it (`actor`), the source record
  (`source_record_id`, e.g. `zendesk:ticket:123`), when (`occurred_at`) and, in full mode,
  an `excerpt` of the original text. Cite the record and date when you pass a fact on.
- Need the record's current state? Open the record itself with its source's tool
  (e.g. `zendesk_get_ticket`): memory says what was seen, the source says what is true now.
- Say how sure the memory is. A `contested` belief has sources that disagree — present both
  sides, never pick one silently. Low confidence means "reported once", not "true".
- Beliefs have dates. An old belief may be out of date; say when it was learned. When two
  beliefs disagree, the one with the later version or date wins unless they are `contested`.
- A belief whose narrative ends "No longer valid" (all its `values` inactive) was replaced:
  never present it as current.
- The past: `as_of` with `as_of_basis="event"` (default) answers what was TRUE then ("who
  managed Acme on 1 November"); `as_of_basis="ingest"` answers what memory had RECORDED by
  then ("what did we know on 1 November"). Records loaded late exist only for `event`.
- Withheld rows and `[redacted]` fields are permissions, not gaps. Do not infer them.

Writing (`memory_remember`):

- Write what is worth keeping — a decision and why, a fact learned, an outcome, a customer
  preference, a mistake not to repeat — in plain sentences that stand on their own: name the
  subject ("Acme", not "they"), the fact, and why it matters. No chit-chat, no transcripts.
- Set `occurred_at` when it happened earlier than now.
- That is all. Do not label, classify or place what you write, and do not think about where
  or how it is stored: memory decides how to file it, what is sensitive and who may read it.
  Do not store secrets or passwords.

Never call `memory_forget` unless the task is an explicit, approved erasure request — it is
irreversible and normally only a data-protection agent has it.
