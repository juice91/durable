"""CSV import for eval suites (parity round 2, D6): columns, upsert by name, all or nothing, JSON
or text expected output, and the workflow suite's input check."""

from __future__ import annotations

import uuid

import pytest

from eap_api.eval_format import CsvFormatError, parse_csv_cases

from .test_evals_api import API, _agent, _suite, api, fake, pg  # noqa: F401
from .test_evals_workflow_api import wf_api, workflow_suite  # noqa: F401
from .test_workflow_runs_api import publish_demo


def post_csv(client, h, suite_id, body: str, **kw):
    return client.post(f"{API}/eval-suites/{suite_id}/import?format=csv", content=body.encode(), headers=h, **kw)


def cases_of(client, h, suite_id):
    return {c["name"]: c for c in client.get(f"{API}/eval-suites/{suite_id}/cases", headers=h).json()["items"]}


@pg
def test_csv_import_creates_and_updates_by_name(api):
    client, _, tenant_of = api
    _, h = tenant_of()
    suite = _suite(client, h, _agent(client, h), cases=("a",))
    before = cases_of(client, h, suite)["a"]
    assert client.patch(f"{API}/eval-suites/{suite}/cases/{before['id']}", headers=h, json={
        "evaluators": [{"kind": "regex", "pattern": "x", "name": "mine"}]}).status_code == 200
    body = 'name,input,expected_output,metadata\na,new input,done,"{""area"": ""chat""}"\nb,"two\nlines, with comma",,\n'
    r = post_csv(client, h, suite, body)
    assert r.status_code == 200, r.text
    assert (r.json()["created"], r.json()["updated"], r.json()["suite"]["case_count"]) == (1, 1, 2)
    cases = cases_of(client, h, suite)
    assert cases["a"]["id"] == before["id"] and cases["a"]["position"] == before["position"]
    assert cases["a"]["input"] == "new input" and cases["a"]["metadata"] == {"area": "chat"}
    assert [e["name"] for e in cases["a"]["evaluators"]] == ["mine"]  # a CSV has no evaluators: kept
    assert cases["b"]["input"] == "two\nlines, with comma" and cases["b"]["expected_output"] is None
    assert cases["b"]["position"] == 1
    # The suite's own evaluators stay too, and text/csv without ?format is read as CSV.
    r = client.post(f"{API}/eval-suites/{suite}/import", content=b"name,input\nc,three\n", headers={**h, "content-type": "text/csv"})
    assert r.status_code == 200 and r.json()["created"] == 1
    assert r.json()["suite"]["evaluators"] == [{"kind": "equals_expected"}]


@pg
def test_csv_import_is_all_or_nothing(api):
    client, _, tenant_of = api
    _, h = tenant_of()
    suite = _suite(client, h, _agent(client, h), cases=("only",))
    r = post_csv(client, h, suite, "name,input\nok,hello\n,missing-name\n")
    assert r.status_code == 422 and r.json()["detail"]["row"] == 3
    assert r.json()["detail"]["code"] == "csv_invalid" and "name" in r.json()["detail"]["message"]
    assert list(cases_of(client, h, suite)) == ["only"]  # no case changed or added
    # An update row before the bad one is not applied either.
    r = post_csv(client, h, suite, "name,input\nonly,CHANGED\nbad,\n")
    assert r.status_code == 422 and r.json()["detail"]["row"] == 3
    assert cases_of(client, h, suite)["only"]["input"] == "in only"
    assert client.get(f"{API}/eval-suites/{suite}", headers=h).json()["case_count"] == 1


@pg
def test_csv_expected_output_json_or_text(api):
    client, _, tenant_of = api
    _, h = tenant_of()
    suite = _suite(client, h, _agent(client, h), cases=())
    body = 'name,input,expected_output\nobj,i,"{""label"": ""bug""}"\nnum,i,42\ntext,i,hello world\nbroken,i,"{not json"\nempty,i,\n'
    assert post_csv(client, h, suite, body).status_code == 200
    cases = cases_of(client, h, suite)
    assert cases["obj"]["expected_output"] == {"label": "bug"}
    assert cases["num"]["expected_output"] == 42
    assert cases["text"]["expected_output"] == "hello world"
    assert cases["broken"]["expected_output"] == "{not json"
    assert cases["empty"]["expected_output"] is None


