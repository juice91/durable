"""Writes the API's OpenAPI schema to the path given, without a database.

Run from the repository root: uv run --frozen --all-packages python site/scripts/dump-openapi.py OUT.json
"""
import json
import os
import sys
from pathlib import Path

os.environ["EAP_AUTH_MODE"] = "dev"
os.environ["EAP_DATABASE_URL"] = "postgresql+psycopg://docs:docs@127.0.0.1:1/docs"

import eap_db  # noqa: E402

eap_db.require_schema_at_head = lambda engine: None  # the schema comes from the routes, not from a database

from eap_api.app import create_app  # noqa: E402

out = Path(sys.argv[1])
schema = create_app().openapi()
out.parent.mkdir(parents=True, exist_ok=True)
out.write_text(json.dumps(schema, indent=1, sort_keys=True) + "\n")
print(f"{len(schema['paths'])} paths -> {out}", file=sys.stderr)
