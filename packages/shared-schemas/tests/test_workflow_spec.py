"""Tests for WorkflowSpec schema and validation."""

from uuid import uuid4

import pytest

from eap_schemas.workflows import (
    AgentStep,
    ApprovalStep,
    BranchCase,
    BranchStep,
    Condition,
    ParallelStep,
    Step,
    ToolStep,
    WorkflowSpec,
    canonical_workflow_hash,
    iter_refs,
    step_targets,
)


# --- Valid spec examples ---

def test_agent_step_valid():
    """Agent step with $ref to input and version."""
    spec = WorkflowSpec(
        name="Test",
        input_schema={"type": "object"},
        steps=(AgentStep(id="a1", agent_id=uuid4(), version=2, input={"q": {"$ref": "input.q"}}),),
        start="a1",
    )
    assert len(spec.steps) == 1
    assert spec.steps[0].version == 2


def test_tool_step_valid():
    """Tool step with $ref to earlier step output."""
    spec = WorkflowSpec(
        name="Test",
        input_schema={"type": "object"},
        steps=(
            ToolStep(id="t1", tool_id="clock.now", input={"param": {"$ref": "input.param"}}),
        ),
        start="t1",
    )
    assert spec.steps[0].tool_id == "clock.now"


def test_branch_step_valid():
    """Branch step with cases and default."""
    spec = WorkflowSpec(
        name="Test",
        input_schema={"type": "object"},
        steps=(
            BranchStep(
                id="b1",
                cases=(
                    BranchCase(when=Condition(op="eq", left={"$ref": "input.x"}, right=1), next="s1"),
                    BranchCase(when=Condition(op="gt", left={"$ref": "input.x"}, right=2), next="s2"),
                ),
                default="s3",
            ),
            ToolStep(id="s1", tool_id="clock.now"),
            ToolStep(id="s2", tool_id="clock.now"),
            ToolStep(id="s3", tool_id="clock.now"),
        ),
        start="b1",
    )
    assert len(spec.steps[0].cases) == 2


def test_parallel_step_valid():
    """Parallel step with 2-8 branches and join."""
    spec = WorkflowSpec(
        name="Test",
        input_schema={"type": "object"},
        steps=(
            ParallelStep(
                id="p1",
                branches=("b1", "b2", "b3"),
                join="j1",
            ),
            ToolStep(id="b1", tool_id="clock.now"),
            ToolStep(id="b2", tool_id="clock.now"),
            ToolStep(id="b3", tool_id="clock.now"),
            ToolStep(id="j1", tool_id="clock.now"),
        ),
        start="p1",
    )
    assert len(spec.steps[0].branches) == 3


def test_approval_step_valid():
    """Approval step with prompt and timeout."""
    spec = WorkflowSpec(
        name="Test",
        input_schema={"type": "object"},
        steps=(
            ApprovalStep(id="app1", prompt="Approve?", timeout_hours=24),
        ),
        start="app1",
    )
    assert spec.steps[0].prompt == "Approve?"
    assert spec.steps[0].timeout_hours == 24


def test_nested_parallel_block():
    """Parallel inside parallel with nested join."""
    spec = WorkflowSpec(
        name="Test",
        input_schema={"type": "object"},
        steps=(
            ParallelStep(
                id="p1",
                branches=("inner", "solo"),
                join="j1",
            ),
            ParallelStep(
                id="inner",
                branches=("i1", "i2"),
                join="ij1",
            ),
            ToolStep(id="i1", tool_id="clock.now"),
            ToolStep(id="i2", tool_id="clock.now"),
            ToolStep(id="ij1", tool_id="clock.now"),
            ToolStep(id="solo", tool_id="clock.now"),
            ToolStep(id="j1", tool_id="clock.now"),
        ),
        start="p1",
    )
    assert spec.steps[0].id == "p1"


