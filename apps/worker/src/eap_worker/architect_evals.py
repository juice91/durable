"""The Architect's test-suite (eval suite) tools, in the `architect.` namespace.

Same rules as `architect_tools`: they run in the worker over the app database with the run's tenant, never
through HTTP, and never raise for a problem the model can fix (`{"error": {"code", "message"}}`).

Reads (no approval): `architect.list_eval_suites`, `architect.get_eval_run`.
Gated (a person approves each call): `architect.create_eval_suite`, `architect.add_eval_case`,
`architect.attach_scorer`, `architect.start_eval_run`.

The logic is the platform's own: the suites, cases and runs are `eap_run_store.evals.EvalStore` and
`RunStore` (the same ones the API routes and the MCP control plane call), scorers are validated with the
shared `EvaluatorSpec` contract (`eap_schemas.evals`) and RE2 compile, and a run is enqueued as
`eap.eval_run` exactly as the API's launcher does. Only HTTP, auth and the MCP allowlists stay in the API.

A "scorer" here is a suite evaluator: a check on every case's output. It is what the MCP tool
`scorer.attach` calls an evaluator when it adds one to a suite (`eval.suite.create`'s `evaluators`).
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any

from pydantic import TypeAdapter, ValidationError
from sqlalchemy import text
from sqlalchemy.engine import Engine

from eap_processors import Re2PatternError, compile_re2
from eap_run_store import RunStore
from eap_run_store.evals import (
    EvalCaseLimitExceeded,
    EvalCaseNameConflict,
    EvalStore,
    EvalSuiteArchived,
    EvalSuiteInvalid,
    EvalSuiteNameConflict,
    EvalSuiteNotFound,
    dump_evaluators,
)
from eap_schemas.architect import as_uuid
from eap_schemas.evals import DEFAULT_MAX_CONCURRENCY, MAX_MAX_CONCURRENCY, EvaluatorSpec
from eap_schemas.models_policy import ModelPolicy

log = logging.getLogger("eap_worker.architect_evals")

EVAL_WORKFLOW_NAME = "eap.eval_run"  # mirrors eap_api.launcher.EVAL_WORKFLOW_NAME
EVAL_QUEUE_NAME = "eap-agent-runs"  # mirrors eap_api.launcher.QUEUE_NAME
TARGETS = ("workflow", "agent")
MAX_BATCH_CASES = 20
MAX_RESULTS = 50
_SCORER = TypeAdapter(EvaluatorSpec)


def _error(code: str, message: str, **extra: Any) -> dict[str, Any]:
    return {"error": {"code": code, "message": message, **extra}}


# ------------------------------------------------------------------------------------ resolving


def _target_row(conn: Any, tenant_id: str, target: str, ref: str) -> Any:
    """The tenant's agent or workflow named by slug or uuid (any definition of the tenant, not only the
    Architect's: a test suite changes nothing live), or None."""
    table = "agents" if target == "agent" else "workflows"
    as_id = as_uuid(ref)
    column, key = ("id", as_id) if as_id is not None else ("slug", ref)
    return conn.execute(
        text(f"SELECT id, slug, name, archived FROM {table} WHERE tenant_id = :t AND {column} = :k"),
        {"t": tenant_id, "k": key},
    ).mappings().first()


def _resolve_target(engine: Engine, tenant_id: str, target: Any, ref: Any) -> tuple[Any, dict[str, Any] | None]:
    if target not in TARGETS:
        return None, _error("invalid_target", "target must be 'workflow' or 'agent'.")
    if not isinstance(ref, str) or not ref.strip():
        return None, _error("missing_field", "id is required: the slug or uuid of the " + str(target) + ".")
    with engine.connect() as conn:
        row = _target_row(conn, tenant_id, target, ref.strip())
        if row is None:
            other = "agent" if target == "workflow" else "workflow"
            hint = f" It is a {other}: use target '{other}'." if _target_row(conn, tenant_id, other, ref.strip()) else ""
            return None, _error("not_found", f"No {target} has the id {ref!r}.{hint}")
    if row["archived"]:
        return None, _error("archived", f"The {target} {ref!r} is archived.")
    return row, None


