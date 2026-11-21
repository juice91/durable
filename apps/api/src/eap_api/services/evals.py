"""Test suites, cases and eval runs: the business logic behind eap_api.routes.evals.

A service holds what the routes used to do besides HTTP: the target lookups (agent and workflow), the
regex, judge-model, payload and workflow-input checks, the idempotent suite creation and eval-run start
(freeze the suite, record the run, hand it to the worker), paging cursors and the reads. It refuses with
`ServiceError` subclasses; the stores' own errors are translated here, so a route never sees them.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Iterator

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from eap_api import eval_overview
from eap_api.eval_format import (
    MAX_IMPORT_BYTES,
    CsvFormatError,
    EvalFormatError,
    ExportFormat,
    dataset_dict,
    dump_dataset,
    parse_csv_cases,
    parse_file,
    read_dataset,
)
from eap_api.models import AgentModel
from eap_api.ports import RunLauncher
from eap_api.services.agent_archive import refuse_if_archived
from eap_api.services.errors import Conflict, InvalidInput, NotFound, ServiceError, Unavailable
from eap_api.services.run_options import (
    DRAFT,
    RunTarget,
    check_spec_payload_size,
    resolve_target,
    resolve_workflow_target,
    stored_target,
)
from eap_api.services.workflow_inputs import input_schema_errors, non_finite_errors
from eap_api.text_checks import unstorable_strings
from eap_api.workflows.models import WorkflowModel, WorkflowVersionModel
from eap_observability.redaction import log_failure
from eap_processors import Re2PatternError, compile_re2
from eap_run_store import ENQUEUE_FAILED_ERROR, InvalidRunCursor, RunCursor, RunIdConflict
from eap_run_store.cursor import MAX_CURSOR_LENGTH  # noqa: F401 - re-exported for the routes' query limits
from eap_run_store.evals import (  # noqa: F401 - the limits and the dumper are re-exported for the routes
    MAX_LIST_RESULTS,
    MAX_LIST_RUNS,
    EvalCaseLimitExceeded,
    EvalCaseNameConflict,
    EvalCaseNotFound,
    EvalRunNotFound,
    EvalStore,
    EvalSuiteArchived,
    EvalSuiteNameConflict,
    EvalSuiteNotFound,
    EvalSuiteRecord,
    dump_evaluators,
)
from eap_schemas import AgentSpec
from eap_schemas.evals import EvalSuiteInvalid, EvalSuiteSnapshot
from eap_schemas.models_policy import ModelNotAllowed, ModelPolicy
from eap_schemas.tools import TOOL_DECLARATIONS
from eap_schemas.workflows import WorkflowSpec

log = logging.getLogger("eap_api")

AGENT_NOT_FOUND = "Agent not found"
WORKFLOW_NOT_FOUND = "Workflow not found"
RUN_ID_IN_USE = "run_id is already in use"
RUN_ID_OTHER_REQUEST = "run_id is already used by a different run request"
SUITE_ID_IN_USE = "suite_id is already in use"
CASE_ID_IN_USE = "case_id is already in use"
INVALID_CURSOR = "cursor is not valid; pass the previous page's next_cursor verbatim"
ENQUEUE_FAILED_DETAIL = "Eval run recorded but could not be started; retry the same request (same run_id)"
REGEX_INVALID = "pattern is not valid RE2 syntax or is too complex"
OVERVIEW_MAX_LIMIT = 100


@dataclass(frozen=True)
class StartedEvalRun:
    """An eval run recorded and handed to the worker: the run row, whether this call created it, and the
    side-effect warnings of its target (always empty for a workflow)."""

    run: dict[str, Any]
    created: bool
    warnings: list[dict[str, str]]


@contextmanager
def _store_refusals() -> Iterator[None]:
    """The EvalStore's (and the file format's) errors as the refusals the API has always answered."""
    try:
        yield
    except (EvalSuiteNotFound, EvalCaseNotFound, EvalRunNotFound) as exc:
        raise NotFound(str(exc)) from None
    except (EvalSuiteNameConflict, EvalCaseNameConflict, EvalSuiteArchived) as exc:
        raise Conflict(str(exc)) from None
    except CsvFormatError as exc:
        raise InvalidInput(
            exc.message, detail_object={"code": "csv_invalid", "row": exc.row, "message": exc.message}
        ) from None
    except (EvalSuiteInvalid, EvalCaseLimitExceeded, EvalFormatError) as exc:
        raise InvalidInput(str(exc)) from None


# ---------------------------------------------------------------------------- pure checks


def side_effect_warnings(spec: AgentSpec) -> list[dict[str, str]]:
    """The target's tools that run for real during an evaluation: built-in tools declared with
    `side_effect` and every MCP tool (their effects are unknown), unless the spec gates them with
    an approval (a gated call is denied in an eval run, so it never runs)."""
    gated = set(spec.approvals.tools) if spec.approvals is not None else set()
    warnings: list[dict[str, str]] = []
    for tool_id in spec.tools:
        declaration = TOOL_DECLARATIONS.get(tool_id)
        if declaration is not None and declaration.side_effect and tool_id not in gated:
            warnings.append({
                "code": "tool_side_effect",
                "tool": tool_id,
                "kind": "builtin",
                "message": f"Tool {tool_id} has side effects; every case that calls it runs it for real",
            })
    for ref in spec.mcp_tools:
        name = f"{ref.server}/{ref.tool}"
        if name not in gated:
            warnings.append({
                "code": "mcp_tool_side_effect",
                "tool": name,
                "kind": "mcp",
                "message": f"MCP tool {name} may have side effects; every case that calls it runs it for real",
            })
    return warnings


def check_regex_evaluators(evaluators: Any, path: str) -> None:
    """EvalSuiteInvalid (422) naming `path.<i>.pattern` for a `regex` evaluator whose pattern RE2
    refuses (eap_processors.compile_re2, as the worker matches it). The message is fixed: the
    pattern, which RE2's own error text quotes, is never echoed. Models or dicts."""
    for index, evaluator in enumerate(evaluators or ()):
        kind = evaluator.get("kind") if isinstance(evaluator, dict) else getattr(evaluator, "kind", None)
        if kind != "regex":
            continue
        pattern = evaluator.get("pattern") if isinstance(evaluator, dict) else evaluator.pattern
        if not isinstance(pattern, str):
            continue  # the contract's own validation reports it
        try:
            compile_re2(pattern, allow_empty_match=True)
        except Re2PatternError:
            raise EvalSuiteInvalid(REGEX_INVALID, f"{path}.{index}.pattern") from None


