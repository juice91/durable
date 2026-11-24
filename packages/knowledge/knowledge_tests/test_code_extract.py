"""Source-code extraction (extractor `code`): AST and declaration chunks with exact line ranges."""

from __future__ import annotations

import pytest

from eap_knowledge import CODE_LANGUAGES, EXTRACTOR_VERSIONS, ExtractionFailed, chunk, extractor_for, format_for_filename
from eap_knowledge.config import ChunkingConfig
from eap_knowledge.formats.code import extract_code

PATH = "pkg/mod.py"
URL = "https://github.com/o/r/blob/abc123/pkg/mod.py"


def lines_of(text: str) -> list[str]:
    return text.split("\n")[:-1] if text.endswith("\n") else text.split("\n")


def body(section) -> str:
    return section.text.split("\n", 1)[1]


def assert_exact(source: str, document) -> None:
    """Every section's text is the header plus exactly its line range, verbatim."""
    src = lines_of(source)
    for section in document.sections:
        c = section.code
        assert body(section) == "\n".join(src[c.start_line - 1 : c.end_line]), c
        assert section.anchor == f"L{c.start_line}-L{c.end_line}"


def by_symbol(document) -> dict[str, object]:
    return {s.code.symbol: s for s in document.sections}


PY = '''"""Module docstring."""
from __future__ import annotations

import os
from dataclasses import dataclass

LIMIT = 10


# A leading comment stays with its function.
@decorator(1)
@other
def top(a, b):
    """Add."""
    return a + b


async def later():
    return 1


class Small:
    """Tiny."""

    x = 1

    def m(self):
        return self.x


if __name__ == "__main__":
    top(1, 2)
'''


def test_python_one_chunk_per_definition_with_exact_lines():
    doc = extract_code(PY, "python", path=PATH, source_url=URL)
    assert_exact(PY, doc)
    symbols = [s.code.symbol for s in doc.sections]
    assert symbols == ["<module>", "top", "later", "Small", "<module-level>"]
    top = by_symbol(doc)["top"]
    assert top.code.start_line == 10  # the comment above the decorators
    assert body(top).startswith("# A leading comment stays with its function.\n@decorator(1)\n@other\ndef top")
    assert '"""Add."""' in body(top)
    header = by_symbol(doc)["<module>"]
    assert '"""Module docstring."""' in body(header) and "import os" in body(header) and "LIMIT = 10" in body(header)
    assert header.code.start_line == 1


def test_python_metadata_and_header_line():
    doc = extract_code(PY, "python", path=PATH, kind="test", source_url=URL)
    top = by_symbol(doc)["top"]
    assert top.text.startswith(f"# {PATH}:{top.code.start_line}-{top.code.end_line} top\n")
    assert top.code.language == "python" and top.code.path == PATH and top.code.kind == "test"
    assert top.heading == f"{PATH}:{top.code.start_line}-{top.code.end_line} top"
    assert doc.format == "code" and doc.source_url == URL


def test_chunks_cite_the_exact_lines_at_the_commit():
    doc = extract_code(PY, "python", path=PATH, source_url=URL)
    chunks = chunk(doc, ChunkingConfig(max_tokens=512, overlap_tokens=32, min_tokens=8, chunker_version=2))
    top = next(c for c in chunks if c.code.symbol == "top")
    assert top.anchor == f"{URL}#L10-L15"
    assert top.title.startswith(f"{PATH}:10-15")
    assert top.code.start_line == 10 and top.code.end_line == 15
    assert PATH in top.embed_text and "def top(a, b)" in top.embed_text
    # small neighbours are never merged by the chunker: one chunk per section
    assert len(chunks) == len(doc.sections)