def _suite(engine: Engine, tenant_id: str, suite_id: Any) -> tuple[Any, dict[str, Any] | None]:
    sid = as_uuid(suite_id) if isinstance(suite_id, str) else None
    if sid is None:
        return None, _error("not_found", "suite_id must be the uuid of a test suite (list_eval_suites shows them).")
    try:
        return EvalStore(engine).get_suite(tenant_id, sid), None
    except EvalSuiteNotFound:
        return None, _error("not_found", f"No test suite has the id {suite_id!r}.")


def _suite_json(suite: Any, target_name: str | None = None) -> dict[str, Any]:
    return {
        "id": suite.id,
        "name": suite.name,
        "description": suite.description,
        "target": suite.target_kind,
        "target_id": suite.target_id,
        "target_name": target_name,
        "scorers": dump_evaluators(suite.evaluators),
        "case_count": suite.case_count,
        "archived": suite.archived,
    }


def _target_name(engine: Engine, tenant_id: str, kind: str, target_id: str) -> str | None:
    table = "agents" if kind == "agent" else "workflows"
    with engine.connect() as conn:
        return conn.execute(
            text(f"SELECT name FROM {table} WHERE tenant_id = :t AND id = :i"), {"t": tenant_id, "i": target_id}
        ).scalar()


def _latest_workflow_version(engine: Engine, tenant_id: str, workflow_id: str, version: int | None) -> Any:
    sql = "SELECT version, spec FROM workflow_versions WHERE workflow_id = :w AND tenant_id = :t"
    params: dict[str, Any] = {"w": workflow_id, "t": tenant_id}
    if version is not None:
        sql += " AND version = :v"
        params["v"] = version
    with engine.connect() as conn:
        return conn.execute(text(sql + " ORDER BY version DESC LIMIT 1"), params).first()


def _latest_agent_version(engine: Engine, tenant_id: str, agent_id: str, version: int | None) -> Any:
    sql = "SELECT version FROM agent_versions WHERE agent_id = :a AND tenant_id = :t"
    params: dict[str, Any] = {"a": agent_id, "t": tenant_id}
    if version is not None:
        sql += " AND version = :v"
        params["v"] = version
    with engine.connect() as conn:
        return conn.execute(text(sql + " ORDER BY version DESC LIMIT 1"), params).first()


# ------------------------------------------------------------------------------------ validation


