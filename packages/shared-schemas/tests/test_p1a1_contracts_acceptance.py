"""RAG P1.A1 acceptance (tester): nothing stored before P1 changes, the reserved key, exact errors.

- Every stored `knowledge_bases.chunking` value of the dev database (a read-only snapshot taken on
  2026-11-15) and every config literal of today's tests round-trips unchanged, and the dumps and
  the agent-spec hashes equal those of `main`'s eap_schemas (loaded with `git show` into a temp dir).
- The reserved metadata/filter key `eap`, the extraction error table and ExtractConfig edges.
"""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
from pydantic import ValidationError

from eap_schemas import AgentSpec, canonical_spec_hash
from eap_schemas.knowledge import (
    EXTRACTION_ERROR_CODES,
    EXTRACTION_ERROR_MESSAGES,
    ChunkingConfig,
    ExtractConfig,
    KnowledgeBaseRef,
    extraction_error_message,
    validate_filter,
    validate_metadata,
)

KB_A = "00000000-0000-4000-8000-000000000001"
KB_B = "00000000-0000-4000-8000-000000000002"
REPO = Path(__file__).resolve().parents[3]

# `select chunking::text from knowledge_bases` (jsonb text: keys by length, then alphabetically).
DEV_DB_CHUNKING = [
    '{"max_tokens": 512, "min_tokens": 40, "overlap_tokens": 64, "chunker_version": 1}',
    '{"max_tokens": 512, "min_tokens": 120, "overlap_tokens": 64, "chunker_version": 1}',
]
# Literals of today's tests (main), with and without chunker_version, as the API/run store receive them.
TEST_CONFIGS = [
    {"max_tokens": 512, "overlap_tokens": 64, "min_tokens": 40, "chunker_version": 1},
    {"max_tokens": 512, "overlap_tokens": 64, "min_tokens": 40},
    {"max_tokens": 128, "overlap_tokens": 16, "min_tokens": 20},
    {"max_tokens": 128, "overlap_tokens": 16, "min_tokens": 10},
    {"max_tokens": 64, "overlap_tokens": 8, "min_tokens": 0},
    {"max_tokens": 64, "overlap_tokens": 0, "min_tokens": 0},
    {"max_tokens": 64, "overlap_tokens": 8, "min_tokens": 4},
    {"max_tokens": 32, "overlap_tokens": 4, "min_tokens": 0},
    {"max_tokens": 512, "overlap_tokens": 64, "min_tokens": 100},
    {"max_tokens": 512},
    {},
]


def _jsonb_text(obj: dict) -> str:
    """Postgres jsonb's text form: keys ordered by length, then bytewise; ', ' and ': ' separators."""
    return "{" + ", ".join(f"{json.dumps(k)}: {json.dumps(obj[k])}" for k in sorted(obj, key=lambda k: (len(k), k))) + "}"


@pytest.mark.parametrize("stored", DEV_DB_CHUNKING)
def test_dev_db_chunking_round_trips_byte_for_byte(stored):
    config = ChunkingConfig.model_validate_json(stored)
    dumped = config.model_dump()
    assert dumped == json.loads(stored) and "extract" not in dumped
    # What the run store would write and Postgres would read back, in jsonb's canonical text.
    assert _jsonb_text(json.loads(json.dumps(dumped))) == stored
    assert json.loads(config.model_dump_json()) == json.loads(stored)
    assert json.dumps(dumped, sort_keys=True) == json.dumps(json.loads(stored), sort_keys=True)
    assert ChunkingConfig(**json.loads(stored)) == config


@pytest.mark.parametrize("stored", DEV_DB_CHUNKING)
def test_stored_chunking_survives_a_second_round_trip_in_json_mode(stored):
    config = ChunkingConfig.model_validate(json.loads(stored))
    again = ChunkingConfig.model_validate(config.model_dump(mode="json"))
    assert again == config and again.model_dump_json() == config.model_dump_json()
    assert config.model_dump(exclude_none=True) == config.model_dump()


# --- the same dumps and hashes as main -------------------------------------------------------------

