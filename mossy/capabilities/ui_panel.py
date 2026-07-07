"""Adaptive UI panel capability — skill-agnostic widget boxes for the /aui channel.

The tools here do not render anything themselves. They validate a small, generic
widget vocabulary and emit AG-UI ``STATE_SNAPSHOT`` / ``STATE_DELTA`` events via
``ToolReturn`` metadata (pydantic-ai forwards any AG-UI ``BaseEvent`` found in tool
return metadata to the SSE stream). The /aui frontend keeps panel state as
``{"boxes": {<box_id>: <box>}}`` and re-renders on every event.

Attach this toolset ONLY to the /aui channel agent. In every other channel the
tools simply do not exist, so skills that reference them conditionally ("if panel
tools are available…") degrade gracefully to plain chat. See
``mossy/channels/aui/README.md`` for the skill-authoring contract.
"""

from __future__ import annotations

import re
from typing import Annotated, Any, Literal, Union

from ag_ui.core import EventType, StateDeltaEvent, StateSnapshotEvent
from pydantic import BaseModel, Field, ValidationError
from pydantic_ai import ModelRetry
from pydantic_ai.capabilities.toolset import Toolset
from pydantic_ai.messages import ToolReturn
from pydantic_ai.toolsets import FunctionToolset

_BOX_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


# ── Widget vocabulary ─────────────────────────────────────────────────────────


class PanelAction(BaseModel):
    """A button. Clicking it sends a message back into the conversation."""

    id: str
    label: str
    # Exact message sent to the agent on click. If omitted, the frontend sends
    # `[panel action] id=<id> box=<box_id> item=<item_id> payload=<json>`.
    prompt: str | None = None
    style: Literal["default", "primary", "danger"] = "default"
    payload: dict[str, Any] | None = None


class CardItem(BaseModel):
    id: str
    title: str
    subtitle: str | None = None
    badge: str | None = None
    # Small key→value facts shown on the card face.
    fields: dict[str, str] | None = None
    # Markdown shown in a popup when the card is clicked.
    detail: str | None = None
    actions: list[PanelAction] = Field(default_factory=list)


class CardGridWidget(BaseModel):
    type: Literal["card_grid"]
    items: list[CardItem]


class TableWidget(BaseModel):
    type: Literal["table"]
    columns: list[str]
    rows: list[list[str]]


class MarkdownWidget(BaseModel):
    type: Literal["markdown"]
    text: str


class ProgressWidget(BaseModel):
    type: Literal["progress"]
    status: str
    percent: float | None = Field(default=None, ge=0, le=100)
    done: bool = False


class LogWidget(BaseModel):
    type: Literal["log"]
    lines: list[str] = Field(default_factory=list)


class DocumentWidget(BaseModel):
    type: Literal["document"]
    # Path of a file already shared via the file-sharing skill, relative to /files/.
    path: str
    description: str | None = None
    actions: list[PanelAction] = Field(default_factory=list)


PanelWidget = Annotated[
    Union[
        CardGridWidget,
        TableWidget,
        MarkdownWidget,
        ProgressWidget,
        LogWidget,
        DocumentWidget,
    ],
    Field(discriminator="type"),
]


class _WidgetEnvelope(BaseModel):
    widget: PanelWidget


def _validate_box_id(box_id: str) -> str:
    box_id = box_id.strip()
    if not _BOX_ID_RE.match(box_id):
        raise ModelRetry(
            "box_id must match [A-Za-z0-9][A-Za-z0-9_-]{0,63} (letters, digits, '-', '_')."
        )
    return box_id


def _validate_widget(widget: dict[str, Any]) -> dict[str, Any]:
    try:
        parsed = _WidgetEnvelope(widget=widget)  # type: ignore[arg-type]
    except ValidationError as exc:
        raise ModelRetry(
            "Invalid widget. Allowed types: card_grid, table, markdown, progress, log, "
            f"document. Validation errors: {exc.errors(include_url=False)}"
        ) from exc
    return parsed.widget.model_dump(exclude_none=True)