def coerce_object(value: Any, what: str) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """A JSON object from a dict or a JSON string (thinking models sometimes encode the argument)."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return None, _error("invalid_" + what, f"{what} must be a JSON object, not text.")
    if not isinstance(value, dict):
        return None, _error("invalid_" + what, f"{what} must be a JSON object.")
    return value, None


def validate_scorer(scorer: Any) -> tuple[Any, dict[str, Any] | None]:
    """The scorer as a shared `EvaluatorSpec` model, or an `invalid_scorer` error naming the first problems.
    A regex is also compiled as RE2, as the worker matches it (the pattern is never echoed)."""
    obj, err = coerce_object(scorer, "scorer")
    if err is not None:
        return None, err
    try:
        spec = _SCORER.validate_python(obj)
    except ValidationError as exc:
        problems = "; ".join(f"{'.'.join(str(p) for p in e['loc']) or 'scorer'}: {e['msg']}" for e in exc.errors()[:5])
        return None, _error("invalid_scorer", problems + " (kinds: regex, contains, equals_expected, field_equals, schema_valid, max_duration, max_cost, llm_judge)")
    if spec.kind == "regex":
        try:
            compile_re2(spec.pattern, allow_empty_match=True)
        except Re2PatternError:
            return None, _error("invalid_scorer", "regex.pattern is not a valid RE2 pattern.")
    if spec.kind == "llm_judge" and not ModelPolicy.from_env().allows(spec.model.provider, spec.model.name):
        return None, _error("invalid_scorer", f"judge model {spec.model.provider}:{spec.model.name} is not allowed on this platform.")
    return spec, None


def _case_inputs_problem(spec: Any, name: str, inputs: Any, agent: bool = False) -> dict[str, Any] | None:
    from eap_worker.schedule_tick import input_problems

    if not isinstance(inputs, dict):
        return _error("invalid_case", f"case {name!r}: inputs must be a JSON object.", case=name)
    if agent and not isinstance(inputs.get("input"), str):
        return _error("invalid_case", f"case {name!r}: for an agent give inputs {{\"input\": \"<the text sent to the agent>\"}}.", case=name)
    schema = (spec or {}).get("input_schema") if isinstance(spec, dict) else None
    if schema is not None and (problems := input_problems(schema, inputs)):
        return _error("input_schema_errors", f"case {name!r}: inputs do not fit the workflow's input schema.", case=name, errors=problems)
    return None


# ----------------------------------------------------------------------------------------- reads


def list_eval_suites_sync(engine: Engine, tenant_id: str, target: Any, id: Any) -> dict[str, Any]:
    row, err = _resolve_target(engine, tenant_id, target, id)
    if err is not None:
        return err
    store = EvalStore(engine)
    suites = store.list_suites(tenant_id, row["id"]) if target == "agent" else store.list_workflow_suites(tenant_id, row["id"])
    return {"target": target, "id": id, "name": row["name"], "suites": [_suite_json(s, row["name"]) for s in suites]}


def get_eval_run_sync(engine: Engine, tenant_id: str, run_id: Any, results_limit: int = 20) -> dict[str, Any]:
    rid = as_uuid(run_id) if isinstance(run_id, str) else None
    run = RunStore(engine).get_run(tenant_id, uuid.UUID(rid)) if rid else None
    if run is None or run.get("kind") != "eval":
        return _error("not_found", f"No eval run has the id {run_id!r}.")
    store = EvalStore(engine)
    target = {"agent_id": run["agent_id"]} if run.get("agent_id") else {"workflow_id": run["workflow_id"]}
    aggregates = store.aggregates(tenant_id, rid, **target)
    out: dict[str, Any] = {
        "run_id": rid,
        "status": run["status"],
        "suite_id": run.get("eval_suite_id"),
        "version": run.get("workflow_version") if run.get("workflow_id") else run.get("agent_version"),
        "error": run.get("error"),
        "aggregates": aggregates,
    }
    limit = max(0, min(int(results_limit or 0), MAX_RESULTS))
    if limit:
        results = store.list_case_results(tenant_id, rid, limit=limit, **target)
        out["results"] = [
            {
                "case": r.case_name,
                "status": r.status,
                "output": (r.output[:600] if isinstance(r.output, str) else r.output),
                "assertions": r.assertions,
                "scores": r.scores,
                "child_run_id": r.child_run_id,
            }
            for r in results
        ]
    return out


# ----------------------------------------------------------------------------------- the writes


def create_eval_suite_sync(
    engine: Engine, tenant_id: str, target: Any, id: Any, name: Any, description: str | None = None,
    run_id: str | None = None,
) -> dict[str, Any]:
    row, err = _resolve_target(engine, tenant_id, target, id)
    if err is not None:
        return err
    if not isinstance(name, str) or not name.strip():
        return _error("missing_field", "name is required.")
    store = EvalStore(engine)
    try:
        make = store.create_suite if target == "agent" else store.create_workflow_suite
        suite = make(tenant_id, row["id"], name.strip(), description=description)
    except EvalSuiteNameConflict:
        return _error("suite_name_taken", f"The {target} already has a test suite named {name!r}.")
    except EvalSuiteInvalid as exc:
        return _error("invalid_suite", str(exc))
    log.info("architect.create_eval_suite tenant=%s run=%s suite=%s", tenant_id, run_id, suite.id)
    return {"status": "created", **_suite_json(suite, row["name"])}


def normalise_cases(
    name: Any = None, inputs: Any = None, expected: Any = None, metadata: Any = None, cases: Any = None
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """The cases of one call: the `cases` list, or the single case given as name/inputs/expected/metadata."""
    if cases is None:
        cases = [{"name": name, "inputs": inputs, "expected": expected, "metadata": metadata}]
    if isinstance(cases, str):
        try:
            cases = json.loads(cases)
        except ValueError:
            return [], _error("invalid_case", "cases must be a JSON list of {name, inputs, expected?, metadata?}.")
    if not isinstance(cases, list) or not cases:
        return [], _error("invalid_case", "give at least one case: cases [{name, inputs, expected?, metadata?}].")
    if len(cases) > MAX_BATCH_CASES:
        return [], _error("too_many_cases", f"add at most {MAX_BATCH_CASES} cases per call.")
    out = []
    for item in cases:
        item, err = coerce_object(item, "case")
        if err is not None:
            return [], err
        if not isinstance(item.get("name"), str) or not item["name"].strip():
            return [], _error("invalid_case", "every case needs a name.")
        out.append(item)
    return out, None


def add_eval_case_sync(
    engine: Engine, tenant_id: str, suite_id: Any, name: Any = None, inputs: Any = None, expected: Any = None,
    metadata: Any = None, cases: Any = None, run_id: str | None = None,
) -> dict[str, Any]:
    items, err = normalise_cases(name, inputs, expected, metadata, cases)
    if err is not None:
        return err
    suite, err = _suite(engine, tenant_id, suite_id)
    if err is not None:
        return err
    if suite.archived:
        return _error("archived", "The test suite is archived.")
    spec = None
    if suite.target_kind == "workflow":
        version = _latest_workflow_version(engine, tenant_id, suite.target_id, None)
        spec = version.spec if version is not None else None
    store = EvalStore(engine)
    existing = {c.name for c in store.list_cases(tenant_id, suite.id)}
    seen: set[str] = set()
    for item in items:  # check the whole batch first: nothing is added when one case is wrong
        case_name = item["name"].strip()
        if case_name in existing or case_name in seen:
            return _error("case_name_taken", f"The suite already has a case named {case_name!r}.", case=case_name)
        seen.add(case_name)
        if (problem := _case_inputs_problem(spec, case_name, item.get("inputs"), suite.target_kind == "agent")) is not None:
            return problem
    added: list[str] = []
    for item in items:
        case = {
            "name": item["name"].strip(),
            # a workflow case's input is the JSON text of its input object; an agent's is the text it is sent
            "input": item["inputs"]["input"] if suite.target_kind == "agent"
            else json.dumps(item["inputs"], ensure_ascii=False, sort_keys=True),
            "expected_output": item.get("expected"),
            "metadata": item.get("metadata") or {},
        }
        try:
            store.add_case(tenant_id, suite.id, case)
        except (EvalCaseNameConflict, EvalSuiteInvalid, EvalCaseLimitExceeded, EvalSuiteArchived) as exc:
            return _error("invalid_case", f"case {case['name']!r}: {exc}", added=added)
        added.append(case["name"])
    log.info("architect.add_eval_case tenant=%s run=%s suite=%s n=%d", tenant_id, run_id, suite.id, len(added))
    return {"suite_id": suite.id, "status": "added", "added": added, "case_count": suite.case_count + len(added)}


def attach_scorer_sync(
    engine: Engine, tenant_id: str, suite_id: Any, scorer: Any, run_id: str | None = None
) -> dict[str, Any]:
    spec, err = validate_scorer(scorer)
    if err is not None:
        return err
    suite, err = _suite(engine, tenant_id, suite_id)
    if err is not None:
        return err
    store = EvalStore(engine)
    try:
        updated = store.update_suite(tenant_id, suite.id, evaluators=[*suite.evaluators, spec])
    except EvalSuiteArchived:
        return _error("archived", "The test suite is archived.")
    except EvalSuiteInvalid as exc:
        return _error("invalid_scorer", str(exc))
    log.info("architect.attach_scorer tenant=%s run=%s suite=%s kind=%s", tenant_id, run_id, suite.id, spec.kind)
    return {"suite_id": suite.id, "status": "attached", "scorers": dump_evaluators(updated.evaluators)}


def _enqueue(run_id: uuid.UUID, tenant_id: str, input_json: str, traceparent: str | None) -> None:
    """Enqueue `eap.eval_run(run_id, tenant_id, input_json)`, workflow id = run id, as the API's launcher does."""
    from eap_observability import dbos_trace_attributes
    from eap_worker.kb_source_sync import _dbos_client
    from eap_worker.runtime import get_runtime

    options: dict[str, Any] = {
        "queue_name": EVAL_QUEUE_NAME,
        "workflow_name": EVAL_WORKFLOW_NAME,
        "workflow_id": str(run_id),
        "app_version": get_runtime().settings.app_version,
    }
    attributes = dbos_trace_attributes(traceparent)
    if attributes is not None:
        options["attributes"] = attributes
    _dbos_client().enqueue(options, str(run_id), tenant_id, input_json)


