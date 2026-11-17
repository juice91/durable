"""The brief's §19 benchmark world, on top of the fixture TI server (`benchmark_server`).

Ten requested parts of two families (`FIXA-Q1`: six, `FIXB`: four of which three exist), three datasheets, and
the five situations a sync has to get right:

| part | situation |
|---|---|
| FIXA1DR | datasheet unchanged since the last sync (same ETag) |
| FIXB1DR/NOPB | datasheet changed (a newer revision than the one stored) |
| FIXA3DR | datasheet new |
| FIXB2DR/NOPB | malformed response (`{not json`) |
| FIXB3DR/NOPB | transient API error (503 once, then fine) |
| FIXB4DR/NOPB | missing product (404) |

`EXPECTED` is what a first run over the seeded ledger must report: 8 parts stored, 2 failures, 2 datasheets queued.
"""

from __future__ import annotations

from dataclasses import dataclass

from eap_kb_sources.providers.ti.fixtures import FakeTiServer, benchmark_server, pdf_bytes

PARTS = 10
FAMILIES = 2
DATASHEETS = 3
UNCHANGED, CHANGED, NEW = "FIXA1DR", "FIXB1DR/NOPB", "FIXA3DR"
MALFORMED, TRANSIENT, MISSING = "FIXB2DR/NOPB", "FIXB3DR/NOPB", "FIXB4DR/NOPB"
# Every request the fixture needs for one stored part: product, parametrics, quality (+ the datasheet, not an API call).
CALLS_PER_STORED_PART = 3


@dataclass(frozen=True)
class Expected:
    discovered: int = 8  # 10 requested, one missing, one malformed
    failed: int = 2
    error_codes: tuple[str, ...] = ("connection_response_invalid", "not_found")
    documents_queued: int = 2  # the changed one and the new one; the unchanged one is skipped
    stored_parts: int = 8


EXPECTED = Expected()


def world() -> tuple[FakeTiServer, list[str]]:
    server, names = benchmark_server()
    assert len(names) == PARTS and len({p.generic for p in server.parts.values()}) == FAMILIES
    assert sum(1 for p in server.parts.values() if p.datasheet) == DATASHEETS
    return server, names


def previous_ledger(server: FakeTiServer) -> dict[str, tuple[bytes, str | None, str | None]]:
    """What the store holds from the previous sync: the unchanged datasheet as the server has it now, the changed
    one as an older revision. Maps part -> (bytes, etag, last_modified)."""
    a1 = server.parts[UNCHANGED]
    return {
        UNCHANGED: (a1.datasheet or b"", a1.etag, a1.last_modified),
        CHANGED: (pdf_bytes("old", 1), '"old"', None),
    }
