"""Authenticated tool catalog API tests."""


def test_tool_catalog_requires_authentication(client) -> None:
    """The platform catalog is not exposed to anonymous callers."""
    response = client.get("/api/v1/tools")

    assert response.status_code == 401


def test_tool_catalog_defaults_to_all_tools_allowed(client, auth_header, monkeypatch) -> None:
    """An absent platform allowlist exposes every declaration as allowed."""
    monkeypatch.delenv("EAP_ALLOWED_TOOLS", raising=False)

    response = client.get("/api/v1/tools", headers=auth_header)

    assert response.status_code == 200
    body = response.json()
    # The kb source sync tools (kb.source.*, settings.connection.check, workflow.run.*) follow the original three.
    assert [t["id"] for t in body[3:]] == [
        "kb.source.get", "settings.connection.check", "kb.source.sync", "workflow.run.wait", "workflow.run.result",
    ]
    assert all(t["allowed"] for t in body[3:])
    assert {t["id"]: t["side_effect"] for t in body[3:]}["kb.source.sync"] is True
    assert body[:3] == [
        {
            "id": "clock.now",
            "title": "Current time",
            "description": "Returns current UTC ISO time",
            "side_effect": False,
            "external_idempotent": False,
            "input_schema": {
                "additionalProperties": False,
                "properties": {},
                "type": "object",
            },
            "allowed": True,
        },
        {
            "id": "tickets.create",
            "title": "Create ticket",
            "description": "Create a new ticket",
            "side_effect": True,
            "external_idempotent": True,
            "input_schema": {
                "additionalProperties": False,
                "properties": {
                    "body": {"type": "string"},
                    "title": {"type": "string"},
                },
                "required": ["title", "body"],
                "type": "object",
            },
            "allowed": True,
        },
        {
            "id": "secrets.request",
            "title": "Request a secret",
            "description": (
                "Ask the person for a credential. They type it into a masked card; you receive only a "
                "reference (secret:<name>) to pass as an argument, never the value"
            ),
            "side_effect": True,
            "external_idempotent": False,
            "input_schema": {
                "additionalProperties": False,
                "properties": {
                    "name": {"type": "string"},
                    "purpose": {"type": "string"},
                },
                "required": ["name", "purpose"],
                "type": "object",
            },
            "allowed": True,
        },
    ]


def test_tool_detail_reflects_explicit_platform_allowlist(client, auth_header, monkeypatch) -> None:
    """A registered tool outside EAP_ALLOWED_TOOLS remains visible but is marked disallowed."""
    monkeypatch.setenv("EAP_ALLOWED_TOOLS", " clock.now, unknown.tool ")

    response = client.get("/api/v1/tools/tickets.create", headers=auth_header)

    assert response.status_code == 200
    assert response.json()["id"] == "tickets.create"
    assert response.json()["allowed"] is False


def test_unknown_tool_detail_returns_404(client, auth_header) -> None:
    """Catalog lookup distinguishes an unknown declaration from a disallowed tool."""
    response = client.get("/api/v1/tools/missing.tool", headers=auth_header)

    assert response.status_code == 404
    assert response.json() == {"detail": "Tool not found"}