def test_condition_ops():
    """All condition operators."""
    for op in ("eq", "ne", "gt", "lt", "contains"):
        spec = WorkflowSpec(
            name="Test",
            input_schema={"type": "object"},
            steps=(
                BranchStep(
                    id="b1",
                    cases=(BranchCase(when=Condition(op=op, left={"$ref": "input.x"}, right=1), next="n1"),),
                    default="n1",
                ),
                ToolStep(id="n1", tool_id="clock.now"),
            ),
            start="b1",
        )
        assert spec.steps[0].cases[0].when.op == op

    # exists takes no right operand
    spec = WorkflowSpec(
        name="Test",
        input_schema={"type": "object"},
        steps=(
            BranchStep(
                id="b1",
                cases=(BranchCase(when=Condition(op="exists", left={"$ref": "input.x"}), next="n1"),),
                default="n1",
            ),
            ToolStep(id="n1", tool_id="clock.now"),
        ),
        start="b1",
    )
    assert spec.steps[0].cases[0].when.op == "exists"
    assert spec.steps[0].cases[0].when.right is None

    # Operands may be bounded JSON objects and arrays, not only scalars.
    spec = WorkflowSpec(
        name="Test",
        input_schema={"type": "object"},
        steps=(
            BranchStep(
                id="b1",
                cases=(
                    BranchCase(
                        when=Condition(op="eq", left={"$ref": "input.obj"}, right={"a": 1, "b": [True, None]}),
                        next="n1",
                    ),
                    BranchCase(
                        when=Condition(op="contains", left=["a", {"k": [1, 2]}], right={"$ref": "input.tag"}),
                        next="n1",
                    ),
                ),
                default="n1",
            ),
            ToolStep(id="n1", tool_id="clock.now"),
        ),
        start="b1",
    )
    assert spec.steps[0].cases[0].when.right == {"a": 1, "b": [True, None]}
    assert spec.steps[0].cases[1].when.left == ["a", {"k": [1, 2]}]


def test_iter_refs():
    """collect $refs from a step."""
    spec = WorkflowSpec(
        name="Test",
        input_schema={"type": "object"},
        steps=(
            ToolStep(id="a1", tool_id="clock.now", input={"q": {"$ref": "input.q"}}, next="b1"),
            BranchStep(
                id="b1",
                cases=(
                    BranchCase(
                        when=Condition(op="eq", left={"$ref": "input.x"}, right={"$ref": "steps.a1.output.y"}),
                        next="n1",
                    ),
                ),
                default="n1",
            ),
            ToolStep(id="n1", tool_id="clock.now"),
        ),
        start="a1",
    )
    refs = iter_refs(spec.steps[1])
    assert "input.x" in refs
    assert "steps.a1.output.y" in refs


def test_step_targets():
    """Collect all step targets for different step types."""
    spec = WorkflowSpec(
        name="Test",
        input_schema={"type": "object"},
        steps=(
            ToolStep(id="start", tool_id="clock.now", next="p1"),
            ParallelStep(id="p1", branches=("b1", "b2"), join="j1"),
            ToolStep(id="b1", tool_id="clock.now"),  # No next - ends branch
            ToolStep(id="b2", tool_id="clock.now"),  # No next - ends branch
            ToolStep(id="j1", tool_id="clock.now", next="br1"),
            BranchStep(
                id="br1",
                cases=(BranchCase(when=Condition(op="eq", left=1, right=1), next="c1"),),
                default="d1",
            ),
            ToolStep(id="c1", tool_id="clock.now"),
            ToolStep(id="d1", tool_id="clock.now"),
        ),
        start="start",
    )
    p1_targets = step_targets(spec.steps[1])  # p1
    assert set(p1_targets) == {"b1", "b2", "j1"}
    br1_targets = step_targets(spec.steps[5])  # br1
    assert set(br1_targets) == {"c1", "d1"}
    tool_targets = step_targets(spec.steps[2])  # b1 (no next)
    assert tool_targets == []


def test_canonical_hash_stable():
    """Hash is stable under key reordering."""
    spec1 = WorkflowSpec(
        name="Test",
        input_schema={"type": "object", "properties": {"a": {"type": "string"}}},
        steps=(ToolStep(id="t1", tool_id="clock.now"),),
        start="t1",
    )
    spec2 = WorkflowSpec(
        name="Test",
        input_schema={"properties": {"a": {"type": "string"}}, "type": "object"},
        steps=(ToolStep(id="t1", tool_id="clock.now"),),
        start="t1",
    )
    assert canonical_workflow_hash(spec1) == canonical_workflow_hash(spec2)


