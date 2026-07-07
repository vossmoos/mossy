# Adaptive UI Channel (`/aui`)

A chat page with an **adaptive side panel**: next to the chat, skills can render widget
boxes — card teasers with detail popups and action buttons, tables, live progress/log
streams, and shared documents (e.g. PDF reports). What appears in the panel is decided
by **skills**, not by this channel: the channel only knows a small generic widget
vocabulary and renders whatever the agent's `panel_*` tools emit.

## Run & try

```bash
python main.py            # /aui is enabled with the HTTP server
python main.py --no-aui   # disable this channel
```

Open `http://127.0.0.1:8765/aui`, enter the `MOSSY_API_KEY`, chat as usual.

Optional env: `AUI_RUN_PATH=/aui/run` (POST endpoint used by the page).

## How it works

- `GET /aui` serves the page (auth-exempt, like `/ui`); `POST /aui/run` is an AG-UI SSE
  endpoint (bearer key protected, same body shape as `/agui`).
- The `/aui` agent is the regular Mossy agent **plus** the `ui-panel` toolset
  (`mossy/capabilities/ui_panel.py`): `panel_upsert_box`, `panel_append_log`,
  `panel_remove_box`, `panel_clear`.
- Panel tools emit AG-UI `STATE_DELTA` / `STATE_SNAPSHOT` events (via `ToolReturn`
  metadata). The frontend keeps panel state as `{"boxes": {<box_id>: <box>}}`, applies
  the JSON-Patch deltas, and re-renders.
- Widget buttons send a message back into the chat: either the action's `prompt`
  verbatim, or a generated `[panel action] id=<action_id> {"box": ..., "item": ...,
  "payload": ...}` message. The agent handles it like any user message.

```
skill (SKILL.md + scripts)  →  model calls panel_upsert_box(...)  →  STATE_DELTA on SSE
        ↑                                                                     ↓
  '[panel action] …' user message   ←   user clicks a widget button   ←   panel renders
```

## Widget vocabulary

A box: `panel_upsert_box(box_id, title, widget, actions?)`. Re-using a `box_id`
replaces that box in place (use this for progress updates). `box_id` must match
`[A-Za-z0-9][A-Za-z0-9_-]{0,63}`.

An **action** (usable on boxes, cards, and documents):

```json
{"id": "export_fhir", "label": "▶ Export to Medplum", "style": "primary",
 "prompt": "Export patient SIM0042 to the Medplum FHIR server.",
 "payload": {"patient_id": "SIM0042"}}
```

Prefer an explicit `prompt` — it makes the round-trip deterministic. Without it the
frontend sends `[panel action] id=export_fhir {"box": ..., "item": ..., "payload": ...}`.

### `card_grid` — entity teasers with detail popups

```json
{"type": "card_grid", "items": [
  {"id": "SIM0042", "title": "Rivera, Elena", "subtitle": "F, 58 — Cirrhosis",
   "badge": "MELD 18",
   "fields": {"MRN": "SIM0042", "Encounter": "2026-06-30"},
   "detail": "**Problems**\n- Cirrhosis (K74.60)\n- Ascites\n\n**Meds**\n- Furosemide 40mg",
   "actions": [{"id": "export_fhir", "label": "▶ Export", "style": "primary",
                "prompt": "Export patient SIM0042 to the FHIR server."}]}
]}
```

Clicking a card opens a popup with `fields` + `detail` (markdown-lite: `**bold**`,
`` `code` ``, line breaks, `/files/...` links) + the item's actions.

### `table`

```json
{"type": "table", "columns": ["Scenario", "Result"], "rows": [["Cirrhosis", "PASS"]]}
```

### `markdown`

```json
{"type": "markdown", "text": "**Summary**\n12 documents generated, 0 errors."}
```

### `progress` — long-running work (update via same `box_id`)

```json
{"type": "progress", "status": "Exporting bundle 3/8…", "percent": 37.5, "done": false}
```

### `log` — streaming lines (append with `panel_append_log(box_id, lines)`)