def _validate_actions(actions: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    if not actions:
        return []
    try:
        parsed = [PanelAction.model_validate(action) for action in actions]
    except ValidationError as exc:
        raise ModelRetry(
            f"Invalid box actions: {exc.errors(include_url=False)}"
        ) from exc
    return [action.model_dump(exclude_none=True) for action in parsed]


# ── Tools ─────────────────────────────────────────────────────────────────────


async def panel_upsert_box(
    box_id: str,
    title: str,
    widget: dict[str, Any],
    actions: list[dict[str, Any]] | None = None,
) -> ToolReturn:
    """Create or replace one panel box next to the chat.

    `widget` must be one of (discriminated by `type`):
    - {"type": "card_grid", "items": [{"id", "title", "subtitle"?, "badge"?,
       "fields"?: {k: v}, "detail"?: markdown, "actions"?: [action]}]}
    - {"type": "table", "columns": [...], "rows": [[...], ...]}
    - {"type": "markdown", "text": "..."}
    - {"type": "progress", "status": "...", "percent"?: 0-100, "done"?: bool}
    - {"type": "log", "lines": ["..."]}
    - {"type": "document", "path": "<shared file path>", "description"?, "actions"?}

    An action is {"id", "label", "prompt"?, "style"?: default|primary|danger,
    "payload"?: {...}}. Clicking it sends `prompt` (or a generated
    `[panel action] …` message) back to you as a user message.

    Re-use the same box_id to update a box in place (e.g. progress updates).
    """
    box_id = _validate_box_id(box_id)
    box = {
        "id": box_id,
        "title": title.strip() or box_id,
        "widget": _validate_widget(widget),
        "actions": _validate_actions(actions),
    }
    return ToolReturn(
        return_value=f"panel box '{box_id}' rendered",
        metadata=[
            StateDeltaEvent(
                type=EventType.STATE_DELTA,
                delta=[{"op": "add", "path": f"/boxes/{box_id}", "value": box}],
            )
        ],
    )


async def panel_append_log(box_id: str, lines: list[str]) -> ToolReturn:
    """Append lines to an existing `log` box (create it first with panel_upsert_box)."""
    box_id = _validate_box_id(box_id)
    if not lines:
        raise ModelRetry("lines must contain at least one string.")
    ops = [
        {"op": "add", "path": f"/boxes/{box_id}/widget/lines/-", "value": str(line)}
        for line in lines
    ]
    return ToolReturn(
        return_value=f"appended {len(ops)} line(s) to panel box '{box_id}'",
        metadata=[StateDeltaEvent(type=EventType.STATE_DELTA, delta=ops)],
    )


async def panel_remove_box(box_id: str) -> ToolReturn:
    """Remove one panel box."""
    box_id = _validate_box_id(box_id)
    return ToolReturn(
        return_value=f"panel box '{box_id}' removed",
        metadata=[
            StateDeltaEvent(
                type=EventType.STATE_DELTA,
                delta=[{"op": "remove", "path": f"/boxes/{box_id}"}],
            )
        ],
    )


async def panel_clear() -> ToolReturn:
    """Remove all panel boxes."""
    return ToolReturn(
        return_value="panel cleared",
        metadata=[
            StateSnapshotEvent(type=EventType.STATE_SNAPSHOT, snapshot={"boxes": {}})
        ],
    )


def ui_panel_capability() -> Toolset:
    """Panel tools for the /aui channel agent (and only that agent)."""
    return Toolset(
        FunctionToolset(
            [panel_upsert_box, panel_append_log, panel_remove_box, panel_clear],
            id="ui-panel",
            instructions=(
                "An adaptive panel with widget boxes is shown next to the chat. Use the "
                "panel_* tools to show structured results: card grids with detail popups "
                "and action buttons, tables, progress/log streams, and shared documents "
                "(share files first with the file-sharing skill, then reference the shared "
                "path in a document widget). The panel is an enhancement: your chat reply "
                "must remain complete and useful on its own. Follow the active skill's "
                "'Adaptive panel' instructions when present. Messages starting with "
                "'[panel action]' are button clicks — handle them according to the skill "
                "that rendered the button, using the ids/payload in the message."
            ),
        )
    )