def _check_case_regexes(cases: Any, path: str = "cases") -> None:
    for i, case in enumerate(cases or ()):
        evaluators = case.get("evaluators") if isinstance(case, dict) else getattr(case, "evaluators", None)
        name = case.get("name") if isinstance(case, dict) else getattr(case, "name", None)
        check_regex_evaluators(evaluators, f"{path}.{name if isinstance(name, str) else i}.evaluators")


def _check_judge_models(snapshot: EvalSuiteSnapshot) -> None:
    """422 when an llm_judge evaluator names a model outside the platform allowlist."""
    policy = ModelPolicy.from_env()
    for path, evaluators in [("evaluators", snapshot.evaluators)] + [
        (f"cases.{c.name}.evaluators", c.evaluators) for c in snapshot.ordered_cases()
    ]:
        for evaluator in evaluators:
            if evaluator.kind == "llm_judge" and not policy.allows(evaluator.model.provider, evaluator.model.name):
                raise InvalidInput(
                    f"judge model at {path}: {ModelNotAllowed(evaluator.model.provider, evaluator.model.name)}"
                )


def workflow_input_problem(spec: WorkflowSpec | None, case_name: str, text: str) -> InvalidInput | None:
    """A 422 (`{code: "input_schema_errors", case, errors}`) when a workflow case's `input` is not
    JSON text of an object that satisfies the spec's input_schema (compact errors: input path and
    failing keyword, never the value); None when it is fine. `spec` None checks only that it is a
    JSON object (the workflow has no published version yet)."""
    try:
        value = json.loads(text)
    except ValueError:
        errors = [{"loc": ["input"], "msg": "must be JSON text", "type": "json_invalid"}]
    else:
        if not isinstance(value, dict):
            errors = [{"loc": ["input"], "msg": "must be a JSON object", "type": "json_object"}]
        elif spec is None:
            return None
        else:
            errors = non_finite_errors(value) or input_schema_errors(spec.input_schema, value)
    if not errors:
        return None
    return InvalidInput(
        "input_schema_errors",
        detail_object={"code": "input_schema_errors", "case": case_name, "errors": errors},
    )


