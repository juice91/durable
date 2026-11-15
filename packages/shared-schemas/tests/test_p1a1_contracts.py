"""RAG P1.A1 contracts in eap_schemas: ChunkingConfig.extract and chunker v2, the reserved metadata
key `eap`, the chunk extraction record, the `extracted` event and the extraction error codes."""

from __future__ import annotations

import json
from typing import get_args

import pytest
from pydantic import ValidationError

from eap_schemas import AgentSpec, RunEventType
from eap_schemas.knowledge import (
    CHUNKER_VERSION,
    CHUNKER_VERSIONS,
    EXTRACTION_ERROR_CODES,
    EXTRACTION_ERROR_MESSAGES,
    EXTRACTOR_IDS,
    INGEST_FAILURE_CODES,
    LATEST_CHUNKER_VERSION,
    RESERVED_METADATA_KEY,
    ChunkExtractMetadata,
    ChunkingConfig,
    ExtractConfig,
    ExtractedEventData,
    KnowledgeBaseRef,
    extraction_error_message,
    new_knowledge_base_chunking,
    validate_filter,
    validate_metadata,
)

KB_ID = "00000000-0000-4000-8000-000000000001"

# Shapes stored in knowledge_bases.chunking before P1 (the dev database holds only these four keys).
STORED_CHUNKING = [
    {"max_tokens": 512, "overlap_tokens": 64, "min_tokens": 40, "chunker_version": 1},
    {"max_tokens": 128, "overlap_tokens": 0, "min_tokens": 0, "chunker_version": 1},
    {"max_tokens": 4096, "overlap_tokens": 2048, "min_tokens": 4095, "chunker_version": 1},
]


@pytest.mark.parametrize("stored", STORED_CHUNKING)
def test_stored_chunking_round_trips_byte_for_byte(stored):
    text = json.dumps(stored)
    config = ChunkingConfig.model_validate(json.loads(text))
    # The run store writes json.dumps(config.model_dump()).
    assert json.dumps(config.model_dump()) == text
    assert config.extract is None
    assert json.loads(config.model_dump_json()) == stored


def test_default_chunking_is_unchanged():
    assert CHUNKER_VERSION == 1
    assert ChunkingConfig().model_dump() == {
        "max_tokens": 512, "overlap_tokens": 64, "min_tokens": 40, "chunker_version": 1,
    }


@pytest.mark.parametrize("extract", [{}, None, {"rst_config": None, "pdf_headings": None}])
def test_empty_extract_is_dropped(extract):
    config = ChunkingConfig(extract=extract)
    assert config.extract is None
    assert "extract" not in config.model_dump()
    assert config == ChunkingConfig()


def test_extract_fields_omitted_while_none():
    config = ChunkingConfig(extract={"markdown_flavor": "doxygen"})
    assert config.model_dump()["extract"] == {"markdown_flavor": "doxygen"}
    full = {
        "rst_config": {"CONFIG_part_variant": "AM62X", "n": 3, "flag": True, "x": 1.5},
        "rst_tags": ["html", "am62x"],
        "pdf_headings": "font",
        "pdf_images": "embedded",
        "markdown_flavor": "plain",
    }
    dumped = json.loads(json.dumps(ChunkingConfig(extract=full).model_dump()))
    assert dumped["extract"] == full
    assert ChunkingConfig.model_validate(dumped).model_dump() == ChunkingConfig(extract=full).model_dump()


@pytest.mark.parametrize("bad", [
    {"a": {"nested": 1}}, {"a": [1]}, {"a": None}, {"bad-key": 1}, {"1x": 1}, {"": 1},
    {"a": "x" * 257}, {"a": float("nan")}, ["a"], "a=1",
    {f"k{i}": i for i in range(65)},
])
def test_rst_config_accepts_only_flat_scalars(bad):
    with pytest.raises(ValidationError):
        ExtractConfig(rst_config=bad)


