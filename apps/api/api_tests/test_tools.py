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
    assert response.json() == [
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
