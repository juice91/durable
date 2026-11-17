"""Unsent chat attachments expire (eap.attachment_retention): config, the sweep loop, the batch
(rows first, guarded by `message_id IS NULL`, then blobs), the workflow and the schedule.

The in-process tests date rows in 2001 and run a 2001 tick, so the sweep (which crosses tenants)
only ever touches this file's rows."""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import text

from eap_blob_store import FsBlobStore, attachment_key
from eap_run_store.chat_attachments import ChatAttachmentStore
from eap_worker import attachment_retention as ar
from eap_worker.attachment_retention import (
    DEFAULT_SCHEDULE,
    SCHEDULE_NAME,
    AttachmentRetentionConfig,
    AttachmentRetentionConfigError,
    apply_attachment_retention_schedule,
    attachment_retention,
    delete_batch,
    sweep,
)

TICK = datetime(2001, 6, 2, tzinfo=timezone.utc)  # 24 h: cutoff 2001-06-01
OLD, RECENT = "2001-01-01T00:00:00+00", "2001-06-01T12:00:00+00"


# ---------------------------------------------------------------------------------- config


def test_config_defaults_and_overrides():
    cfg = AttachmentRetentionConfig.from_env({})
    assert (cfg.hours, cfg.schedule, cfg.off) == (24, "0 30 3 * * *", False) and cfg.schedule == DEFAULT_SCHEDULE
    cfg = AttachmentRetentionConfig.from_env({"EAP_ATTACHMENT_ORPHAN_HOURS": "6", "EAP_ATTACHMENT_RETENTION_SCHEDULE": "0 0 1 * * *"})
    assert (cfg.hours, cfg.schedule) == (6, "0 0 1 * * *") and cfg.to_context() == {"hours": 6}
    assert AttachmentRetentionConfig.from_env({"EAP_ATTACHMENT_RETENTION_SCHEDULE": " OFF "}).off is True
    for bad in ("0", "-1", "x", "99999"):
        with pytest.raises(AttachmentRetentionConfigError, match="EAP_ATTACHMENT_ORPHAN_HOURS"):
            AttachmentRetentionConfig.from_env({"EAP_ATTACHMENT_ORPHAN_HOURS": bad})


def test_context_hours_fall_back_to_the_default():
    for context in (None, {}, {"hours": 0}, {"hours": "x"}, {"hours": True}):
        assert AttachmentRetentionConfig.hours_from_context(context) == 24
    assert AttachmentRetentionConfig.hours_from_context({"hours": 5}) == 5


# ---------------------------------------------------------------------------------- the loop


def test_batches_until_done_and_counts_per_tenant():
    calls: list[tuple[str, int]] = []
    pages = [
        {"listed": 2, "deleted": 2, "blob_failures": 0, "tenants": {"a": 2}},
        {"listed": 2, "deleted": 1, "blob_failures": 1, "tenants": {"a": 1}},
        {"listed": 1, "deleted": 1, "blob_failures": 0, "tenants": {"b": 1}},
    ]

    def batch(cutoff: str, limit: int):
        calls.append((cutoff, limit))
        return pages[len(calls) - 1]

    out = sweep(24, TICK, batch, batch_size=2)
    assert out["complete"] is True and out["batches"] == 3
    assert out["deleted"] == 4 and out["blob_failures"] == 1 and out["tenants"] == {"a": 3, "b": 1}
    assert {c for c, _ in calls} == {"2001-06-01T00:00:00+00:00"}  # one cutoff for the whole run


def test_a_batch_that_lost_every_row_to_a_link_still_continues():
    seen = []

    def batch(cutoff, limit):
        seen.append(1)
        return {"listed": 2, "deleted": 0, "blob_failures": 0, "tenants": {}} if len(seen) == 1 else {
            "listed": 0, "deleted": 0, "blob_failures": 0, "tenants": {}}

    assert sweep(24, TICK, batch, batch_size=2)["batches"] == 2


def test_the_run_is_bounded():
    out = sweep(24, TICK, lambda c, n: {"listed": n, "deleted": n, "blob_failures": 0, "tenants": {}},
                batch_size=3, max_batches=4)
    assert out["complete"] is False and out["batches"] == 4 and out["deleted"] == 12


# ---------------------------------------------------------------------------------- the batch


def _put(root, tenant: str, attachment_id: str) -> str:
    key = attachment_key(tenant, attachment_id)

    async def chunks():
        yield b"png"

    asyncio.run(FsBlobStore(root).put(key, chunks(), 3, "image/png"))
    return key


def _plant(engine, root, tenant: str, created_at: str, *, message_id: str | None = None) -> tuple[str, str]:
    attachment_id = str(uuid.uuid4())
    key = _put(root, tenant, attachment_id)
    ChatAttachmentStore(engine).create(
        tenant, attachment_id=attachment_id, name="a.png", media_type="image/png", size=3, sha256="0" * 64,
        storage_key=key, created_by="alice",
    )
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE chat_attachments SET created_at = CAST(:c AS timestamptz), message_id = :m WHERE id = :i"),
            {"c": created_at, "m": message_id, "i": attachment_id},
        )
    return attachment_id, key


