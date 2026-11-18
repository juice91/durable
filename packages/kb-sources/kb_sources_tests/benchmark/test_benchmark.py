"""The §19 benchmark through the real TI adapter and an in-memory store (no DBOS): correctness, idempotency,
resume, throughput, calls per product, retries, duplicates, payload sizes and a secret-leak scan, printed as a
table (run with `-s`, or read the captured log). The same world runs through the real worker in
`apps/worker/worker_tests/test_kb_source_sync_e2e.py::test_the_benchmark_world_through_the_real_workflow`."""

from __future__ import annotations

import json
import logging
import resource
import sys
import time
import tracemalloc

from eap_kb_sources.providers.ti.fixtures import CLIENT_ID, CLIENT_SECRET, TOKEN_PREFIX

from kb_sources_tests.providers.ti.helpers import MemoryStore, make_adapter, run_segment

from .fixtures import CALLS_PER_STORED_PART, EXPECTED, MALFORMED, MISSING, PARTS, TRANSIENT, previous_ledger, world

SENTINELS = (CLIENT_ID, CLIENT_SECRET, TOKEN_PREFIX)
STEP_CAP, CHECKPOINT_CAP = 32 * 1024, 8 * 1024


def seeded() -> tuple:
    server, names = world()
    store = MemoryStore()
    for part, (data, etag, last_modified) in previous_ledger(server).items():
        store.seed_document(part, "datasheet", data, etag=etag, last_modified=last_modified)
    return server, names, store


def size(value) -> int:
    return len(json.dumps(value, sort_keys=True, separators=(",", ":")).encode())


def segment_payload(result) -> dict:
    """What a segment step hands back to the workflow: counters, errors and the cursor, never payloads."""
    c = result.counters
    return {"counters": vars(c) if hasattr(c, "__dict__") else str(c), "errors": result.errors, "cursor": result.next_cursor,
            "complete": result.complete, "calls_used": result.calls_used}


def rss_mib() -> float:
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return peak / (1024 * 1024 if sys.platform == "darwin" else 1024)


def retries(server) -> int:
    """Requests beyond the first per (path, query): the transient error's second attempt."""
    seen: dict[tuple, int] = {}
    for r in server.requests:
        if r.host == "api.vendor.example" and "oauth" not in r.path:
            key = (r.path, json.dumps(r.query, sort_keys=True))
            seen[key] = seen.get(key, 0) + 1
    return sum(n - 1 for n in seen.values())


def test_the_benchmark_world(caplog):
    caplog.set_level(logging.DEBUG)
    timing_server, timing_names, timing_store = seeded()  # throughput: a run of its own, without the heap tracer
    started = time.perf_counter()
    timed = run_segment(make_adapter(timing_server), timing_store, timing_names)
    elapsed = time.perf_counter() - started
    records = timed.counters.inserted + timed.counters.updated + timed.counters.unchanged

    server, names, store = seeded()
    adapter = make_adapter(server)
    tracemalloc.start()
    first = run_segment(adapter, store, names)
    heap_peak = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()

    # correctness
    assert first.complete
    c = first.counters
    assert c.discovered == EXPECTED.discovered and c.failed == EXPECTED.failed
    assert sorted(e["code"] for e in first.errors) == sorted(EXPECTED.error_codes)
    assert c.documents_queued == EXPECTED.documents_queued
    assert len(store.entities) == EXPECTED.stored_parts and MISSING not in store.entities and MALFORMED not in store.entities
    assert TRANSIENT in store.entities, "the transient error was retried"
    api_calls = sum(1 for r in server.requests if r.host == "api.vendor.example" and "oauth" not in r.path)
    stored = EXPECTED.stored_parts
    retried = retries(server)
    # the stored parts cost three calls each; the retry (503), the malformed and the missing part one call each
    assert api_calls == stored * CALLS_PER_STORED_PART + 3

    assert retried == 1 and records == c.inserted + c.updated + c.unchanged

    # idempotency: the same parts again change nothing and queue nothing
    submits_before = len(store.submits)
    # (the scripted malformed response and 503 fire once: the second pass runs over the eight stored parts)
    again = run_segment(adapter, store, [n for n in names if n not in (MALFORMED, MISSING)])
    assert (again.counters.inserted, again.counters.updated, again.counters.documents_queued) == (0, 0, 0)
    assert again.counters.unchanged == c.inserted and again.counters.failed == 0
    assert len(store.submits) == submits_before, "no document is submitted twice"

    # resume: the walk of the whole listing in one-page segments equals a single segment
    one, sizes = MemoryStore(), []
    cursor, segments = {}, 0
    walk_server, _ = world()
    walk_adapter = make_adapter(walk_server)
    while True:
        part = run_segment(walk_adapter, one, mode="full", cursor=cursor, config={"page_size": 3}, page_budget=1)
        sizes.append(size(part.next_cursor))
        segments += 1
        if part.complete:
            break
        cursor = part.next_cursor
    whole, ref = MemoryStore(), world()[0]
    run_segment(make_adapter(ref), whole, mode="full", config={"page_size": 3}, page_budget=10)
    assert segments == 3 and one.entities == whole.entities and one.records == whole.records, "resume = one pass"

    # duplicates: one document per (part, relationship)
    keys = [(s[0], s[1]) for s in store.submits]
    duplicates = len(keys) - len(set(keys))
    assert duplicates == 0

    # sizes and the leak scan
    step_bytes = max(size(segment_payload(r)) for r in (first, again))
    checkpoint_bytes = max([size(first.next_cursor), size(again.next_cursor), *sizes])
    assert step_bytes < STEP_CAP and checkpoint_bytes < CHECKPOINT_CAP
    leak = [s for s in SENTINELS if s in caplog.text or s in repr(store.records) or s in repr(first) or s in repr(store.entities)]
    assert leak == []

    rows = [
        ("parts requested / stored / failed", f"{PARTS} / {stored} / {c.failed}"),
        ("datasheets queued first / second run", f"{c.documents_queued} / {again.counters.documents_queued}"),
        ("second run: unchanged / changed", f"{again.counters.unchanged} / {again.counters.inserted + again.counters.updated}"),
        ("resume: segments / equal to one pass", f"{segments} / True"),
        ("records per second", f"{records / elapsed:,.0f}"),
        ("API calls per product", f"{api_calls / PARTS:.2f}"),
        ("retries", str(retried)),
        ("duplicate documents", str(duplicates)),
        ("largest step payload (bytes)", f"{step_bytes} (< {STEP_CAP})"),
        ("largest checkpoint (bytes)", f"{checkpoint_bytes} (< {CHECKPOINT_CAP})"),
        ("heap peak (MiB) / process peak RSS (MiB)", f"{heap_peak / 2**20:.1f} / {rss_mib():.0f}"),
        ("secret-leak scan", "clean"),
    ]
    width = max(len(k) for k, _ in rows)
    table = "\n".join(f"| {k.ljust(width)} | {v} |" for k, v in rows)
    print("\n§19 benchmark (adapter level)\n" + table)
    logging.getLogger("benchmark").info("§19 benchmark (adapter level)\n%s", table)
