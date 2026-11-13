"""Compiled processor chains and their deterministic execution.

`compile_processors(specs)` turns a spec's processor list into compiled processors (RE2 patterns
come from a bounded per-pattern cache, so a spec's patterns are compiled once per process). Each
deterministic processor's `apply(text, budget)` returns the processed text (None when it blocks) and a
`ProcessorOutcome`: the processor type, the action taken and match counts, **never the matched
text**. `moderation` calls a model, so it only compiles here (the worker runs it).

Bounds (every one fails closed, as a block with a `reason`):
- text longer than MAX_PROCESSOR_INPUT_CHARS is blocked by `pii_redact` and text longer than
  MAX_REGEX_INPUT_CHARS by `regex_blocklist` (`input_too_large`): with tenant patterns of up to
  MAX_PROGRAM_SIZE instructions, one uninterruptible search on 128 Ki characters stays near 1 s;
- one chain has CHAIN_TIME_BUDGET_SECONDS, checked before every search and after every
  processor's searches (`budget_exceeded`), so a chain ends within the budget plus one search and
  never allows text after its deadline;
- `regex_blocklist` with action redact finds at most MAX_REDACTED_MATCHES matches per chain
  (`too_many_matches`). With action block it runs one search per pattern (linear: it stops at the
  first match), so its counts are `pattern_<i>: 1` for each pattern that matched.
Finding every match is O(n * matches) for some patterns (`a+b|a`), which is why these exist.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from eap_schemas.processors import (
    MaxLengthConfig,
    MaxLengthProcessor,
    ModerationProcessor,
    PiiRedactConfig,
    PiiRedactProcessor,
    ProcessorAction,
    ProcessorPhase,
    RegexBlocklistConfig,
    RegexBlocklistProcessor,
)

from eap_processors.pii import detect
from eap_processors.regex import (
    BUDGET_EXCEEDED,
    TOO_MANY_MATCHES,
    MatchBudget,
    MatchBudgetExceeded,
    Re2PatternError,
    Regexp,
    compile_re2,
    first_span,
    iter_spans,
)

MAX_PROCESSOR_INPUT_CHARS = 1_000_000
MAX_REGEX_INPUT_CHARS = 128 * 1024
MAX_REDACTED_MATCHES = 1_000
CHAIN_TIME_BUDGET_SECONDS = 5.0
REGEX_REDACTION_MARKER = "[REDACTED:regex]"
INPUT_TOO_LARGE = "input_too_large"
# Moderation (run by the worker) fails closed with these: its model is no longer on the allowlist
# when the check runs, or its answer is not a valid ModerationVerdict.
MODERATION_MODEL_NOT_ALLOWED = "model_not_allowed"
MODERATION_VERDICT_INVALID = "verdict_invalid"
# Every `reason` an outcome can carry (served as-is by the API; anything else is dropped).
OUTCOME_REASONS: frozenset[str] = frozenset(
    {INPUT_TOO_LARGE, BUDGET_EXCEEDED, TOO_MANY_MATCHES, MODERATION_MODEL_NOT_ALLOWED, MODERATION_VERDICT_INVALID}
)


def new_budget(seconds: float = CHAIN_TIME_BUDGET_SECONDS) -> MatchBudget:
    """The budget one processor chain shares (a deadline and the redaction match cap)."""
    return MatchBudget(seconds, MAX_REDACTED_MATCHES)


def pii_marker(kind: str) -> str:
    return f"[REDACTED:{kind}]"


class InvalidProcessorConfig(ValueError):
    """A processor config that cannot run (an RE2 pattern that does not compile). `location` names
    it, e.g. `input_processors[1].config.patterns[0]`. Pickle-safe (raised inside worker steps)."""

    def __init__(self, location: str, reason: str) -> None:
        super().__init__(location, reason)
        self.location = location
        self.reason = reason

    def __str__(self) -> str:
        return f"{self.location}: {self.reason}"


class ProcessorNeedsModel(RuntimeError):
    """`apply` was called on a processor that calls a model (moderation). Pickle-safe."""

    def __init__(self, processor_type: str) -> None:
        super().__init__(processor_type)
        self.processor_type = processor_type

    def __str__(self) -> str:
        return f"processor '{self.processor_type}' calls a model and runs in the worker"


@dataclass(frozen=True)
class ProcessorOutcome:
    """What one processor did. `action` is the action taken: `allow` (nothing to do), `redact`,
    `truncate` or `block`. `match_counts` counts matches by PII kind (`pii_redact`), by pattern
    position `pattern_<i>` (`regex_blocklist`; 1 = matched when the action is block) or
    `chars_over_limit` (`max_length`); zero counts are omitted. `reason` is set when a block is not
    a match (`input_too_large`, `budget_exceeded`, `too_many_matches`). No matched text."""

    processor_type: str
    action: ProcessorAction
    match_counts: Mapping[str, int] = field(default_factory=dict)
    reason: str | None = None

    @property
    def blocked(self) -> bool:
        return self.action == "block"

    def to_json(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "processor_type": self.processor_type,
            "action": self.action,
            "match_counts": dict(self.match_counts),
        }
        if self.reason is not None:
            data["reason"] = self.reason
        return data

    def event_data(self, phase: ProcessorPhase, index: int) -> dict[str, Any]:
        """The `data` of the `processor_result` event (key `processor:{phase}:{index}`)."""
        return {"phase": phase, "index": index, **self.to_json()}


@dataclass(frozen=True)
class ProcessorResult:
    """The processed text (None when blocked: blocked text must not be passed on) and the outcome."""

    text: str | None
    outcome: ProcessorOutcome

    @property
    def blocked(self) -> bool:
        return self.outcome.blocked


class CompiledProcessor:
    """A processor ready to run. Deterministic ones implement `apply`."""

    processor_type: str = ""
    deterministic: bool = True

    def apply(self, text: str, budget: MatchBudget | None = None) -> ProcessorResult:  # pragma: no cover
        """Run on `text`. `budget` is shared by a chain; None gives this call a fresh one."""
        raise NotImplementedError


def _refused(processor_type: str, reason: str) -> ProcessorResult:
    return ProcessorResult(None, ProcessorOutcome(processor_type, "block", {}, reason))


def _too_large(processor_type: str, text: str, limit: int = MAX_PROCESSOR_INPUT_CHARS) -> ProcessorResult | None:
    if len(text) > limit:
        return _refused(processor_type, INPUT_TOO_LARGE)
    return None


def _replace_spans(text: str, spans: Iterable[tuple[int, int, str]]) -> str:
    """Replace sorted, non-overlapping (start, end, marker) spans."""
    out: list[str] = []
    position = 0
    for start, end, marker in spans:
        out.append(text[position:start])
        out.append(marker)
        position = end
    out.append(text[position:])
    return "".join(out)


class PiiRedact(CompiledProcessor):
    processor_type = "pii_redact"

    def __init__(self, config: PiiRedactConfig) -> None:
        self.config = config

    def apply(self, text: str, budget: MatchBudget | None = None) -> ProcessorResult:
        if (too_large := _too_large(self.processor_type, text)) is not None:
            return too_large
        budget = budget or new_budget()
        try:
            # The PII scans are linear (bounded candidate patterns): only the deadline applies.
            detections = detect(text, self.config.kinds, _TimeOnly(budget))
            budget.check_time()
        except MatchBudgetExceeded as exc:
            return _refused(self.processor_type, exc.reason)
        if not detections:
            return ProcessorResult(text, ProcessorOutcome(self.processor_type, "allow"))
        counts: dict[str, int] = {}
        for d in detections:
            counts[d.kind] = counts.get(d.kind, 0) + 1
        if self.config.action == "block":
            return ProcessorResult(None, ProcessorOutcome(self.processor_type, "block", counts))
        redacted = _replace_spans(text, ((d.start, d.end, pii_marker(d.kind)) for d in detections))
        return ProcessorResult(redacted, ProcessorOutcome(self.processor_type, "redact", counts))


class RegexBlocklist(CompiledProcessor):
    processor_type = "regex_blocklist"

    def __init__(self, config: RegexBlocklistConfig, patterns: Sequence[Regexp]) -> None:
        self.config = config
        self.patterns = tuple(patterns)

    def apply(self, text: str, budget: MatchBudget | None = None) -> ProcessorResult:
        if (too_large := _too_large(self.processor_type, text, MAX_REGEX_INPUT_CHARS)) is not None:
            return too_large
        budget = budget or new_budget()
        if self.config.action == "block":
            return self._block(text, budget)
        counts: dict[str, int] = {}
        spans: list[tuple[int, int]] = []
        try:
            for i, regexp in enumerate(self.patterns):
                before = len(spans)
                spans.extend(iter_spans(regexp, text, budget))
                if len(spans) > before:
                    counts[f"pattern_{i}"] = len(spans) - before
            budget.check_time()
        except MatchBudgetExceeded as exc:
            return _refused(self.processor_type, exc.reason)
        if not spans:
            return ProcessorResult(text, ProcessorOutcome(self.processor_type, "allow"))
        # Matches of different patterns can overlap: redact their union.
        spans.sort()
        merged: list[list[int]] = []
        for start, end in spans:
            if merged and start <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], end)
            else:
                merged.append([start, end])
        redacted = _replace_spans(text, ((s, e, REGEX_REDACTION_MARKER) for s, e in merged))
        return ProcessorResult(redacted, ProcessorOutcome(self.processor_type, "redact", counts))

    def _block(self, text: str, budget: MatchBudget) -> ProcessorResult:
        """One search per pattern (linear); counts are 1 per pattern that matched."""
        counts: dict[str, int] = {}
        try:
            for i, regexp in enumerate(self.patterns):
                if first_span(regexp, text, _TimeOnly(budget)) is not None:
                    counts[f"pattern_{i}"] = 1
            budget.check_time()
        except MatchBudgetExceeded as exc:
            return _refused(self.processor_type, exc.reason)
        if not counts:
            return ProcessorResult(text, ProcessorOutcome(self.processor_type, "allow"))
        return ProcessorResult(None, ProcessorOutcome(self.processor_type, "block", counts))


class _TimeOnly(MatchBudget):
    """A view of a chain budget that applies its deadline (checks go to the chain budget) but not
    its redaction match cap."""

    def __init__(self, budget: MatchBudget) -> None:
        self.budget = budget
        self.deadline = budget.deadline
        self.max_matches = None
        self.matches = 0
        self.last_passed_check = None

    def check_time(self) -> None:
        self.budget.check_time()

    def count_match(self) -> None:
        self.matches += 1


class MaxLength(CompiledProcessor):
    processor_type = "max_length"

    def __init__(self, config: MaxLengthConfig) -> None:
        self.config = config

    def apply(self, text: str, budget: MatchBudget | None = None) -> ProcessorResult:
        over = len(text) - self.config.max_chars
        if over <= 0:
            return ProcessorResult(text, ProcessorOutcome(self.processor_type, "allow"))
        counts = {"chars_over_limit": over}
        if self.config.action == "block":
            return ProcessorResult(None, ProcessorOutcome(self.processor_type, "block", counts))
        return ProcessorResult(text[: self.config.max_chars], ProcessorOutcome(self.processor_type, "truncate", counts))


class Moderation(CompiledProcessor):
    """Compiles only: the verdict comes from a model call in the worker (see `ModerationVerdict`)."""

    processor_type = "moderation"
    deterministic = False

    def __init__(self, spec: ModerationProcessor) -> None:
        self.spec = spec
        self.config = spec.config

    def apply(self, text: str, budget: MatchBudget | None = None) -> ProcessorResult:
        raise ProcessorNeedsModel(self.processor_type)


def _compile_one(spec: Any, location: str) -> CompiledProcessor:
    if isinstance(spec, PiiRedactProcessor):
        return PiiRedact(spec.config)
    if isinstance(spec, RegexBlocklistProcessor):
        compiled = []
        for i, pattern in enumerate(spec.config.patterns):
            try:
                compiled.append(compile_re2(pattern))
            except Re2PatternError as exc:
                raise InvalidProcessorConfig(f"{location}.config.patterns[{i}]", exc.reason) from None
        return RegexBlocklist(spec.config, compiled)
    if isinstance(spec, MaxLengthProcessor):
        return MaxLength(spec.config)
    if isinstance(spec, ModerationProcessor):
        return Moderation(spec)
    raise InvalidProcessorConfig(location, f"unknown processor {type(spec).__name__}")


def compile_processors(specs: Sequence[Any], location: str = "processors") -> tuple[CompiledProcessor, ...]:
    """Compile a processor list (eap_schemas ProcessorSpec models), or raise InvalidProcessorConfig
    naming the first invalid entry as `<location>[i]...`."""
    return tuple(_compile_one(spec, f"{location}[{i}]") for i, spec in enumerate(specs))


def compiled_chain(specs: Sequence[Any], location: str = "processors") -> tuple[CompiledProcessor, ...]:
    """`compile_processors`. Kept as the runtime entry point: compiling a chain is cheap because the
    RE2 patterns come from the bounded per-pattern cache (no chain-level cache pins them)."""
    return compile_processors(specs, location)


def processor_errors(input_processors: Sequence[Any], output_processors: Sequence[Any]) -> list[str]:
    """Every reason an agent spec's processors cannot run, as `<field>[i].config...: <reason>`
    (empty when they all compile). Used by publish, restore and draft-run validation."""
    errors: list[str] = []
    for name, specs in (("input_processors", input_processors), ("output_processors", output_processors)):
        for i, spec in enumerate(specs):
            try:
                _compile_one(spec, f"{name}[{i}]")
            except InvalidProcessorConfig as exc:
                errors.append(str(exc))
    return errors


@dataclass(frozen=True)
class ChainResult:
    """Result of running deterministic processors in order. `text` is None when one blocked;
    `outcomes` holds one outcome per processor that ran (processing stops at the first block)."""

    text: str | None
    outcomes: tuple[ProcessorOutcome, ...]

    @property
    def blocked(self) -> bool:
        return bool(self.outcomes) and self.outcomes[-1].blocked


def apply_deterministic(
    processors: Sequence[CompiledProcessor], text: str, budget: MatchBudget | None = None
) -> ChainResult:
    """Run deterministic processors in list order, each on the previous one's output, stopping at
    the first block. One budget (deadline, redaction match cap) covers the whole chain. Raises
    ProcessorNeedsModel if the list contains moderation."""
    budget = budget or new_budget()
    outcomes: list[ProcessorOutcome] = []
    current: str | None = text
    for processor in processors:
        assert current is not None
        result = processor.apply(current, budget)
        outcomes.append(result.outcome)
        current = result.text
        if result.blocked:
            return ChainResult(None, tuple(outcomes))
    return ChainResult(current, tuple(outcomes))