def test_canonical_hash_changes():
    """Any change in spec changes the hash."""
    spec1 = WorkflowSpec(
        name="Test",
        input_schema={"type": "object"},
        steps=(ToolStep(id="t1", tool_id="clock.now"),),
        start="t1",
    )
    spec2 = WorkflowSpec(
        name="Test",
        input_schema={"type": "object"},
        steps=(ToolStep(id="t2", tool_id="clock.now"),),
        start="t2",
    )
    assert canonical_workflow_hash(spec1) != canonical_workflow_hash(spec2)


# --- Schema rejections ---

def test_duplicate_step_id_rejected():
    with pytest.raises(ValueError, match="duplicate step id"):
        WorkflowSpec(
            name="Test",
            input_schema={"type": "object"},
            steps=(
                ToolStep(id="s1", tool_id="clock.now"),
                ToolStep(id="s1", tool_id="clock.now"),
            ),
            start="s1",
        )


def test_bad_step_id_pattern_rejected():
    with pytest.raises(ValueError, match="step id '0step' must match"):
        WorkflowSpec(
            name="Test",
            input_schema={"type": "object"},
            steps=(ToolStep(id="0step", tool_id="clock.now"),),
            start="0step",
        )


def test_step_id_ending_in_pause_is_rejected():
    with pytest.raises(ValueError, match="step_id_reserved_suffix"):
        WorkflowSpec(
            name="Test",
            input_schema={"type": "object"},
            steps=(ToolStep(id="a_pause", tool_id="clock.now"),),
            start="a_pause",
        )


def test_missing_start_rejected():
    with pytest.raises(ValueError, match="start step 'missing' does not exist"):
        WorkflowSpec(
            name="Test",
            input_schema={"type": "object"},
            steps=(ToolStep(id="s1", tool_id="clock.now"),),
            start="missing",
        )


def test_dangling_next_rejected():
    with pytest.raises(ValueError, match="step 's1': next target 'missing' does not exist"):
        WorkflowSpec(
            name="Test",
            input_schema={"type": "object"},
            steps=(
                ToolStep(id="s1", tool_id="clock.now", next="missing"),
            ),
            start="s1",
        )


def test_dangling_case_target_rejected():
    with pytest.raises(ValueError, match="case 1 target"):
        WorkflowSpec(
            name="Test",
            input_schema={"type": "object"},
            steps=(
                BranchStep(
                    id="b1",
                    cases=(BranchCase(when=Condition(op="eq", left=1, right=1), next="missing"),),
                    default="d1",
                ),
                ToolStep(id="d1", tool_id="clock.now"),
            ),
            start="b1",
        )


def test_dangling_default_rejected():
    with pytest.raises(ValueError, match="default target"):
        WorkflowSpec(
            name="Test",
            input_schema={"type": "object"},
            steps=(
                BranchStep(
                    id="b1",
                    cases=(BranchCase(when=Condition(op="eq", left=1, right=1), next="n1"),),
                    default="missing",
                ),
                ToolStep(id="n1", tool_id="clock.now"),
            ),
            start="b1",
        )


def test_dangling_branch_target_rejected():
    with pytest.raises(ValueError, match="branch target"):
        WorkflowSpec(
            name="Test",
            input_schema={"type": "object"},
            steps=(
                ParallelStep(id="p1", branches=("b1", "missing"), join="j1"),
                ToolStep(id="b1", tool_id="clock.now"),
                ToolStep(id="j1", tool_id="clock.now"),
            ),
            start="p1",
        )


def test_dangling_join_target_rejected():
    with pytest.raises(ValueError, match="join target"):
        WorkflowSpec(
            name="Test",
            input_schema={"type": "object"},
            steps=(
                ParallelStep(id="p1", branches=("b1", "b2"), join="missing"),
                ToolStep(id="b1", tool_id="clock.now"),
                ToolStep(id="b2", tool_id="clock.now"),
            ),
            start="p1",
        )