_PROBE = textwrap.dedent("""
    import json, sys
    sys.path.insert(0, sys.argv[1])
    import eap_schemas
    from eap_schemas import AgentSpec, canonical_spec_hash
    from eap_schemas.knowledge import ChunkingConfig, KnowledgeBaseRef
    data = json.load(sys.stdin)
    out = {"module": eap_schemas.__file__, "chunking": [], "refs": [], "hashes": [], "dumps": []}
    for c in data["chunking"]:
        cfg = ChunkingConfig(**c)
        out["chunking"].append([json.dumps(cfg.model_dump()), cfg.model_dump_json()])
    for r in data["refs"]:
        ref = KnowledgeBaseRef(**r)
        out["refs"].append([json.dumps(ref.model_dump()), ref.model_dump_json()])
    for s in data["specs"]:
        spec = AgentSpec.model_validate(s)
        out["hashes"].append(canonical_spec_hash(spec))
        out["dumps"].append(spec.model_dump_json())
    print(json.dumps(out))
""")

BASE = {"name": "kb agent", "model": {"provider": "test", "name": "echo"}}
SPECS = [
    BASE,
    {**BASE, "instructions": "Open a ticket.", "tools": ["tickets.create", "clock.now"], "model_settings": {"temperature": 0.2}},
    {**BASE, "knowledge_bases": [{"id": KB_A}]},
    {**BASE, "knowledge_bases": [{"id": KB_A, "top_k": 3, "min_score": 0.25, "filter": {"device": "am62x"}}]},
    {**BASE, "knowledge_bases": [
        {"id": KB_A, "filter": {"device": ["am62x", "am64x"], "n": 3, "ok": True}},
        {"id": KB_B, "top_k": 8},
    ]},
]
REFS = [{"id": KB_A}, {"id": KB_A, "top_k": 9, "min_score": 0.5, "filter": {"k": ["a", "b"], "eap_x": 1, "EAP": 2}}]


def _main_schemas(tmp_path: Path) -> Path:
    prefix = "packages/shared-schemas/src/"
    names = subprocess.run(
        ["git", "ls-tree", "-r", "--name-only", "main", "--", prefix], cwd=REPO, check=True, capture_output=True, text=True,
    ).stdout.split()
    assert any(n.endswith("eap_schemas/knowledge.py") for n in names)
    for name in names:
        body = subprocess.run(["git", "show", f"main:{name}"], cwd=REPO, check=True, capture_output=True).stdout
        target = tmp_path / name[len(prefix):]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(body)
    return tmp_path


def _probe(src: Path | None) -> dict:
    payload = json.dumps({"chunking": TEST_CONFIGS + [json.loads(s) for s in DEV_DB_CHUNKING], "refs": REFS, "specs": SPECS})
    argv = [sys.executable, "-c", _PROBE, str(src) if src else str(Path(sys.modules["eap_schemas"].__file__).parents[1])]
    done = subprocess.run(argv, input=payload, capture_output=True, text=True, cwd=REPO, check=True)
    return json.loads(done.stdout)


def test_dumps_and_spec_hashes_equal_main(tmp_path):
    old_src = _main_schemas(tmp_path)
    old, new = _probe(old_src), _probe(None)
    assert Path(old["module"]).is_relative_to(old_src)  # really main's package
    assert not Path(new["module"]).is_relative_to(old_src)
    assert old["chunking"] == new["chunking"]  # byte for byte, both dumps
    assert old["refs"] == new["refs"]
    assert old["hashes"] == new["hashes"] and len(set(old["hashes"])) == len(SPECS)
    assert old["dumps"] == new["dumps"]
    # And the hash computed in this process is main's.
    assert [canonical_spec_hash(AgentSpec.model_validate(s)) for s in SPECS] == old["hashes"]


def test_main_run_event_types_are_all_still_there(tmp_path):
    src = _main_schemas(tmp_path)
    script = "import sys; sys.path.insert(0, sys.argv[1]); import json, typing, eap_schemas as e; print(json.dumps(typing.get_args(e.RunEventType)))"
    old = json.loads(subprocess.run([sys.executable, "-c", script, str(src)], capture_output=True, text=True, check=True).stdout)
    from typing import get_args
    from eap_schemas import RunEventType
    # Main's event set as of the P1 part 1 merge (2026-11-15): main may only grow from here.
    assert set(MAIN_RUN_EVENT_TYPES) <= set(old), "an event type disappeared from main"
    assert [t for t in old if t in MAIN_RUN_EVENT_TYPES] == list(MAIN_RUN_EVENT_TYPES)
    # Nothing main has is gone from this branch, and the order of main's ones is kept.
    assert set(old) <= set(get_args(RunEventType))
    assert [t for t in get_args(RunEventType) if t in old] == old