def test_rst_config_keeps_scalar_types():
    cfg = ExtractConfig(rst_config={"a": True, "b": 1, "c": 1.5, "d": "s"})
    assert cfg.rst_config == {"a": True, "b": 1, "c": 1.5, "d": "s"}
    assert type(cfg.rst_config["a"]) is bool and type(cfg.rst_config["b"]) is int


@pytest.mark.parametrize("bad", ["html", ["has space"], [1], ["a-b"], [f"t{i}" for i in range(65)]])
def test_rst_tags_rejects_bad_values(bad):
    with pytest.raises(ValidationError):
        ExtractConfig(rst_tags=bad)


@pytest.mark.parametrize("field,value", [
    ("pdf_headings", "toc"), ("pdf_images", "all"), ("markdown_flavor", "gfm"), ("unknown", 1),
])
def test_extract_enums_and_unknown_fields_rejected(field, value):
    with pytest.raises(ValidationError):
        ExtractConfig(**{field: value})


@pytest.mark.parametrize("version", CHUNKER_VERSIONS)
def test_chunker_versions_one_and_two(version):
    assert ChunkingConfig(chunker_version=version).chunker_version == version


@pytest.mark.parametrize("version", [0, 3, -1])
def test_other_chunker_versions_rejected(version):
    with pytest.raises(ValidationError):
        ChunkingConfig(chunker_version=version)


def test_new_knowledge_base_chunking_uses_v2():
    assert LATEST_CHUNKER_VERSION == 2
    cfg = new_knowledge_base_chunking()
    assert (cfg.chunker_version, cfg.min_tokens, cfg.max_tokens) == (2, 64, 512)
    assert new_knowledge_base_chunking(max_tokens=256, min_tokens=10).model_dump() == {
        "max_tokens": 256, "overlap_tokens": 64, "min_tokens": 10, "chunker_version": 2,
    }
    # v2's min_tokens is capped below max_tokens; v1 keeps v1's default; explicit values win.
    assert new_knowledge_base_chunking(max_tokens=64, overlap_tokens=16).model_dump() == {
        "max_tokens": 64, "overlap_tokens": 16, "min_tokens": 63, "chunker_version": 2,
    }
    assert new_knowledge_base_chunking(max_tokens=65, overlap_tokens=0).min_tokens == 64
    assert new_knowledge_base_chunking(max_tokens=32, overlap_tokens=0).min_tokens == 31
    assert new_knowledge_base_chunking(chunker_version=1).model_dump() == ChunkingConfig().model_dump()
    assert new_knowledge_base_chunking(chunker_version=1, max_tokens=256).min_tokens == 40
    assert new_knowledge_base_chunking(max_tokens=64, min_tokens=5, overlap_tokens=0).min_tokens == 5


def test_rst_config_is_hashable_and_dumps_as_a_flat_object():
    import copy
    import pickle

    cfg = ChunkingConfig(extract={"rst_config": {"CONFIG_part_variant": "AM62X", "n": 2}, "rst_tags": ["html"]})
    assert hash(cfg) == hash(ChunkingConfig.model_validate(json.loads(json.dumps(cfg.model_dump()))))
    assert cfg.extract.rst_config == {"CONFIG_part_variant": "AM62X", "n": 2}
    assert json.dumps(cfg.model_dump()["extract"]["rst_config"]) == '{"CONFIG_part_variant": "AM62X", "n": 2}'
    assert type(cfg.model_dump()["extract"]["rst_config"]) is dict
    assert pickle.loads(pickle.dumps(cfg)) == cfg and copy.deepcopy(cfg) == cfg
    with pytest.raises(TypeError):
        cfg.extract.rst_config["x"] = 1
    with pytest.raises(TypeError):
        cfg.extract.rst_config.update(x=1)


