"""RAG P1 quality fix (yaml extractor 3; json stays 1): bulk re-extract selects the YAML documents
recorded with an older extractor, and nothing else. JSON is not bumped: its only change is the
title fallback, which API uploads never reach (they always pass a title)."""

from __future__ import annotations

from eap_knowledge import EXTRACTOR_VERSIONS

from .run_helpers import requires_pg
from .test_p1a4_upload_reextract import (  # noqa: F401 - fixtures
    PDF,
    RST,
    YAML,
    _activate,
    _b64,
    _kb,
    _knowledge_env,
    _put,
    kb_cleanup,
)
from .test_runs import deltas, fake_dbos, pg, stream_settings, tenant_of  # noqa: F401 - fixtures

pytestmark = requires_pg

API = "/api/v1"


def test_bulk_reextract_selects_old_yaml_only(pg, tenant_of, fake_dbos, kb_cleanup):
    assert (EXTRACTOR_VERSIONS["yaml"], EXTRACTOR_VERSIONS["json"]) == (3, 1)
    client, _ = pg
    _, h = tenant_of()
    kb = _kb(client, h)
    docs = {
        # key: (upload body, extractor, recorded version)
        "y2.yaml": ({"content": YAML, "mime_type": "application/yaml"}, "yaml", 2),  # comments fix: outdated
        "y3.yaml": ({"content": YAML, "mime_type": "application/yaml"}, "yaml", 3),  # current
        "j1.json": ({"content": '{"a": 1}', "mime_type": "application/json"}, "json", 1),  # current: not bumped
        "a.rst": ({"content": RST, "mime_type": "text/x-rst"}, "rst", 1),
        "p.pdf": ({"content_base64": _b64(PDF), "mime_type": "application/pdf"}, "pdf", 1),
        "m.md": ({"content": "# M\n\nx\n", "mime_type": "text/markdown"}, None, 1),
        "t.txt": ({"content": "plain\n", "mime_type": "text/plain"}, None, 1),
        "h.html": ({"content": "<h1>H</h1><p>x</p>", "mime_type": "text/html"}, None, 1),
    }
    for key, (body, extractor, version) in docs.items():
        doc = _put(client, h, kb["id"], key, **body)
        assert doc.status_code in (200, 201, 202), doc.text
        _activate(pg, doc.json()["document"]["id"], extractor, version)
    dry = client.post(f"{API}/knowledge-bases/{kb['id']}/reextract", json={"dry_run": True}, headers=h)
    assert dry.status_code == 200, dry.text
    assert sorted(d["document_key"] for d in dry.json()["documents"]) == ["y2.yaml"]
