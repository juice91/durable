"""The model file and its DSL transformer: shipped, parseable, and the same JSON the OpenFGA CLI produces."""

import json
import shutil
import subprocess

import pytest
from eap_iam_openfga.dsl import ModelSyntaxError, canonical, parse_model, relations_of
from eap_iam_openfga.model import model_json, model_path, model_relations, model_text
from eap_iam_openfga.names import relation_for, requirements


def test_the_model_ships_next_to_its_tests():
    assert model_path().name == "durable.fga"
    assert (model_path().parent / "durable.fga.yaml").is_file()


def test_every_relation_the_engine_can_ask_exists_in_the_model():
    rel = model_relations()
    assert {"agent", "workflow", "knowledge_base", "secret", "schedule", "mcp_server", "api_key", "run", "tenant", "user", "group"} <= set(rel)
    for kind in ("agent", "workflow"):
        assert {"can_read", "can_run", "can_edit", "can_publish", "can_approve", "can_archive", "can_manage_access", "can_grant_use"} <= rel[kind]
    assert relation_for("create", "secret", False) == "can_create_secret"
    assert relation_for("approve", "run", True) == "can_approve" and relation_for("approve", "secret", True) is None


def test_the_directly_related_types_match_the_grants_and_registrations_the_engine_writes():
    meta = {t["type"]: t["metadata"]["relations"] for t in model_json()["type_definitions"] if "metadata" in t}
    direct = lambda t, r: {(d["type"], d.get("relation")) for d in meta[t][r].get("directly_related_user_types", [])}  # noqa: E731
    assert ("tenant", None) in direct("agent", "tenant") and ("user", None) in direct("agent", "owner")
    assert ("tenant", "editor") in direct("secret", "legacy_usable_by")
    assert {("agent", None), ("workflow", None)} == direct("secret", "granted_to") == direct("mcp_server", "granted_to")
    assert {("agent", None), ("workflow", None)} == direct("run", "parent")
    # Role grants (the Access tab): editor, runner, approver and viewer take a tenant role; an owner is a person.
    assert {("tenant", r) for r in ("admin", "editor", "viewer")} <= direct("workflow", "runner")
    assert not [d for d in direct("agent", "owner") if d[0] == "tenant"] and not [d for d in direct("secret", "owner") if d[0] == "tenant"]


@pytest.mark.parametrize(
    "text",
    [
        "model\n  schema 1.0\n",
        "model\n  schema 1.1\ntype a\n  relations\n    define x: [user] and y\n",
        "model\n  schema 1.1\ntype a\n  relations\n    define x: [user] but not y\n",
        "model\n  schema 1.1\ntype a\n  relations\n    define x: [user]\n    define x: [user]\n",
        "model\n  schema 1.1\n  define x: [user]\n",
        "type a\n",
    ],
)
def test_what_the_transformer_does_not_support_is_refused(text):
    with pytest.raises(ModelSyntaxError):
        parse_model(text)


def test_a_small_model_transforms_like_the_documented_json():
    got = parse_model("model\n  schema 1.1\ntype user\ntype doc\n  relations\n    define owner: [user]\n    define can_x: owner or owner from parent\n    define parent: [doc]\n")
    assert got["type_definitions"][0] == {"type": "user"}
    doc = got["type_definitions"][1]
    assert doc["relations"]["owner"] == {"this": {}}
    assert doc["relations"]["can_x"]["union"]["child"][1] == {
        "tupleToUserset": {"tupleset": {"relation": "parent"}, "computedUserset": {"relation": "owner"}}
    }
    assert relations_of(got) == {"user": frozenset(), "doc": frozenset({"owner", "can_x", "parent"})}


@pytest.mark.skipif(shutil.which("fga") is None, reason="the OpenFGA CLI (`fga`) is not installed; CI runs this comparison")
def test_the_transformer_agrees_with_the_openfga_cli():
    theirs = json.loads(subprocess.run(["fga", "model", "transform", "--file", str(model_path()), "--output-format", "json"], check=True, capture_output=True, text=True, timeout=60).stdout)
    assert canonical(theirs) == canonical(parse_model(model_text()))


@pytest.mark.skipif(shutil.which("fga") is None, reason="the OpenFGA CLI (`fga`) is not installed; CI runs `fga model test`")
def test_the_model_tests_pass_under_the_openfga_cli():
    done = subprocess.run(["fga", "model", "test", "--tests", str(model_path().with_name("durable.fga.yaml"))], capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stdout + done.stderr


def test_external_access_is_managed_by_admins_and_read_by_editors():
    assert relation_for("manage_external_access", "tenant", False) == "can_manage_external_access"
    assert relation_for("read_external_access", "tenant", False) == "can_read_external_access"
    assert requirements("manage_external_access", "tenant", False).roles == ("admin",)
    assert set(requirements("read_external_access", "tenant", False).roles) == {"admin", "editor"}


def test_the_wave_2_actions_have_relations_and_an_unknown_one_is_still_denied():
    """P2b wave 2: tenant-wide actions the services ask for, and `run` on an MCP server."""
    for action, relation in (
        ("write", "can_write"), ("run", "can_run"), ("manage_connections", "can_manage_connections"),
        ("read_telemetry", "can_read_telemetry"), ("ingest.otlp", "can_otlp"), ("manage_settings", "can_manage_settings"),
    ):
        assert relation_for(action, "tenant", False) == relation, action
    assert relation_for("run", "mcp_server", True) == "can_run"
    assert relation_for("run", "secret", True) is None  # a secret is used through `grant_use`, not run
    assert relation_for("frobnicate", "tenant", False) is None
