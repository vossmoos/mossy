---
name: freshdesk
description: Use this skill to read or act on Freshdesk tickets, contacts, companies, conversations, and solution articles.
---

# Freshdesk

## When To Use This Skill

Use this skill when the user asks to look at Freshdesk tickets, read a ticket's
messages/conversation, look up a customer (contact) or company, read a solution
article (used as a reply template), or post a reply / private note / status or tag
change on a ticket.

This is the generic capability skill. Project-specific rules (which tickets to touch,
tone, templates, when to reply vs. note) belong in a separate project policy skill that
builds on top of these tools.

## Instructions

Use the `freshdesk` tools. Credentials come from `FRESHDESK_DOMAIN` and
`FRESHDESK_API_KEY`; if the tools are unavailable, Freshdesk is not configured.

Reads:

- `list_recent_tickets` — most recent tickets, newest first.
- `get_ticket` — one ticket with its requester contact embedded.
- `get_ticket_conversations` — the full thread (public replies + private notes).
- `get_contact` / `get_company` — customer and company records.
- `get_article` — a solution article in an explicit locale (`de`, `fr`, `nl`); these
  are templates, so always pass the locale you need.

Writes (only when the task explicitly asks):

- `post_public_reply` — customer-visible reply (HTML body).
- `post_private_note` — internal note (HTML body).
- `update_ticket` — change fields, e.g. `{"status": 4, "type": "Question"}`.
- `add_ticket_tags` — append tags without removing existing ones.

Gather context with reads before any write. Summarize results plainly. If a ticket,
contact, or article id is unknown, say so rather than guessing.