def test_large_class_is_split_by_method_and_methods_are_qualified():
    methods = "\n".join(f"    def method_{i}(self):\n        return {'x' * 120!r}\n" for i in range(12))
    source = f'class Big(Base):\n    """Docstring."""\n\n    attr = 1\n\n{methods}\n\n\ndef after():\n    pass\n'
    doc = extract_code(source, "python", path=PATH, max_tokens=256)
    assert_exact(source, doc)
    symbols = [s.code.symbol for s in doc.sections]
    assert symbols[0] == "Big"  # class line, docstring and attributes
    assert "Big.method_0" in symbols and "Big.method_11" in symbols and symbols[-1] == "after"
    assert by_symbol(doc)["Big.method_3"].code.end_line - by_symbol(doc)["Big.method_3"].code.start_line == 1


def test_oversized_function_is_split_in_overlapping_windows():
    source = "def big():\n" + "".join(f"    value_{i} = {i}\n" for i in range(300))
    doc = extract_code(source, "python", path=PATH, max_tokens=128, overlap_tokens=16)
    assert len(doc.sections) > 2
    assert_exact(source, doc)
    first, second = doc.sections[0].code, doc.sections[1].code
    assert second.start_line <= first.end_line  # overlap
    assert second.start_line > first.start_line
    assert doc.sections[0].code.symbol == f"big (part 1/{len(doc.sections)})"
    assert doc.sections[-1].code.end_line == len(lines_of(source))
    for c in chunk(doc, ChunkingConfig(max_tokens=128, overlap_tokens=16, min_tokens=8, chunker_version=2)):
        assert len(c.embed_text) <= 128 * 4


def test_python_syntax_error_falls_back_to_line_windows():
    source = "def broken(:\n    pass\n" + "x = 1\n" * 400
    doc = extract_code(source, "python", path=PATH, max_tokens=128)
    assert doc.warnings == ("parse_fallback",)
    assert len(doc.sections) > 1 and all("part" in s.code.symbol for s in doc.sections)
    assert_exact(source, doc)


def test_crlf_and_bom_do_not_move_lines():
    source = "﻿def a():\r\n    return 1\r\n\r\n\r\ndef b():\r\n    return 2\r\n"
    doc = extract_code(source, "python", path=PATH)
    assert [(s.code.symbol, s.code.start_line, s.code.end_line) for s in doc.sections] == [("a", 1, 2), ("b", 5, 6)]


TS = '''import { useState } from "react";
import type { Foo } from "./foo";

/** Props of the card. */
export interface CardProps {
  title: string;
}

export type Id = string;

const TEMPLATE = `line one
function notADeclaration() {
  still inside the template
}`;

export function formatTitle(title: string): string {
  return title.trim();
}

// The component.
export const Card = ({ title }: CardProps) => {
  const [open, setOpen] = useState(false);
  const label = useMemo(() => title.toUpperCase(), [title]);
  const handler = useCallback(() => setOpen((value) => !value), []);
  const re = /[{]+/;
  return (
    <div onClick={() => setOpen(!open)}>{open ? "it's open" : title}</div>
  );
};

export default function Page() {
  return <Card title="x" />;
}
'''


def test_typescript_declarations_components_and_templates():
    doc = extract_code(TS, "tsx", path="web/Card.tsx", max_tokens=1024)
    assert_exact(TS, doc)
    symbols = [s.code.symbol for s in doc.sections]
    joined = " | ".join(s or "" for s in symbols)
    for name in ("CardProps", "formatTitle", "Card", "Page"):
        assert name in joined, symbols
    # the template literal's fake function never became a declaration
    assert "notADeclaration" not in joined
    card = next(s for s in doc.sections if s.code.symbol and s.code.symbol.split(", ")[0] == "Card")
    assert "// The component." in body(card) and body(card).rstrip().endswith("};")
    assert "it's open" in body(card)  # an apostrophe in JSX text does not derail the scanner
    assert doc.sections[0].code.symbol and "import" not in doc.sections[0].code.symbol


def test_typescript_big_class_is_split_by_member():
    members = "\n".join(f"  method{i}(a: number): number {{\n    return a + {i} + {'1' * 100};\n  }}\n" for i in range(14))
    source = f"export class Service {{\n  private x = 1;\n\n{members}}}\n"
    doc = extract_code(source, "typescript", path="svc.ts", max_tokens=256)
    assert_exact(source, doc)
    symbols = [s.code.symbol for s in doc.sections]
    assert "Service" in symbols and "Service.method0" in symbols and "Service.method13" in symbols