def start_eval_run_sync(
    engine: Engine, tenant_id: str, suite_id: Any, version: Any = None, run_id: str | None = None
) -> dict[str, Any]:
    suite, err = _suite(engine, tenant_id, suite_id)
    if err is not None:
        return err
    if version is not None and (isinstance(version, bool) or not isinstance(version, int) or version < 1):
        return _error("invalid_version", "version must be a published version number (1 or more), or left out for the latest.")
    store, runs = EvalStore(engine), RunStore(engine)
    is_workflow = suite.target_kind == "workflow"
    published = (_latest_workflow_version if is_workflow else _latest_agent_version)(engine, tenant_id, suite.target_id, version)
    if published is None:
        what = f"version {version}" if version else "a published version"
        return _error("not_published", f"The {suite.target_kind} has no {what}: publish it first, tests run a published version.")
    try:
        snapshot = store.snapshot(
            tenant_id, suite.id, **({"workflow_id": suite.target_id} if is_workflow else {"agent_id": suite.target_id}),
            max_concurrency=min(DEFAULT_MAX_CONCURRENCY, MAX_MAX_CONCURRENCY),
        )
    except EvalSuiteArchived:
        return _error("archived", "The test suite is archived.")
    except EvalSuiteInvalid as exc:
        return _error("invalid_suite", f"{exc}. Add cases with add_eval_case first." if "no cases" in str(exc) else str(exc))
    if is_workflow:
        for case in snapshot.ordered_cases():  # a case input may no longer fit the version's input schema
            if (problem := _case_inputs_problem(published.spec, case.name, json.loads(case.input))) is not None:
                return problem
    new_id = uuid.uuid4()
    try:
        if is_workflow:
            rid, created = runs.create_workflow_eval_run(
                tenant_id, suite.target_id, published.version, suite.id, snapshot.canonical_json(), new_id)
        else:
            rid, created = runs.create_eval_run(
                tenant_id, suite.target_id, published.version, suite.id, snapshot.canonical_json(), new_id)
    except ValueError as exc:
        return _error("invalid_suite", str(exc))
    run = runs.get_run(tenant_id, rid)
    owns = created or runs.requeue_after_enqueue_failure(tenant_id, rid)
    try:
        _enqueue(rid, tenant_id, run["input"], run.get("traceparent"))
    except Exception:  # noqa: BLE001 - the run is recorded; say it was not queued
        log.error("architect.start_eval_run enqueue failed tenant=%s run=%s", tenant_id, rid, exc_info=True)
        if owns:
            runs.mark_enqueue_failed(tenant_id, rid)
        return _error("enqueue_failed", "The run was recorded but could not be queued; try again in a moment.")
    log.info("architect.start_eval_run tenant=%s architect_run=%s eval_run=%s", tenant_id, run_id, rid)
    return {
        "run_id": str(rid),
        "status": "queued",
        "suite_id": suite.id,
        "version": published.version,
        "cases": len(snapshot.cases),
        "note": "It runs in the background; read it with get_eval_run in a minute.",
    }