```json
{"type": "log", "lines": ["[10:02:11] bot: Hello, how can I help?"]}
```

### `document` — a shared file (PDF report, XML bundle, …)

Share the file first (file-sharing skill), then reference the shared path:

```json
{"type": "document", "path": "reports/chatbot-test-2026-07-07.pdf",
 "description": "Test imitation report — 14 turns, 2 failures"}
```

The panel renders Open / Download buttons (authenticated `/files/` fetch) plus any
custom actions.

## Writing skills for this channel

**The golden rule — channel idempotency.** The same skill must work on `/ui`, CLI,
Slack, and queued tasks, where `panel_*` tools **do not exist**. So:

1. Your chat answer must always be complete on its own. The panel duplicates and
   enriches; it never carries information that exists nowhere else.
2. Every panel instruction in your SKILL.md must be conditional: *"if the panel tools
   (`panel_upsert_box`, …) are available…"*. On channels without the tools the model
   simply cannot call them, and the conditional wording stops it from trying.
3. Panel actions must map to things a user could also type. `▶ Export` triggering
   "Export patient SIM0042 to the FHIR server" works in any channel; a button that
   only makes sense pixel-wise does not.

**The convention:** add an `## Adaptive panel (optional)` section to your SKILL.md.
The `/aui` agent is instructed to follow it; other channels ignore it naturally.

### Example — EHR simulation skill

```markdown
## Adaptive panel (optional)

If the panel tools (panel_upsert_box, …) are available:

- After generating or listing patients, render box `ehr-patients` ("Patients") as a
  card_grid: one card per patient — title: name; subtitle: sex, age, primary
  condition; badge: key score; fields: MRN, encounter date; detail: markdown summary
  of problems, meds, allergies; actions: one primary action
  {"id": "export_fhir", "label": "▶ Export to Medplum",
   "prompt": "Export patient <MRN> to the Medplum FHIR server."}.
- While an export runs, upsert box `ehr-export` ("FHIR export") as progress and
  update it (same box_id) until done: true.
- When you receive "Export patient <MRN>…" (typed or via button), run the export
  script and report the result in chat as usual.

Always describe the generated patients in your chat reply too.
```

### Example — chatbot-testing skill

```markdown
## Adaptive panel (optional)

If the panel tools are available:

- At test start, upsert box `test-log` ("Conversation imitation") as a log widget;
  stream turns with panel_append_log as the imitation progresses.
- After building the PDF report, share it, then upsert box `test-report`
  ("Test report") as a document widget pointing at the shared path, with action
  {"id": "rerun", "label": "Re-run failed cases",
   "prompt": "Re-run only the failed chatbot test cases."}.

In chat, always give the pass/fail summary and the /files/ link to the report.
```

## Handling `[panel action]` messages

Buttons without a `prompt` arrive as:

```
[panel action] id=export_fhir {"box": "ehr-patients", "item": "SIM0042", "payload": {...}}
```

If your skill uses payload-style actions, tell the model in SKILL.md what each action
`id` means and which script to run with the payload.

## Limitations (v1)

- Panel state lives in the browser tab (client-managed, AG-UI style). "New chat"
  clears it; a reload clears it.
- The chat client keeps only user/assistant text across runs, so the model may not
  remember earlier tool calls in long threads. Make actions self-contained: put
  everything needed into `prompt`/`payload` rather than relying on the model's memory
  of what a box contains.
- Box order = creation order; updates keep position. No per-user persistence yet.

## Testing with curl

```bash
curl -N -X POST http://127.0.0.1:8765/aui/run \
  -H "Content-Type: application/json" \
  -H "Accept: text/event-stream" \
  -H "Authorization: Bearer $MOSSY_API_KEY" \
  -d '{"threadId":"t1","runId":"r1","state":{"boxes":{}},"context":[],"tools":[],
       "forwardedProps":{},
       "messages":[{"id":"m1","role":"user","content":
         "Show a demo: render a panel box with a card_grid of two fake patients."}]}'
```

Look for `STATE_DELTA` events carrying `{"op": "add", "path": "/boxes/..."}`.