@pg
def test_csv_import_for_workflow_suite_validates_input_schema(wf_api):
    client, _, _, tenant_of = wf_api
    _, h = tenant_of()
    wf = publish_demo(client, h)
    suite = workflow_suite(client, h, wf, cases=('{"q": "a"}',))
    good = 'name,input\nok,"{""q"": ""hello""}"\n'
    assert post_csv(client, h, suite, good).status_code == 200
    bad = 'name,input\nfine,"{""q"": ""x""}"\nwrong,"{""q"": 5}"\n'
    r = post_csv(client, h, suite, bad)
    assert r.status_code == 422, r.text
    detail = r.json()["detail"]
    assert detail["code"] == "input_schema_errors" and detail["case"] == "wrong"
    assert detail["errors"][0]["type"] == "json_schema.type"
    assert sorted(cases_of(client, h, suite)) == ["c0", "ok"]  # nothing from the bad file
    # The JSON dataset import checks it too.
    r = client.post(f"{API}/eval-suites/{suite}/import?format=json", headers=h, json={
        "cases": [{"name": "j", "inputs": "not json"}]})
    assert r.status_code == 422 and r.json()["detail"]["case"] == "j"


@pg
def test_csv_import_checks_the_suite_first_and_caps_the_size(api):
    client, _, tenant_of = api
    _, h = tenant_of()
    _, other = tenant_of("bob")
    suite = _suite(client, h, _agent(client, h), cases=())
    assert post_csv(client, other, suite, "name,input\na,b\n").status_code == 404
    assert post_csv(client, h, str(uuid.uuid4()), "garbage").status_code == 404
    assert client.patch(f"{API}/eval-suites/{suite}", json={"archived": True}, headers=h).status_code == 200
    assert post_csv(client, h, suite, "garbage").status_code == 409  # archived, before any parse
    assert client.patch(f"{API}/eval-suites/{suite}", json={"archived": False}, headers=h).status_code == 200
    assert post_csv(client, h, suite, "name,input\na," + "x" * (1024 * 1024)).status_code == 422


@pytest.mark.parametrize(
    "text, row, fragment",
    [
        ("", 1, "empty"),
        ("input\nx\n", 1, "name column is missing"),
        ("name\nx\n", 1, "input column is missing"),
        ("name,input,extra\na,b,c\n", 1, "unknown column"),
        ("name,input,name\na,b,c\n", 1, "twice"),
        ("name,input\na\n", 2, "expected 2 cells"),
        ("name,input\na,b\na,c\n", 3, "earlier row"),
        ("name,input\na,\n", 2, "input"),
        ('name,input,metadata\na,b,"[1]"\n', 2, "object"),
        ('name,input,metadata\na,b,"{bad"\n', 2, "object"),
        ('name,input,metadata\na,b,"{""k"": [1]}"\n', 2, "metadata"),
        ('name,input\na,"unclosed\n', 2, "not valid CSV"),
    ],
)
def test_csv_parse_errors_name_the_row(text, row, fragment):
    with pytest.raises(CsvFormatError) as info:
        parse_csv_cases(text)
    assert info.value.row == row and fragment in info.value.message


def test_csv_parse_accepts_a_bom_case_insensitive_headers_and_blank_lines():
    cases = parse_csv_cases("﻿Name, Input ,Metadata\r\n\r\nrow one,hi,\r\nrow two,yo,\"{\"\"n\"\": 1}\"\r\n")
    assert [(c["name"], c["input"], c["metadata"]) for c in cases] == [("row one", "hi", {}), ("row two", "yo", {"n": 1})]