def _check_case_payloads(target: RunTarget, snapshot: EvalSuiteSnapshot) -> None:
    """413 when a case's child run hand-off (the spec plus the case input) would exceed the run
    payload limit: checked before the eval run exists, for the largest case input. For a workflow
    target, 422 when a case input no longer fits the version's input schema."""
    largest = max(snapshot.cases, key=lambda c: len(c.input.encode("utf-8")))
    check_spec_payload_size(target.spec_dict, largest.input)
    if target.kind == "workflow":
        # Every case input must still satisfy the target version's input schema (it may have changed
        # since the case was written): 422 naming the first failing case, before the run exists.
        for case in snapshot.ordered_cases():
            problem = workflow_input_problem(target.spec, case.name, case.input)
            if problem is not None:
                raise problem


def _same_agent_request(run: dict[str, Any], agent_id: uuid.UUID, suite_id: uuid.UUID, version: Any,
                        max_concurrency: int) -> bool:
    """Whether a retry (same run_id) asks for the eval run as recorded."""
    if run.get("kind") != "eval" or run.get("agent_id") != str(agent_id):
        return False
    if run.get("eval_suite_id") != str(suite_id):
        return False
    if version == DRAFT:
        if run.get("draft_spec") is None:
            return False
    elif run.get("draft_spec") is not None or (version is not None and run.get("agent_version") != version):
        return False
    return _recorded_concurrency(run) == max_concurrency


def _same_workflow_request(run: dict[str, Any], workflow_id: uuid.UUID, suite_id: uuid.UUID, version: Any,
                           max_concurrency: int) -> bool:
    """Whether a retry (same run_id) asks for the workflow eval run as recorded."""
    if run.get("kind") != "eval" or run.get("workflow_id") != str(workflow_id):
        return False
    if run.get("eval_suite_id") != str(suite_id):
        return False
    if version is not None and run.get("workflow_version") != version:
        return False
    return _recorded_concurrency(run) == max_concurrency


def _recorded_concurrency(run: dict[str, Any]) -> Any:
    try:
        return json.loads(run["input"]).get("max_concurrency")
    except (TypeError, ValueError, AttributeError):
        return object()  # never equal to a requested value


def _result_cursor(result: Any) -> str:
    raw = json.dumps({"p": result.position, "c": result.case_id}, separators=(",", ":"))
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def _decode_result_cursor(token: str) -> tuple[int, str]:
    try:
        data = json.loads(base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)))
        position, case_id = data["p"], data["c"]
        if set(data) != {"p", "c"} or not isinstance(position, int) or isinstance(position, bool) or position < 0:
            raise ValueError
        return position, str(uuid.UUID(case_id))
    except (ValueError, TypeError, KeyError, AttributeError, binascii.Error):
        raise InvalidInput(INVALID_CURSOR) from None


def _decode_run_cursor(before: str | None) -> RunCursor | None:
    if before is None:
        return None
    try:
        return RunCursor.decode(before)
    except InvalidRunCursor:
        raise InvalidInput(INVALID_CURSOR) from None


# ---------------------------------------------------------------------------- the service