def _rows(engine, tenant: str) -> set[str]:
    with engine.connect() as conn:
        return {r[0] for r in conn.execute(text("SELECT id::text FROM chat_attachments WHERE tenant_id = :t"), {"t": tenant})}


@pytest.fixture
def blobs(tmp_path, monkeypatch):
    store = FsBlobStore(tmp_path)
    monkeypatch.setattr(ar, "blob_store", lambda: store)
    return tmp_path, store


@pytest.fixture
def attach_tenant(tenant, worker):
    yield tenant
    with worker.engine.begin() as conn:
        conn.execute(text("DELETE FROM chat_attachments WHERE tenant_id = :t"), {"t": tenant})


def test_deletes_unsent_older_than_cutoff_and_keeps_linked_and_recent(worker, attach_tenant, blobs):
    root, store = blobs
    engine, t = worker.engine, attach_tenant
    old, old_key = _plant(engine, root, t, OLD)
    linked, linked_key = _plant(engine, root, t, OLD, message_id="m1")
    recent, recent_key = _plant(engine, root, t, RECENT)
    from dbos import DBOS

    out = DBOS.start_workflow(attachment_retention, TICK, {"hours": 24}).get_result()
    assert out["complete"] is True and out["tenants"][t] == 1 and out["blob_failures"] == 0
    assert _rows(engine, t) == {linked, recent}
    assert not asyncio.run(store.exists(old_key))
    assert asyncio.run(store.exists(linked_key)) and asyncio.run(store.exists(recent_key))
    assert t not in str(out["cutoff"])
    again = DBOS.start_workflow(attachment_retention, TICK, {"hours": 24}).get_result()
    assert again["tenants"].get(t) is None  # nothing of this tenant left


def test_retention_skips_rows_linked_during_the_batch(worker, attach_tenant, blobs):
    root, store = blobs
    engine, t = worker.engine, attach_tenant
    racing, racing_key = _plant(engine, root, t, OLD)
    plain, plain_key = _plant(engine, root, t, OLD)
    real = ChatAttachmentStore(engine)

    class Racing:
        """The list happens, then a message links one row before the DELETE."""

        def unsent_before(self, cutoff, limit):
            ids = real.unsent_before(cutoff, limit)
            real.link(t, [racing], message_id="m-race")
            return ids

        def delete_unsent_ids(self, ids):
            return real.delete_unsent_ids(ids)

    out = delete_batch(Racing(), datetime(2001, 6, 1, tzinfo=timezone.utc), 500, ar._remove_blobs)  # type: ignore[arg-type]
    assert out["tenants"] == {t: 1}
    assert _rows(engine, t) == {racing}
    assert asyncio.run(store.exists(racing_key))  # the linked upload keeps its blob
    assert not asyncio.run(store.exists(plain_key))


def test_a_blob_that_cannot_be_deleted_is_counted_and_the_batch_goes_on(worker, attach_tenant, blobs):
    root, _ = blobs
    engine, t = worker.engine, attach_tenant
    _plant(engine, root, t, OLD)
    _plant(engine, root, t, OLD)
    out = delete_batch(ChatAttachmentStore(engine), datetime(2001, 6, 1, tzinfo=timezone.utc), 500, lambda keys: len(keys))
    assert out["deleted"] == 2 and out["blob_failures"] == 2 and _rows(engine, t) == set()


# ---------------------------------------------------------------------------------- schedule


def test_schedule_is_applied_idempotent_and_off_removes_it(worker):
    from dbos import DBOS

    try:
        cfg = AttachmentRetentionConfig.from_env({})
        assert apply_attachment_retention_schedule(cfg) == "applied"
        assert apply_attachment_retention_schedule(cfg) == "applied"
        sched = DBOS.get_schedule(SCHEDULE_NAME)
        assert sched is not None and sched["schedule"] == DEFAULT_SCHEDULE
        assert sched["workflow_name"] == "eap.attachment_retention" and sched["context"] == {"hours": 24}
        assert apply_attachment_retention_schedule(AttachmentRetentionConfig.from_env({"EAP_ATTACHMENT_RETENTION_SCHEDULE": "off"})) == "removed"
        assert DBOS.get_schedule(SCHEDULE_NAME) is None
    finally:
        DBOS.delete_schedule(SCHEDULE_NAME)


def test_off_applies_no_schedule(worker):
    from dbos import DBOS

    DBOS.delete_schedule(SCHEDULE_NAME)
    assert apply_attachment_retention_schedule(AttachmentRetentionConfig.from_env({"EAP_ATTACHMENT_RETENTION_SCHEDULE": "off"})) == "removed"
    assert DBOS.get_schedule(SCHEDULE_NAME) is None