def test_cycle_rejected():
    with pytest.raises(ValueError, match="workflow graph has a cycle"):
        WorkflowSpec(
            name="Test",
            input_schema={"type": "object"},
            steps=(
                ToolStep(id="s1", tool_id="clock.now", next="s2"),
                ToolStep(id="s2", tool_id="clock.now", next="s1"),
            ),
            start="s1",
        )


def test_unreachable_step_rejected():
    with pytest.raises(ValueError, match="step 'unreachable' is not reachable from start"):
        WorkflowSpec(
            name="Test",
            input_schema={"type": "object"},
            steps=(
                ToolStep(id="s1", tool_id="clock.now"),
                ToolStep(id="unreachable", tool_id="clock.now"),
            ),
            start="s1",
        )


def test_ref_to_later_step_rejected():
    """$ref to a step that comes after the current step. s2 is reachable via next, so this is not an unreachable-step error."""
    with pytest.raises(ValueError, match="refers to step 's2', which does not run before 's1'"):
        WorkflowSpec(
            name="Test",
            input_schema={"type": "object"},
            steps=(
                ToolStep(id="s1", tool_id="clock.now", input={"q": {"$ref": "steps.s2.output.x"}}, next="s2"),
                ToolStep(id="s2", tool_id="clock.now"),
            ),
            start="s1",
        )


def test_ref_to_unknown_step_rejected():
    with pytest.raises(ValueError, match="refers to unknown step 'unknown'"):
        WorkflowSpec(
            name="Test",
            input_schema={"type": "object"},
            steps=(
                ToolStep(id="s1", tool_id="clock.now", input={"q": {"$ref": "steps.unknown.output.x"}}),
            ),
            start="s1",
        )


def test_ref_to_sibling_parallel_branch_rejected():
    """Branches run concurrently, so they cannot $ref each other."""
    with pytest.raises(ValueError, match="refers to step 'b2', which does not run before"):
        WorkflowSpec(
            name="Test",
            input_schema={"type": "object"},
            steps=(
                ParallelStep(id="p1", branches=("b1", "b2"), join="j1"),
                ToolStep(id="b1", tool_id="clock.now", input={"q": {"$ref": "steps.b2.output.x"}}),
                ToolStep(id="b2", tool_id="clock.now"),
                ToolStep(id="j1", tool_id="clock.now"),
            ),
            start="p1",
        )


def test_self_ref_rejected():
    with pytest.raises(ValueError, match="refers to step 's1', which does not run before"):
        WorkflowSpec(
            name="Test",
            input_schema={"type": "object"},
            steps=(
                ToolStep(id="s1", tool_id="clock.now", input={"q": {"$ref": "steps.s1.output.x"}}),
            ),
            start="s1",
        )


def test_malformed_ref_path_rejected():
    with pytest.raises(ValueError, match="invalid \\$ref"):
        WorkflowSpec(
            name="Test",
            input_schema={"type": "object"},
            steps=(
                ToolStep(
                    id="s1",
                    tool_id="clock.now",
                    input={"q": {"$ref": "invalid.path[0]"}},
                ),
            ),
            start="s1",
        )


def test_nested_ref_rejected():
    with pytest.raises(ValueError, match="inside a literal"):
        WorkflowSpec(
            name="Test",
            input_schema={"type": "object"},
            steps=(
                ToolStep(
                    id="s1",
                    tool_id="clock.now",
                    input={"q": {"data": {"$ref": "input.x"}}},
                ),
            ),
            start="s1",
        )


def test_parallel_branch_jumping_past_join_rejected():
    """Branch cannot skip to a step after the join."""
    with pytest.raises(ValueError, match="branch 'b1' jumps past the join"):
        WorkflowSpec(
            name="Test",
            input_schema={"type": "object"},
            steps=(
                ParallelStep(id="p1", branches=("b1", "b2"), join="j1"),
                ToolStep(id="b1", tool_id="clock.now", next="after"),
                ToolStep(id="b2", tool_id="clock.now"),
                ToolStep(id="j1", tool_id="clock.now", next="after"),  # j1 has next, so after IS after join
                ToolStep(id="after", tool_id="clock.now"),
            ),
            start="p1",
        )