class EvalService:
    """Suites, cases and eval runs of one tenant at a time (every method takes the tenant id)."""

    def __init__(self, store: EvalStore, db: Session, run_store: Any, launcher: RunLauncher) -> None:
        self.store = store
        self.db = db
        self.run_store = run_store
        self.launcher = launcher

    # ------------------------------------------------------------------------ lookups

    def _agent(self, tenant_id: str, agent_id: uuid.UUID) -> AgentModel:
        agent = (
            self.db.query(AgentModel).filter(AgentModel.id == str(agent_id), AgentModel.tenant_id == tenant_id).first()
        )
        if agent is None:
            raise NotFound(AGENT_NOT_FOUND)
        return agent

    def _workflow(self, tenant_id: str, workflow_id: uuid.UUID) -> WorkflowModel:
        workflow = (
            self.db.query(WorkflowModel)
            .filter(WorkflowModel.id == str(workflow_id), WorkflowModel.tenant_id == tenant_id)
            .first()
        )
        if workflow is None or workflow.archived:
            raise NotFound(WORKFLOW_NOT_FOUND)
        return workflow

    def _target_name(self, tenant_id: str, suite: EvalSuiteRecord) -> str | None:
        names = eval_overview.target_names(self.db, tenant_id, [suite])
        return names.get((suite.target_kind, suite.target_id))

    def _latest_workflow_spec(self, tenant_id: str, workflow_id: str) -> WorkflowSpec | None:
        row = (
            self.db.query(WorkflowVersionModel)
            .filter(WorkflowVersionModel.workflow_id == workflow_id, WorkflowVersionModel.tenant_id == tenant_id)
            .order_by(WorkflowVersionModel.version.desc())
            .first()
        )
        return None if row is None else WorkflowSpec.model_validate(row.spec)

    def _overview_agent(self, tenant_id: str, agent_id: uuid.UUID | None) -> str | None:
        return None if agent_id is None else self._agent(tenant_id, agent_id).id

    # ------------------------------------------------------------------------ suites

    def list_suites(self, tenant_id: str, agent_id: uuid.UUID, *, include_archived: bool) -> list[Any]:
        """The agent's suites by name, with their case counts."""
        self._agent(tenant_id, agent_id)
        return self.store.list_suites(tenant_id, agent_id, include_archived=include_archived)

    def list_workflow_suites(
        self, tenant_id: str, workflow_id: uuid.UUID, *, include_archived: bool
    ) -> tuple[str, list[Any]]:
        """`(workflow name, suites)`."""
        workflow = self._workflow(tenant_id, workflow_id)
        suites = self.store.list_workflow_suites(tenant_id, workflow_id, include_archived=include_archived)
        return workflow.name, suites

    def list_tenant_suites(
        self, tenant_id: str, *, agent_id: uuid.UUID | None, include_archived: bool, limit: int, after: str | None
    ) -> tuple[list[dict[str, Any]], str | None]:
        agent = self._overview_agent(tenant_id, agent_id)
        try:
            return eval_overview.list_suite_overview(
                self.store, self.db, tenant_id, agent_id=agent, include_archived=include_archived, limit=limit,
                after=after,
            )
        except eval_overview.InvalidCursor:
            raise InvalidInput(INVALID_CURSOR) from None

    def list_tenant_eval_runs(
        self, tenant_id: str, *, agent_id: uuid.UUID | None, suite_id: uuid.UUID | None, limit: int,
        before: str | None,
    ) -> tuple[list[dict[str, Any]], str | None]:
        agent = self._overview_agent(tenant_id, agent_id)
        cursor = _decode_run_cursor(before)
        return eval_overview.list_run_overview(
            self.store, self.db, tenant_id, agent_id=agent, suite_id=None if suite_id is None else str(suite_id),
            limit=limit, before=cursor,
        )

    def list_scorer_overview(
        self, tenant_id: str, *, limit: int, after: str | None
    ) -> tuple[list[dict[str, Any]], str | None]:
        try:
            return eval_overview.list_scorer_overview(self.store, self.db, tenant_id, limit=limit, after=after)
        except eval_overview.InvalidCursor:
            raise InvalidInput(INVALID_CURSOR) from None

    def create_suite(
        self, tenant_id: str, kind: str, target_id: uuid.UUID, *, name: str, description: str | None,
        evaluators: Any, suite_id: uuid.UUID | None,
    ) -> tuple[EvalSuiteRecord, str, bool]:
        """`(suite, target name, created)` for an `agent` or `workflow` target. Idempotent by `suite_id`: a
        repeat of the same request (same target and name) answers its suite with `created` False; a
        `suite_id` used otherwise is a Conflict."""
        target_name = (self._agent(tenant_id, target_id) if kind == "agent" else self._workflow(tenant_id, target_id)).name
        existing = self._same_suite(tenant_id, kind, target_id, name, suite_id)
        if existing is not None:
            return existing, target_name, False
        try:
            check_regex_evaluators(evaluators, "evaluators")
            make = self.store.create_suite if kind == "agent" else self.store.create_workflow_suite
            suite = make(tenant_id, target_id, name, description=description, evaluators=evaluators, suite_id=suite_id)
        except (EvalSuiteNameConflict, IntegrityError) as exc:
            # A concurrent identical request may have won the insert: answer its suite. Otherwise the name
            # is taken (the store's message) or the id is used elsewhere.
            existing = self._same_suite(tenant_id, kind, target_id, name, suite_id)
            if existing is not None:
                return existing, target_name, False
            if isinstance(exc, EvalSuiteNameConflict):
                raise Conflict(str(exc)) from None
            raise Conflict(SUITE_ID_IN_USE) from None
        except EvalSuiteInvalid as exc:
            raise InvalidInput(str(exc)) from None
        except (EvalCaseLimitExceeded, EvalFormatError) as exc:
            raise InvalidInput(str(exc)) from None
        return suite, target_name, True

    def _same_suite(
        self, tenant_id: str, kind: str, target_id: uuid.UUID, name: str, suite_id: uuid.UUID | None
    ) -> EvalSuiteRecord | None:
        if suite_id is None:
            return None
        try:
            existing = self.store.get_suite(tenant_id, suite_id)
        except EvalSuiteNotFound:
            return None
        if existing.target_kind != kind or existing.target_id != str(target_id) or existing.name != name:
            raise Conflict(SUITE_ID_IN_USE)
        return existing

    def get_suite(self, tenant_id: str, suite_id: uuid.UUID) -> tuple[EvalSuiteRecord, list[Any], str | None]:
        """`(suite, cases in execution order, target name)`."""
        with _store_refusals():
            suite = self.store.get_suite(tenant_id, suite_id)
            cases = self.store.list_cases(tenant_id, suite_id)
        return suite, cases, self._target_name(tenant_id, suite)

    def suite_revision(self, tenant_id: str, suite_id: uuid.UUID) -> int:
        with _store_refusals():
            return self.store.get_suite(tenant_id, suite_id).revision

    def suite_target(self, tenant_id: str, suite_id: uuid.UUID) -> tuple[str | None, str | None]:
        """`(agent_id, workflow_id)` of the tenant's suite (NotFound when there is none)."""
        with _store_refusals():
            suite = self.store.get_suite(tenant_id, suite_id)
        return suite.agent_id, suite.workflow_id

    def update_suite(
        self, tenant_id: str, suite_id: uuid.UUID, fields: dict[str, Any], *, expected_revision: int | None
    ) -> tuple[EvalSuiteRecord, str | None]:
        """Change the given fields (`name`, `description`, `evaluators`, `archived`); an archived suite
        accepts only `archived: false`. A stale `expected_revision` raises the store's RevisionMismatch."""
        with _store_refusals():
            check_regex_evaluators(fields.get("evaluators"), "evaluators")
            suite = self.store.update_suite(tenant_id, suite_id, expected_revision=expected_revision, **fields)
        return suite, self._target_name(tenant_id, suite)

    def delete_suite(self, tenant_id: str, suite_id: uuid.UUID) -> None:
        with _store_refusals():
            self.store.delete_suite(tenant_id, suite_id)

    # ------------------------------------------------------------------------ cases

    def _check_workflow_case_input(self, tenant_id: str, suite_id: uuid.UUID, name: str, text: str) -> None:
        """For a workflow suite, 422 unless `text` is a JSON object valid for the latest published
        version's input schema (only the object check while the workflow has none). Agent suites: no-op;
        an unknown suite is left to the store's 404."""
        try:
            suite = self.store.get_suite(tenant_id, suite_id)
        except EvalSuiteNotFound:
            return
        if suite.workflow_id is None:
            return
        problem = workflow_input_problem(self._latest_workflow_spec(tenant_id, suite.workflow_id), name, text)
        if problem is not None:
            raise problem

    def list_cases(self, tenant_id: str, suite_id: uuid.UUID) -> list[Any]:
        with _store_refusals():
            return self.store.list_cases(tenant_id, suite_id)

    def add_case(
        self, tenant_id: str, suite_id: uuid.UUID, case: dict[str, Any], *, position: int | None,
        case_id: uuid.UUID | None,
    ) -> Any:
        """Add a case: Conflict for a name already in the suite or an archived suite, InvalidInput for an
        invalid case (for a workflow suite, an input that is not a JSON object valid for the workflow's
        input schema) or past the case cap."""
        try:
            with _store_refusals():
                self._check_workflow_case_input(tenant_id, suite_id, case["name"], case["input"])
                check_regex_evaluators(case.get("evaluators"), "case.evaluators")
                return self.store.add_case(tenant_id, suite_id, case, position=position, case_id=case_id)
        except IntegrityError:
            raise Conflict(CASE_ID_IN_USE) from None

    def get_case(self, tenant_id: str, suite_id: uuid.UUID, case_id: uuid.UUID) -> Any:
        with _store_refusals():
            return self.store.get_case(tenant_id, case_id, suite_id=suite_id)

    def update_case(
        self, tenant_id: str, suite_id: uuid.UUID, case_id: uuid.UUID, fields: dict[str, Any], *,
        expected_revision: int | None,
    ) -> Any:
        """Change the given fields; the merged case is validated. A stale `expected_revision` raises the
        store's RevisionMismatch."""
        with _store_refusals():
            if isinstance(fields.get("input"), str):
                name = fields.get("name")
                if not isinstance(name, str):
                    name = self.store.get_case(tenant_id, case_id, suite_id=suite_id).name
                self._check_workflow_case_input(tenant_id, suite_id, name, fields["input"])
            check_regex_evaluators(fields.get("evaluators"), "case.evaluators")
            return self.store.update_case(
                tenant_id, case_id, suite_id=suite_id, expected_revision=expected_revision, **fields
            )

    def delete_case(self, tenant_id: str, suite_id: uuid.UUID, case_id: uuid.UUID) -> None:
        with _store_refusals():
            self.store.delete_case(tenant_id, case_id, suite_id=suite_id)

    # ------------------------------------------------------------------------ import and export

    def export_suite(self, tenant_id: str, suite_id: uuid.UUID, fmt: str) -> tuple[str, str]:
        """`(suite name, the suite as a pydantic_evals dataset file in `fmt`)`."""
        with _store_refusals():
            suite = self.store.get_suite(tenant_id, suite_id)
            cases = self.store.list_cases(tenant_id, suite_id)
        return suite.name, dump_dataset(dataset_dict(suite.name, suite.evaluators, cases), fmt)

    def import_cases(
        self, tenant_id: str, suite_id: uuid.UUID, text: str, fmt: ExportFormat
    ) -> tuple[Any, EvalSuiteRecord, str | None]:
        """`(result {created, updated}, suite, target name)`. The suite is checked before the file is
        parsed: an unknown or foreign suite is NotFound and an archived one Conflict, whatever the file
        holds. Everything is validated before the one transaction that writes (all or nothing)."""
        with _store_refusals():
            suite = self.store.get_suite(tenant_id, suite_id)
            if suite.archived:
                raise EvalSuiteArchived(suite.id)
            if fmt == "csv":
                cases, evaluators = parse_csv_cases(text), None
                issues = unstorable_strings(cases)
            else:
                data = parse_file(text, fmt)
                issues = unstorable_strings(data)
                if not issues:
                    cases, evaluators = read_dataset(data)
            if issues:
                raise InvalidInput("the file holds strings that cannot be stored", detail_object=issues)
            check_regex_evaluators(evaluators, "evaluators")
            _check_case_regexes(cases)
            if suite.workflow_id is not None:
                spec = self._latest_workflow_spec(tenant_id, suite.workflow_id)
                for case in cases:
                    problem = workflow_input_problem(spec, case["name"], case["input"])
                    if problem is not None:
                        raise problem
            kwargs: dict[str, Any] = {} if evaluators is None else {"evaluators": evaluators}
            result = self.store.upsert_cases(tenant_id, suite_id, cases, keep_case_evaluators=fmt == "csv", **kwargs)
            suite = self.store.get_suite(tenant_id, suite_id)
        return result, suite, self._target_name(tenant_id, suite)

    # ------------------------------------------------------------------------ starting eval runs

    def start_agent_eval_run(
        self, tenant_id: str, agent_id: uuid.UUID, *, run_id: uuid.UUID, suite_id: uuid.UUID, version: Any,
        max_concurrency: int, trace_id: str | None, traceparent: str | None,
    ) -> StartedEvalRun:
        """Record an eval run of the suite against the agent's target (a published version, null: the
        latest, or the draft) and hand it to the worker. Idempotent by `run_id`: a retry of the same
        request is relaunched as recorded, a different request is a Conflict."""
        agent = self._agent(tenant_id, agent_id)
        run = self.run_store.get_run(tenant_id, run_id)
        created = False
        if run is None:
            # As the run routes: a new eval run of an archived agent is a Conflict; a retry of a recorded
            # one (same run_id) is answered and relaunched as recorded.
            refuse_if_archived(agent)
            target = resolve_target(self.db, tenant_id, agent, version)
            with _store_refusals():
                snapshot = self.store.snapshot(tenant_id, suite_id, agent_id=agent_id, max_concurrency=max_concurrency)
            _check_judge_models(snapshot)
            _check_case_payloads(target, snapshot)
            try:
                new_id, created = self.run_store.create_eval_run(
                    tenant_id, agent_id, target.version, suite_id, snapshot.canonical_json(), run_id,
                    trace_id=trace_id, traceparent=traceparent, draft_spec=target.draft_spec,
                )
            except RunIdConflict:
                raise Conflict(RUN_ID_IN_USE) from None
            run = self.run_store.get_run(tenant_id, new_id)
            if run is None:  # pragma: no cover - the row was just written
                raise NotFound("Run not found")
        if not created and not _same_agent_request(run, agent_id, suite_id, version, max_concurrency):
            raise Conflict(RUN_ID_OTHER_REQUEST)
        spec = stored_target(self.db, run).spec  # the recorded target: its draft snapshot or its version
        run = self._launch(tenant_id, run, created)
        return StartedEvalRun(run, created, side_effect_warnings(spec))

    def start_workflow_eval_run(
        self, tenant_id: str, workflow_id: uuid.UUID, *, run_id: uuid.UUID, suite_id: uuid.UUID, version: Any,
        max_concurrency: int, trace_id: str | None, traceparent: str | None,
    ) -> StartedEvalRun:
        """As the agent's, against a published workflow version; `version: "draft"` is InvalidInput."""
        if version == DRAFT:
            raise InvalidInput("a workflow is evaluated at a published version, not a draft")
        workflow = self._workflow(tenant_id, workflow_id)
        run = self.run_store.get_run(tenant_id, run_id)
        created = False
        if run is None:
            target = resolve_workflow_target(self.db, tenant_id, workflow.id, version)
            with _store_refusals():
                snapshot = self.store.snapshot(
                    tenant_id, suite_id, workflow_id=workflow_id, max_concurrency=max_concurrency
                )
            _check_judge_models(snapshot)
            _check_case_payloads(target, snapshot)
            try:
                new_id, created = self.run_store.create_workflow_eval_run(
                    tenant_id, workflow_id, target.version, suite_id, snapshot.canonical_json(), run_id,
                    trace_id=trace_id, traceparent=traceparent,
                )
            except RunIdConflict:
                raise Conflict(RUN_ID_IN_USE) from None
            run = self.run_store.get_run(tenant_id, new_id)
            if run is None:  # pragma: no cover - the row was just written
                raise NotFound("Run not found")
        if not created and not _same_workflow_request(run, workflow_id, suite_id, version, max_concurrency):
            raise Conflict(RUN_ID_OTHER_REQUEST)
        run = self._launch(tenant_id, run, created)
        return StartedEvalRun(run, created, [])

    def _launch(self, tenant_id: str, run: dict[str, Any], created: bool) -> dict[str, Any]:
        """Hand the recorded eval run to the orchestrator (the same rules as launch_recorded_run: a
        retry re-enqueues the recorded run, a failed enqueue marks an owned queued run failed, 503)."""
        run_id = uuid.UUID(str(run["id"]))
        owns_handoff = created or self.run_store.requeue_after_enqueue_failure(tenant_id, run_id)
        try:
            self.launcher.launch_eval(
                run_id=run_id, tenant_id=tenant_id, input_json=run["input"], traceparent=run.get("traceparent")
            )
        except Exception as exc:
            log_failure(
                log, "enqueue failed", exc, code="enqueue_failed", level=logging.ERROR,
                run_id=run["id"], tenant_id=tenant_id,
            )
            if owns_handoff:
                try:
                    self.run_store.mark_enqueue_failed(tenant_id, run_id)
                except Exception as mark_exc:  # noqa: BLE001 - the refusal below is the answer either way
                    log_failure(
                        log, "marking enqueue failure failed", mark_exc, code="enqueue_failed_mark",
                        level=logging.ERROR, run_id=run["id"], tenant_id=tenant_id,
                    )
            raise Unavailable(ENQUEUE_FAILED_DETAIL) from None
        run = self.run_store.get_run(tenant_id, run_id) or run
        if run["error"] == ENQUEUE_FAILED_ERROR:
            run = {**run, "status": "queued", "display_status": "queued", "error": None}
        log.info(
            "eval run %s queued: tenant=%s target=%s suite=%s",
            run["id"], tenant_id, run["agent_id"] or run.get("workflow_id"), run["eval_suite_id"],
        )
        return run

    def target_warnings(self, tenant_id: str, agent_id: uuid.UUID, version: int | str | None) -> tuple[RunTarget, list[dict[str, str]]]:
        """The target an eval run of this agent would use and its side-effect warnings."""
        agent = self._agent(tenant_id, agent_id)
        target = resolve_target(self.db, tenant_id, agent, version)
        return target, side_effect_warnings(target.spec)

    # ------------------------------------------------------------------------ eval runs

    def get_run(self, tenant_id: str, run_id: uuid.UUID) -> dict[str, Any] | None:
        """The tenant's run row (any kind), or None."""
        return self.run_store.get_run(tenant_id, run_id)

    def list_eval_runs(
        self, tenant_id: str, target_id: uuid.UUID, *, workflow: bool, suite_id: uuid.UUID | None, limit: int,
        before: str | None,
    ) -> tuple[list[dict[str, Any]], dict[str, Any], str | None]:
        """`(runs newest first, aggregates by run id, next cursor)` of an agent's (or a workflow's) eval runs."""
        if workflow:
            self._workflow(tenant_id, target_id)
        else:
            self._agent(tenant_id, target_id)
        cursor = _decode_run_cursor(before)
        if workflow:
            runs = self.store.list_runs(tenant_id, target_id, suite_id=suite_id, limit=limit, before=cursor, workflow=True)
        else:
            runs = self.store.list_runs(tenant_id, target_id, suite_id=suite_id, limit=limit, before=cursor)
        aggregates = self.store.aggregates_for_runs(tenant_id, [r["id"] for r in runs])
        next_cursor = RunCursor.after(runs[-1]).encode() if len(runs) == limit else None
        return runs, aggregates, next_cursor

    def get_eval_run(
        self, tenant_id: str, target_id: uuid.UUID, run_id: uuid.UUID, *, workflow: bool
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """`(run, aggregates)`; NotFound when the run is not an eval run of this agent (workflow)."""
        run = self.run_store.get_run(tenant_id, run_id)
        owner = "workflow_id" if workflow else "agent_id"
        if run is None or run.get("kind") != "eval" or run.get(owner) != str(target_id):
            raise NotFound(str(EvalRunNotFound(tenant_id, run_id)))
        with _store_refusals():
            aggregates = self.store.aggregates(
                tenant_id, run_id, **({"workflow_id": target_id} if workflow else {"agent_id": target_id})
            )
        return run, aggregates

    def list_eval_results(
        self, tenant_id: str, target_id: uuid.UUID, run_id: uuid.UUID, *, workflow: bool, limit: int,
        after: str | None,
    ) -> tuple[list[Any], str | None]:
        """`(results by position, next cursor)`, a page at a time."""
        position_after = _decode_result_cursor(after) if after is not None else None
        with _store_refusals():
            results = self.store.list_case_results(
                tenant_id, run_id, after=position_after, limit=limit,
                **({"workflow_id": target_id} if workflow else {"agent_id": target_id}),
            )
        next_cursor = _result_cursor(results[-1]) if len(results) == limit else None
        return results, next_cursor

    def compare(self, tenant_id: str, agent_id: uuid.UUID, base: uuid.UUID, target: uuid.UUID) -> dict[str, Any]:
        """Two eval runs of this agent case by case (NotFound when either is not this agent's eval run)."""
        with _store_refusals():
            return self.store.compare(tenant_id, base, target, agent_id=agent_id)
