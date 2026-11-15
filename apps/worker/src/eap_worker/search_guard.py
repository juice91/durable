"""The search tester's result writes, serialised with knowledge base and document deletes (P1.C4).

A `knowledge.search` tool run (the knowledge base search tester) holds its hits, with their section
text, between its `eap.tool_call` step and its last write. A delete that lands in between scrubs
only what the run has written so far, so each of the run's three result writes (the
`tool:tool_run:result` event, the `end` event and eap.finish_run) re-checks the sources inside its
own transaction (KnowledgeStore.write_search_result: the knowledge base row and the hits' document
rows locked FOR SHARE, which a delete's FOR UPDATE / DELETE waits for, and the reverse):

- hits whose document no longer exists are left out (the result's `text` is rebuilt from the hits
  that stay, and `dropped` counts the rest);
- when the knowledge base itself is gone, the run fails with `knowledge_base_deleted` and no
  content: the `tool_result` is an error, `end` is `run_failed`, the status `failed`.

Same steps, same positions (`eap.append_event`, `eap.finish_run` with a `search_guard` argument):
an ordinary tool run's layout is unchanged, and so is a tester run's.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from sqlalchemy import text

from eap_run_store import TERMINAL_RUN_STATUSES, KnowledgeStore, append_event_in_session, set_status_in_session

KB_DELETED_CODE = "knowledge_base_deleted"
KB_DELETED_ERROR = "The knowledge base was deleted while the search ran."


def hit_document_ids(content: Any) -> list[str]:
    hits = content.get("hits") if isinstance(content, Mapping) else None
    return sorted({str(h["document_id"]) for h in hits or () if isinstance(h, Mapping) and h.get("document_id")})


def search_guard_for(content: Any, kb_id: str) -> dict[str, Any]:
    """The `search_guard` argument of a tester run's result writes."""
    return {"kb_id": str(kb_id), "document_ids": hit_document_ids(content)}


def _rebuild_text(hits: list[Mapping[str, Any]]) -> str:
    from eap_worker.platform_tools import NO_RESULTS_TEXT, RETRIEVE_INSTRUCTION, quote_section

    if not hits:
        return NO_RESULTS_TEXT
    blocks = [f"[{h.get('n')}] {h.get('title')} — {h.get('ref')}\n" + quote_section(str(h.get("content") or ""))
              for h in hits]
    return "\n\n".join(blocks) + "\n\n" + RETRIEVE_INSTRUCTION


def filter_content(content: Any, live: frozenset[str]) -> Any:
    """`content` (knowledge_search's result) without the hits whose document is not in `live`."""
    if not isinstance(content, Mapping) or not isinstance(content.get("hits"), list):
        return content
    kept = [h for h in content["hits"] if isinstance(h, Mapping) and str(h.get("document_id")) in live]
    dropped = len(content["hits"]) - len(kept)
    if not dropped:
        return dict(content)
    return {**content, "hits": kept, "text": _rebuild_text(kept), "dropped": dropped}


def guarded_event(type: str, data: Mapping[str, Any], live: frozenset[str] | None) -> tuple[str, dict[str, Any]]:
    """The (type, data) a tester run's `tool_result` or `run_completed` event becomes."""
    data = dict(data)
    if type == "tool_result":
        if live is None:
            data.pop("content", None)
            return type, {**data, "status": "error", "error": KB_DELETED_ERROR, "error_code": KB_DELETED_CODE}
        return type, {**data, "content": filter_content(data.get("content"), live)}
    if type == "run_completed":
        if live is None:
            return "run_failed", {"status": "failed", "error": KB_DELETED_ERROR, "error_code": KB_DELETED_CODE}
        result = filter_content(data.get("result"), live)
        return type, {"output": _dump(result), "result": result}
    return type, data


def _dump(content: Any) -> str:
    return json.dumps(content, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def append_guarded(engine, tenant_id: str, run_id: str, event_key: str, type: str, data: Mapping[str, Any],
                   guard: Mapping[str, Any]) -> int:
    def write(session, live):
        etype, edata = guarded_event(type, data, live)
        event, _ = append_event_in_session(session, tenant_id, run_id, event_key, etype, edata)
        return event.seq

    return KnowledgeStore(engine).write_search_result(tenant_id, guard["kb_id"], guard["document_ids"], write)


def finish_guarded(engine, tenant_id: str, run_id: str, output: str | None, guard: Mapping[str, Any]) -> str:
    """Finish a tester run from `output` (its result JSON). Returns the status written. Idempotent:
    a re-executed step (the first commit landed, the checkpoint did not) finds the run already
    terminal and returns that status unchanged, even when the knowledge base was deleted since."""
    def write(session, live):
        current = session.execute(
            text("SELECT status FROM runs WHERE tenant_id = :t AND id = :r FOR UPDATE"),
            {"t": tenant_id, "r": str(run_id)},
        ).first()
        if current is not None and current.status in TERMINAL_RUN_STATUSES:
            return current.status
        if live is None:
            set_status_in_session(session, tenant_id, run_id, "failed", None, KB_DELETED_ERROR)
            return "failed"
        content = json.loads(output) if output else None
        set_status_in_session(session, tenant_id, run_id, "completed", _dump(filter_content(content, live)), None)
        return "completed"

    return KnowledgeStore(engine).write_search_result(tenant_id, guard["kb_id"], guard["document_ids"], write)


__all__ = [
    "KB_DELETED_CODE",
    "KB_DELETED_ERROR",
    "append_guarded",
    "filter_content",
    "finish_guarded",
    "guarded_event",
    "hit_document_ids",
    "search_guard_for",
]