def test_reserved_metadata_key():
    assert RESERVED_METADATA_KEY == "eap"
    with pytest.raises(ValueError, match="reserved"):
        validate_metadata({"eap": "x"})
    with pytest.raises(ValueError, match="reserved"):
        validate_filter({"eap": "x"})
    with pytest.raises(ValidationError):
        KnowledgeBaseRef(id=KB_ID, filter={"eap": ["a"]})
    # Near names stay allowed.
    assert validate_metadata({"EAP": 1, "eap_x": 2, "x.eap": 3}) == {"EAP": 1, "eap_x": 2, "x.eap": 3}


def test_agent_spec_with_reserved_filter_key_rejected():
    base = {"name": "a", "model": {"provider": "test", "name": "echo"}}
    ok = AgentSpec.model_validate({**base, "knowledge_bases": [{"id": KB_ID, "filter": {"x": 1}}]})
    assert ok.knowledge_bases[0].filter == {"x": 1}
    with pytest.raises(ValidationError, match="reserved"):
        AgentSpec.model_validate({**base, "knowledge_bases": [{"id": KB_ID, "filter": {"eap": 1}}]})


def test_chunk_extract_metadata():
    meta = ChunkExtractMetadata(extractor="pdf", extractor_version=1, page=3, page_end=4)
    assert meta.model_dump() == {"extractor": "pdf", "extractor_version": 1, "page": 3, "page_end": 4}
    assert ChunkExtractMetadata(extractor="rst", extractor_version=1, condition="x == 1").model_dump() == {
        "extractor": "rst", "extractor_version": 1, "condition": "x == 1",
    }
    for bad in [
        {"extractor": "docx", "extractor_version": 1},
        {"extractor": "pdf", "extractor_version": 0},
        {"extractor": "pdf", "extractor_version": 1, "page": 0},
        {"extractor": "pdf", "extractor_version": 1, "page": 5, "page_end": 4},
        {"extractor": "pdf", "extractor_version": 1, "page_end": 4},
        {"extractor": "rst", "extractor_version": 1, "condition": "x" * 201},
        {"extractor": "rst", "extractor_version": 1, "extra": 1},
    ]:
        with pytest.raises(ValidationError):
            ChunkExtractMetadata(**bad)
    assert set(get_args(ChunkExtractMetadata.model_fields["extractor"].annotation)) == set(EXTRACTOR_IDS)


def test_extracted_event_type_and_data():
    assert "extracted" in get_args(RunEventType)
    data = ExtractedEventData(extractor="pdf", extractor_version=1, sections=4, chars=900, pages=12,
                              image_only_pages=[3, 7])
    assert json.loads(data.model_dump_json()) == {
        "extractor": "pdf", "extractor_version": 1, "sections": 4, "chars": 900, "pages": 12,
        "image_only_pages": [3, 7],
    }
    assert ExtractedEventData(extractor="yaml", extractor_version=1, sections=0, chars=0).model_dump() == {
        "extractor": "yaml", "extractor_version": 1, "sections": 0, "chars": 0,
    }
    with pytest.raises(ValidationError):
        ExtractedEventData(extractor="pdf", extractor_version=1, sections=1, chars=1, image_only_pages=list(range(201)))
    with pytest.raises(ValidationError):
        ExtractedEventData(extractor="pdf", extractor_version=1, sections=1, chars=1, text="no text allowed")


def test_extraction_error_codes():
    assert EXTRACTION_ERROR_CODES == (
        "extract_failed", "extract_timeout", "extract_memory", "extract_limit", "extract_output_too_large",
        "document_encrypted", "no_extractable_text", "unsupported_content", "image_model_required",
    )
    assert all(msg and msg.endswith(".") for msg in EXTRACTION_ERROR_MESSAGES.values())
    assert extraction_error_message("document_encrypted") == "The PDF is encrypted with a password."
    assert extraction_error_message("anything else") == EXTRACTION_ERROR_MESSAGES["extract_failed"]
    # The run-level failure codes are unchanged: extraction errors are reasons inside extraction_failed.
    assert len(INGEST_FAILURE_CODES) == 9 and "extraction_failed" in INGEST_FAILURE_CODES
    assert not set(EXTRACTION_ERROR_CODES) & set(INGEST_FAILURE_CODES)