MAIN_RUN_EVENT_TYPES = (
    "run_started", "text_delta", "model_message", "tool_call", "tool_result", "run_completed", "run_failed",
    "processor_result", "step_started", "step_waiting", "step_approved", "step_rejected", "step_timed_out",
    "step_completed", "step_failed", "ingest_progress", "embedding", "memory_observe_failed", "memory_updated",
    "memory_observe_trimmed", "post_turn_done", "extracted",
)


# --- the reserved key "eap" ------------------------------------------------------------------------


def test_eap_is_rejected_in_metadata_with_the_exact_message():
    for value in ("x", 1, True, 1.5, {"a": 1}, None):
        with pytest.raises(ValueError) as info:
            validate_metadata({"eap": value})
        assert str(info.value) == "metadata key 'eap' is reserved"
    with pytest.raises(ValueError, match="'eap' is reserved"):
        validate_metadata({"device": "am62x", "eap": "x"})  # next to valid keys


def test_eap_is_rejected_in_filters_with_the_exact_message():
    for value in ("x", ["a", "b"], 3, False):
        with pytest.raises(ValueError) as info:
            validate_filter({"eap": value})
        assert str(info.value) == "filter key 'eap' is reserved"
    with pytest.raises(ValueError, match="'eap' is reserved"):
        validate_filter({"device": "am62x", "eap": ["a"]})
    with pytest.raises(ValidationError, match="reserved"):
        KnowledgeBaseRef(id=KB_A, filter={"eap": "x"})


NEAR_NAMES = ["EAP", "Eap", "eAp", "eap_", "_eap", "eap1", "eap.x", "x.eap", "eap-x", "e", "ea", "eapp", "eap" * 21]


@pytest.mark.parametrize("key", NEAR_NAMES)
def test_near_names_are_allowed_in_metadata_and_filters(key):
    assert validate_metadata({key: "v"}) == {key: "v"}
    assert validate_filter({key: "v"}) == {key: "v"}
    assert validate_filter({key: ["v", 2]}) == {key: ("v", 2)}
    ref = KnowledgeBaseRef(id=KB_A, filter={key: 1})
    assert ref.filter == {key: 1}


@pytest.mark.parametrize("key", ["eap ", " eap", "eap\n", "eаp", "", "eap/x"])
def test_invalid_lookalike_keys_fail_the_key_pattern_not_the_reserved_check(key):
    for check in (validate_metadata, validate_filter):
        with pytest.raises(ValueError, match="must match") as info:
            check({key: "v"})
        assert "reserved" not in str(info.value)


def test_agent_spec_hash_input_still_accepts_near_names():
    spec = AgentSpec.model_validate({**BASE, "knowledge_bases": [{"id": KB_A, "filter": {"EAP": "x", "eap_v": 1}}]})
    assert canonical_spec_hash(spec) == canonical_spec_hash(AgentSpec.model_validate(spec.model_dump(mode="json")))


# --- extraction errors -----------------------------------------------------------------------------

EXPECTED_MESSAGES = {
    "extract_failed": "The document could not be extracted.",
    "extract_timeout": "Extraction took too long and was stopped.",
    "extract_memory": "Extraction ran out of memory.",
    "extract_limit": "The document is over an extraction limit (pages, nodes, nesting or image size).",
    "extract_output_too_large": "The extracted text is too large.",
    "document_encrypted": "The PDF is encrypted with a password.",
    "no_extractable_text": "No text could be extracted from the document.",
    "no_text_layer": "Image-only PDF with no text layer: it needs image extraction (re-extract once it is available).",
    "unsupported_content": "The file's content does not match its declared type.",
    "image_model_required": "Images need an image-capable embedding model.",
}


def test_every_code_maps_to_its_exact_message():
    assert dict(EXTRACTION_ERROR_MESSAGES) == EXPECTED_MESSAGES
    assert EXTRACTION_ERROR_CODES == tuple(EXPECTED_MESSAGES)
    for code, message in EXPECTED_MESSAGES.items():
        assert extraction_error_message(code) == message


@pytest.mark.parametrize("code", ["", "nope", "EXTRACT_TIMEOUT", " extract_timeout", "extraction_failed", "extract_timeout "])
def test_an_unknown_code_falls_back_to_extract_failed(code):
    assert extraction_error_message(code) == EXPECTED_MESSAGES["extract_failed"]


