from eap_iam import ROLE_ADMIN, ROLE_EDITOR, ROLE_VIEWER, scopes_for_roles


def test_viewer_has_no_write_scope_and_roles_union():
    assert scopes_for_roles({ROLE_VIEWER}) == {"mcp"}
    assert scopes_for_roles({ROLE_EDITOR}) == {"agui:run", "mcp", "manage"}
    assert scopes_for_roles({ROLE_ADMIN}) == {"agui:run", "mcp", "manage", "otlp"}
    assert scopes_for_roles({ROLE_VIEWER, ROLE_EDITOR}) == scopes_for_roles({ROLE_EDITOR})
    assert scopes_for_roles({"unknown"}) == frozenset() and scopes_for_roles(()) == frozenset()