def test_typescript_unplaceable_input_falls_back_to_windows():
    source = "\n".join(f"  indented_{i}();" for i in range(400))
    doc = extract_code(source, "javascript", path="x.js", max_tokens=128)
    assert len(doc.sections) > 1
    assert_exact(source, doc)


def test_shell_toml_sql_dockerfile_units():
    sh = "#!/bin/sh\nset -e\n\n# build it\nbuild() {\n  make\n}\n\nfunction clean {\n  rm -rf out\n}\n"
    doc = extract_code(sh, "shell", path="deploy/run.sh", max_tokens=1024)
    assert_exact(sh, doc)
    assert any("build" in (s.code.symbol or "") for s in doc.sections)
    toml = "[project]\nname = 'x'\n\n[tool.ruff]\nline-length = 100\n"
    doc = extract_code(toml, "toml", path="pyproject.toml", max_tokens=1024)
    assert_exact(toml, doc)
    assert "[project]" in doc.sections[0].code.symbol and "[tool.ruff]" in doc.sections[0].code.symbol
    docker = "FROM python:3.13 AS build\nRUN make\n" + "RUN echo x\n" * 200 + "FROM scratch\nCOPY --from=build /a /a\n"
    doc = extract_code(docker, "dockerfile", path="Dockerfile", max_tokens=256)
    assert_exact(docker, doc)
    assert any("stage build" in (s.code.symbol or "") for s in doc.sections)
    sql = "CREATE TABLE a (id int);\n\nCREATE INDEX ix ON a (id);\n"
    doc = extract_code(sql, "sql", path="x.sql", max_tokens=1024)
    assert_exact(sql, doc)
    assert doc.sections[0].text.startswith("-- x.sql:1-3")


@pytest.mark.parametrize(
    ("source", "warning"),
    [
        ("// Code generated by protoc. DO NOT EDIT.\nexport const a = 1;\n", "generated_skipped"),
        ("# DO NOT EDIT\nx = 1\n", "generated_skipped"),
        ("/* @generated */\nvar a=1;\n", "generated_skipped"),
        ("var a=" + "1+" * 1500 + "1;\n", "minified_skipped"),
        ("x=1;" * 800 + "\n" + "y=2;" * 800 + "\n", "minified_skipped"),
    ],
)
def test_generated_and_minified_files_yield_no_sections(source, warning):
    doc = extract_code(source, "javascript", path="a.js")
    assert doc.sections == () and doc.warnings == (warning,) and doc.extracted_chars == 0


def test_binary_is_refused():
    with pytest.raises(ExtractionFailed) as info:
        extract_code("abc\x00def", "python", path="a.py")
    assert info.value.code == "unsupported_content"


def test_empty_file_has_no_sections():
    assert extract_code("", "python", path="a.py").sections == ()
    assert extract_code("\n\n", "python", path="a.py").sections == ()


def test_registry_maps_code_mime_types_and_names_to_the_code_extractor():
    assert EXTRACTOR_VERSIONS["code"] == 1
    assert CODE_LANGUAGES == {"python", "typescript", "tsx", "javascript", "shell", "toml", "sql", "dockerfile", "css"}
    for mime in ("text/x-python", "text/typescript", "text/tsx", "text/javascript", "text/x-shellscript",
                 "application/toml", "application/sql", "text/x-dockerfile", "text/css"):
        assert extractor_for(mime) == "code"
    assert format_for_filename("deploy/docker/Dockerfile", include_code=True).format == "dockerfile"
    assert format_for_filename("Dockerfile.api", include_code=True).format == "dockerfile"
    assert format_for_filename("a/b.py", include_code=True).format == "python"
    assert format_for_filename("a/c.md", include_code=True).format == "markdown"
    # the importers' map is the document formats only: code is opt-in
    assert format_for_filename("a/b.py") is None and format_for_filename("Dockerfile") is None
    assert extractor_for("application/yaml") == "yaml" and extractor_for("text/markdown") is None
