"""A thread's history as AG-UI messages (D13), built from the data `AgUiService.thread_data` reads.

Pure: no store, no request. `build_thread_view` is what `GET /ag-ui/agents/{id}/threads/{threadId}` shows and what a
run's `MESSAGES_SNAPSHOT` starts from.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from eap_api.ag_ui.snapshot import assistant_message_id, turn_messages, user_message
from eap_api.streams.native import TERMINAL_STATUSES


@dataclass
class ThreadView:
    messages: list[dict[str, Any]] = field(default_factory=list)
    runs: list[dict[str, Any]] = field(default_factory=list)
    active_run_id: str | None = None


def turn_entry(prefix: str, client_id: str | None, run: dict[str, Any], done: dict[str, Any] | None) -> list[dict[str, Any]]:
    """One earlier turn's AG-UI messages: from its stored messages when it completed with them,
    else its user input (and a completed run's output as the assistant answer)."""
    if done is not None and done.get("messages"):
        structured = done.get("output") if run.get("output_format") == "json" else None
        return turn_messages(prefix, done["messages"], user_message_client_id=client_id, structured_output=structured)
    out: list[dict[str, Any]] = []
    if run.get("input"):
        out.append(user_message(prefix, run["input"], client_id))
    if done is not None and done.get("output"):
        out.append({"id": assistant_message_id(prefix, 0), "role": "assistant", "content": done["output"]})
    return out


def attachment_parts(
    messages: list[dict[str, Any]], files_of: Callable[[list[str]], dict[str, Any]], *, via_api_key: bool
) -> list[dict[str, Any]]:
    """User messages that sent attachments carry AG-UI 1.0 media parts (text first)."""
    ids = [m["id"] for m in messages if m.get("role") == "user" and isinstance(m.get("content"), str)]
    by_message = files_of(ids)
    if not by_message:
        return messages
    # An API key fetches through the AG-UI twin; the dev bearer through the dev route.
    base = "/api/v1/ag-ui/attachments" if via_api_key else "/api/v1/attachments"
    out: list[dict[str, Any]] = []
    for m in messages:
        files = by_message.get(m.get("id")) if m.get("role") == "user" else None
        if not files:
            out.append(m)
            continue
        parts: list[dict[str, Any]] = [{"type": "text", "text": m["content"]}] if m["content"] else []
        for f in files:
            kind = "image" if f.media_type.startswith("image/") else "document"
            parts.append({
                "type": kind,
                "source": {"type": "url", "value": f"{base}/{f.id}", "mimeType": f.media_type},
                "metadata": {"attachmentId": f.id, "filename": f.name, "size": f.size},
            })
        out.append({**m, "content": parts})
    return out


def build_thread_view(
    data: Any,
    files_of: Callable[[list[str]], dict[str, Any]],
    *,
    via_api_key: bool,
    later_ids: tuple[str, ...] = (),
) -> ThreadView:
    """The thread's turns as AG-UI messages, oldest first. Ids follow D13 with the AG-UI runId that
    started each turn as the prefix (the native run id for a turn started elsewhere, e.g. Studio's REST chat).

    Regenerates (D5, D7): a turn whose user message a later turn sends again (a regenerate
    re-POSTs the same user message under a new runId) is superseded: the later turn shows the
    same first client id, or re-sends all of the earlier turn's ids. Its messages are
    left out, so the thread shows the user message once, followed by the latest branch's answer,
    and AG-UI message ids stay unique in the thread. The superseded turn stays in `runs` with
    `superseded: true` (it is still a run, with its own usage and trace). `later_ids` are the
    user-message ids of a turn after the thread's last (the run being streamed), which supersede too."""
    turns, completed, records = data.turns, data.completed, data.records
    if not turns:
        return ThreadView()
    # Walk newest first. A turn shows its user message under its first client id, so an earlier
    # turn is superseded when a later turn (or `later_ids`) shows the same first id, or re-sends
    # all of its ids: ids stay unique, and a turn re-running only part of an earlier turn's
    # messages (a regenerate of u1 after [u0, u1]) leaves that earlier turn's text in place.
    later: list[tuple[str, ...]] = [later_ids] if later_ids else []
    superseded: set[str] = set()
    for t in reversed(turns):
        rec = records.get(str(t["id"]))
        ids = rec.input_message_ids if rec is not None else ()
        if ids and any(l[0] == ids[0] or set(ids) <= set(l) for l in later):
            superseded.add(str(t["id"]))
        if ids:
            later.append(ids)
    view = ThreadView()
    for t in turns:
        run_id = str(t["id"])
        rec = records.get(run_id)
        prefix = rec.external_run_id if rec is not None else run_id
        client_id = rec.input_message_ids[0] if rec is not None and rec.input_message_ids else None
        if run_id not in superseded:
            view.messages += turn_entry(prefix, client_id, t, completed.get(run_id))
        view.runs.append(
            {
                "runId": rec.external_run_id if rec is not None else None,
                "run_id": run_id,
                "turn": t["turn"],
                "status": t["status"],
                "display_status": t["display_status"],
                "superseded": run_id in superseded,
            }
        )
        if t["status"] not in TERMINAL_STATUSES and rec is not None:
            view.active_run_id = rec.external_run_id
    view.messages = attachment_parts(view.messages, files_of, via_api_key=via_api_key)
    return view