def test_two_branches_converging_before_join_rejected():
    """Two branches cannot both reach the same step before the join."""
    with pytest.raises(ValueError, match="both reach step"):
        WorkflowSpec(
            name="Test",
            input_schema={"type": "object"},
            steps=(
                ParallelStep(id="p1", branches=("b1", "b2"), join="j1"),
                ToolStep(id="b1", tool_id="clock.now", next="shared"),
                ToolStep(id="b2", tool_id="clock.now", next="shared"),
                ToolStep(id="shared", tool_id="clock.now"),
                ToolStep(id="j1", tool_id="clock.now"),
            ),
            start="p1",
        )


def test_step_outside_targeting_join_rejected():
    """A step outside the parallel cannot target the join. Every step is reachable from start."""
    with pytest.raises(ValueError, match="step 'outsider' targets join 'j1' of parallel step 'p1'"):
        WorkflowSpec(
            name="Test",
            input_schema={"type": "object"},
            steps=(
                BranchStep(
                    id="entry",
                    cases=(BranchCase(when=Condition(op="eq", left=1, right=1), next="p1"),),
                    default="outsider",
                ),
                ToolStep(id="outsider", tool_id="clock.now", next="j1"),
                ParallelStep(id="p1", branches=("b1", "b2"), join="j1"),
                ToolStep(id="b1", tool_id="clock.now"),
                ToolStep(id="b2", tool_id="clock.now"),
                ToolStep(id="j1", tool_id="clock.now"),
            ),
            start="entry",
        )


def test_too_many_steps_rejected():
    """51 steps exceeds the limit."""
    steps = tuple(ToolStep(id=f"s{i}", tool_id="clock.now") for i in range(51))
    with pytest.raises(ValueError, match="at most 50 steps"):
        WorkflowSpec(
            name="Test",
            input_schema={"type": "object"},
            steps=steps,
            start="s0",
        )


def test_spec_over_64kib_rejected():
    """Spec JSON over 64 KiB."""
    with pytest.raises(ValueError, match="at most 64"):
        WorkflowSpec(
            name="Test",
            input_schema={"type": "object"},
            steps=(ToolStep(id="s1", tool_id="clock.now", input={"big": "x" * (64 * 1024)}),),
            start="s1",
        )


def test_input_schema_not_object_rejected():
    with pytest.raises(ValueError, match="must be a JSON Schema object schema"):
        WorkflowSpec(
            name="Test",
            input_schema={"type": "string"},
            steps=(ToolStep(id="s1", tool_id="clock.now"),),
            start="s1",
        )


def test_timeout_hours_zero_rejected():
    with pytest.raises(ValueError, match="step 'app1': timeout_hours: Input should be greater than or equal to 1"):
        WorkflowSpec(
            name="Test",
            input_schema={"type": "object"},
            steps=(ApprovalStep(id="app1", prompt="Approve?", timeout_hours=0),),
            start="app1",
        )


def test_timeout_hours_169_rejected():
    with pytest.raises(ValueError, match="step 'app1': timeout_hours: Input should be less than or equal to 168"):
        WorkflowSpec(
            name="Test",
            input_schema={"type": "object"},
            steps=(ApprovalStep(id="app1", prompt="Approve?", timeout_hours=169),),
            start="app1",
        )


def test_parallel_with_one_branch_rejected():
    with pytest.raises(ValueError, match="step 'p1': branches: Tuple should have at least 2 items"):
        WorkflowSpec(
            name="Test",
            input_schema={"type": "object"},
            steps=(
                ParallelStep(id="p1", branches=("b1",), join="j1"),
                ToolStep(id="b1", tool_id="clock.now"),
                ToolStep(id="j1", tool_id="clock.now"),
            ),
            start="p1",
        )