# ---------------------------------------------------------------------------- approval summaries


def pure_summary(tool_id: str, args: dict[str, Any]) -> str:
    """The card's wording from the arguments alone."""
    if tool_id == "architect.create_eval_suite":
        return f"Create test suite {args.get('name')!r} for {args.get('target') or 'workflow'} {args.get('id')!r}."
    if tool_id == "architect.add_eval_case":
        items, _ = normalise_cases(args.get("name"), args.get("inputs"), args.get("expected"), args.get("metadata"), args.get("cases"))
        names = ", ".join(repr(str(i.get("name"))) for i in items) or "cases"
        return f"Add {len(items)} test case{'s' if len(items) != 1 else ''} to suite {args.get('suite_id')!r}: {names}."
    if tool_id == "architect.attach_scorer":
        scorer = args.get("scorer")
        scorer = scorer if isinstance(scorer, dict) else (coerce_object(scorer, "scorer")[0] or {})
        return f"Add a {scorer.get('kind', 'scorer')} check{_scorer_detail(scorer)} to suite {args.get('suite_id')!r}: it scores every case's output."
    if tool_id == "architect.start_eval_run":
        version = args.get("version")
        return (
            f"Run test suite {args.get('suite_id')!r} against "
            f"{'version ' + str(version) if version else 'the latest published version'}: it runs the workflow or agent once per case, which uses model calls."
        )
    return tool_id