def test_messages_carry_no_document_text_paths_or_parser_names():
    for message in EXPECTED_MESSAGES.values():
        assert message.isascii() and "/" not in message and "\n" not in message
        assert not any(w in message.lower() for w in ("pypdf", "docutils", "traceback", "exception"))


# --- ExtractConfig / ChunkingConfig edges ----------------------------------------------------------


def _bad(**kw):
    with pytest.raises(ValidationError):
        ExtractConfig(**kw)


def test_rst_config_size_and_key_boundaries():
    ok = {f"K{i}": i for i in range(64)}
    assert ExtractConfig(rst_config=ok).rst_config == ok
    _bad(rst_config={**ok, "K64": 1})
    assert ExtractConfig(rst_config={"a" * 64: 1}).rst_config == {"a" * 64: 1}
    for key in ("a" * 65, "", "1a", "a-b", "a b", "a.b", "é"):
        _bad(rst_config={key: 1})
    for key in ("_", "_a1", "CONFIG_part_variant", "A"):
        assert ExtractConfig(rst_config={key: 1}).rst_config == {key: 1}


def test_rst_config_value_types_are_kept_and_nested_values_rejected():
    cfg = ExtractConfig(rst_config={"b": True, "i": 1, "f": 1.5, "s": "AM62X", "z": 0, "no": False})
    assert cfg.rst_config == {"b": True, "i": 1, "f": 1.5, "s": "AM62X", "z": 0, "no": False}
    assert [type(v) for v in cfg.rst_config.values()] == [bool, int, float, str, int, bool]
    for value in (None, [1], {"a": 1}, (1,), float("nan"), float("inf"), 2**63, "x" * 257, b"x"):
        _bad(rst_config={"a": value})
    for config in ([("a", 1)], "a=1", 5):
        _bad(rst_config=config)


def test_rst_tags_boundaries():
    assert ExtractConfig(rst_tags=[f"t{i}" for i in range(64)]).rst_tags == tuple(f"t{i}" for i in range(64))
    _bad(rst_tags=[f"t{i}" for i in range(65)])
    assert ExtractConfig(rst_tags=[]).rst_tags == ()
    for tags in ("html", b"html", {"a"}, [1], [""], ["a-b"], ["1a"], ["a" * 65], [None]):
        _bad(rst_tags=tags)


def test_a_non_empty_extract_round_trips_inside_chunking_and_omits_its_none_fields():
    cfg = ChunkingConfig(chunker_version=2, extract={"rst_tags": ["html"], "pdf_headings": "font"})
    dumped = cfg.model_dump(mode="json")
    assert dumped == {
        "max_tokens": 512, "overlap_tokens": 64, "min_tokens": 40, "chunker_version": 2,
        "extract": {"rst_tags": ["html"], "pdf_headings": "font"},
    }
    assert ChunkingConfig.model_validate(dumped) == cfg
    assert json.loads(cfg.model_dump_json()) == dumped
    # Falsy but set values are kept: only None is omitted.
    only_false = ChunkingConfig(extract={"rst_config": {"a": False}, "rst_tags": []})
    assert only_false.model_dump(mode="json")["extract"] == {"rst_config": {"a": False}, "rst_tags": []}


def test_extract_enums_exact_values_and_frozen_models():
    for field, values in {"pdf_headings": ("outline", "font", "pages"), "pdf_images": ("off", "embedded", "pages"),
                          "markdown_flavor": ("plain", "doxygen")}.items():
        for v in values:
            assert getattr(ExtractConfig(**{field: v}), field) == v
        for v in ("OUTLINE", "", "none", 1, True):
            _bad(**{field: v})
    cfg = ChunkingConfig(extract={"pdf_headings": "pages"})
    with pytest.raises(ValidationError):
        cfg.chunker_version = 2
    with pytest.raises(ValidationError):
        cfg.extract.pdf_headings = "font"


@pytest.mark.parametrize("version", [0, -1, 3, 99, 1.5, "2x"])
def test_chunker_version_outside_one_and_two_is_rejected(version):
    with pytest.raises(ValidationError):
        ChunkingConfig(chunker_version=version)


def test_chunking_defaults_stay_v1_and_unknown_keys_are_rejected():
    assert ChunkingConfig().chunker_version == 1 and ChunkingConfig(chunker_version=2).min_tokens == 40
    with pytest.raises(ValidationError):
        ChunkingConfig(max_tokens=512, flavor="x")