def test_parallel_with_nine_branches_rejected():
    branches = tuple(f"b{i}" for i in range(9))
    with pytest.raises(ValueError, match="step 'p1': branches: Tuple should have at most 8 items"):
        WorkflowSpec(
            name="Test",
            input_schema={"type": "object"},
            steps=(ParallelStep(id="p1", branches=branches, join="j1"),),
            start="p1",
        )


def test_exists_with_right_operand_rejected():
    with pytest.raises(ValueError, match="takes no right operand"):
        WorkflowSpec(
            name="Test",
            input_schema={"type": "object"},
            steps=(
                BranchStep(
                    id="b1",
                    cases=(BranchCase(when=Condition(op="exists", left={"$ref": "input.x"}, right=1), next="n1"),),
                    default="n1",
                ),
                ToolStep(id="n1", tool_id="clock.now"),
            ),
            start="b1",
        )


def test_unknown_tool_is_a_schema_value_not_a_catalog_check():
    """Tool existence is checked at publish, not by WorkflowSpec."""
    spec = WorkflowSpec(
        name="Test",
        input_schema={"type": "object"},
        steps=(ToolStep(id="s1", tool_id="unknown.tool"),),
        start="s1",
    )
    assert spec.steps[0].tool_id == "unknown.tool"


def test_unknown_fields_rejected():
    with pytest.raises(ValueError, match="Extra inputs are not permitted"):
        WorkflowSpec(
            name="Test",
            input_schema={"type": "object"},
            steps=(ToolStep(id="s1", tool_id="clock.now"),),
            start="s1",
            bonus=True,
        )
    with pytest.raises(ValueError, match="step 's1': bonus: Extra inputs are not permitted"):
        WorkflowSpec(
            name="Test",
            input_schema={"type": "object"},
            steps=(ToolStep.model_validate({"id": "s1", "type": "tool", "tool_id": "clock.now", "bonus": 1}),),
            start="s1",
        )


def test_nul_and_lone_surrogate_name_the_path():
    base = {
        "input_schema": {"type": "object"},
        "start": "s1",
        "steps": [{"id": "s1", "type": "tool", "tool_id": "clock.now", "input": {}}],
    }
    with pytest.raises(ValueError, match=r"a NUL character \(\\u0000\) at name"):
        WorkflowSpec.model_validate({**base, "name": "bad\x00"})
    with pytest.raises(ValueError, match=r"an unpaired surrogate \(\\ud800\) at name"):
        WorkflowSpec.model_validate({**base, "name": "bad\ud800"})


def test_agent_step_uuid_validation():
    """agent_id must be a valid UUID, and the message names the step."""
    with pytest.raises(ValueError, match="step 's1': agent_id: Input should be a valid UUID"):
        AgentStep(id="s1", agent_id="not-a-uuid")


# --------------------------------------------------------------------------- input steps (W2.1)


def _input_spec(schema, *, in_loop=False):
    step = {"id": "ask", "type": "input", "prompt": "Details?", "timeout_hours": 1, "resume_schema": schema}
    return {"name": "W", "input_schema": {"type": "object"}, "start": "ask", "steps": [step]}


def test_input_step_validates_its_resume_schema():
    from eap_schemas.workflows import InputStep, WorkflowSpec

    ok = WorkflowSpec.model_validate(_input_spec({"type": "object", "properties": {"a": {"type": "string"}}}))
    assert isinstance(ok.steps[0], InputStep)
    for bad in ({"type": "string"}, {"type": "object", "properties": 5},
                {"type": "object", "properties": {"a": {"$ref": "http://x/y"}}}):
        with pytest.raises(Exception):
            WorkflowSpec.model_validate(_input_spec(bad))
    with pytest.raises(Exception):  # an input step is a person's data form: no input in a loop body
        WorkflowSpec.model_validate({
            "name": "W", "input_schema": {"type": "object"}, "start": "f",
            "steps": [{"id": "f", "type": "foreach", "items": {"$ref": "input.xs"}, "concurrency": 1, "max_items": 2,
                       "body": {"start": "ask", "steps": [_input_spec({"type": "object"})["steps"][0]]}}],
        })