def _scorer_detail(scorer: dict[str, Any]) -> str:
    kind = scorer.get("kind")
    if kind == "regex":
        return f" ({'forbids' if scorer.get('negate') else 'requires'} /{scorer.get('pattern')}/{' in ' + scorer['field'] if scorer.get('field') else ''})"
    if kind == "max_duration":
        return f" ({scorer.get('seconds')} s at most)"
    if kind in ("field_equals", "contains"):
        return f" ({scorer.get('path') or ''}{' = ' if scorer.get('path') else ''}{json.dumps(scorer.get('value'), ensure_ascii=False)})"
    return ""


def approval_summary(engine: Engine, tenant_id: str, tool_id: str, args: dict[str, Any]) -> str:
    """The pure summary with facts read now: the target's name, the suite's name and case count."""
    base = pure_summary(tool_id, args)
    try:
        if tool_id == "architect.create_eval_suite":
            row, err = _resolve_target(engine, tenant_id, args.get("target"), args.get("id"))
            if err is not None:
                return f"{base} It would be refused: {err['error']['message']}"
            kind = args["target"]
            return f"Create test suite {args.get('name')!r} for {kind} {row['name']!r}." + (
                f" ({args.get('description')})" if args.get("description") else "")
        suite, err = _suite(engine, tenant_id, args.get("suite_id"))
        if err is not None:
            return f"{base} It would be refused: {err['error']['message']}"
        label = f"suite {suite.name!r} ({suite.target_kind} {_target_name(engine, tenant_id, suite.target_kind, suite.target_id)!r})"
        base = base.replace(f"suite {args.get('suite_id')!r}", label)
        if tool_id == "architect.start_eval_run":
            return f"{base[:-1]} ({suite.case_count} case{'s' if suite.case_count != 1 else ''}, {len(suite.evaluators)} scorers)."
    except Exception:  # noqa: BLE001 - a summary is a courtesy; the pure one stands
        log.warning("architect eval approval summary failed tool=%s", tool_id, exc_info=True)
    return base
