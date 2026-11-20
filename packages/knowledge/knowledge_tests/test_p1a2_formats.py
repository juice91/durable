"""RAG P1.A2: the worker-side text extractors (rst, YAML/JSON, Doxygen Markdown) and chunker v2."""

from __future__ import annotations

import builtins
import hashlib
import json
import pickle
import re
import subprocess
import sys
import urllib.request
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path

import pytest
from eap_schemas.knowledge import ExtractConfig, new_knowledge_base_chunking

from eap_knowledge import Chunk, ChunkingConfig, Document, ExtractionFailed, Section, chunk, extract
from eap_knowledge.formats import doxygen, rst, structured
from eap_knowledge.formats.doxygen import doxygen_to_markdown, extract_doxygen_markdown
from eap_knowledge.formats.rst import evaluate_condition, extract_rst
from eap_knowledge.formats.structured import extract_json, extract_yaml

URL = "https://docs.example.com/page.html"
CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
V2 = new_knowledge_base_chunking()


@dataclass(frozen=True)
class Cfg:
    max_tokens: int = 512
    overlap_tokens: int = 64
    min_tokens: int = 64
    chunker_version: int = 2


def _texts(doc: Document) -> list[tuple[tuple[str, ...], str]]:
    return [(s.heading_path, s.text) for s in doc.sections if s.text or s.heading]


def _all_text(doc: Document) -> str:
    return "\n".join(s.heading + "\n" + s.text for s in doc.sections)


# --- rst: structure -------------------------------------------------------------------------------

RST = r"""
.. _install-label:

Getting Started
===============

Intro paragraph about the SDK, see :ref:`install-label`, :ref:`the guide <somewhere>` and
:doc:`/other/page`. Edit :file:`/etc/hosts` and run :command:`make`.

Building
--------

.. code-block:: bash
   :linenos:

   make -j8 all

.. note:: Build on a host with 16 GB of RAM.

.. toctree::
   :maxdepth: 2

   child_page

.. include:: /common/_fragment.rst

.. tabs-of-the-future:: Linux host

   Unknown directive content survives.

Flashing
~~~~~~~~

+--------+-------------+
| Board  | Command     |
+========+=============+
| AM62x  | flash a|b   |
+--------+-------------+
| AM64x  | flash x     |
+--------+-------------+
"""


def test_rst_sections_heading_paths_and_docutils_ids_as_anchors() -> None:
    doc = extract_rst(RST, source_url=URL)
    assert doc.format == "rst"
    headed = [s for s in doc.sections if s.level]
    assert [(s.heading_path, s.level, s.anchor) for s in headed] == [
        (("Getting Started",), 1, "getting-started"),
        (("Getting Started", "Building"), 2, "building"),
        (("Getting Started", "Building", "Flashing"), 3, "flashing"),
    ]
    anchors = {c.anchor for c in chunk(doc, Cfg(min_tokens=0))}
    assert anchors == {f"{URL}#getting-started", f"{URL}#building", f"{URL}#flashing"}


def test_rst_directives_roles_and_tables() -> None:
    doc = extract_rst(RST, source_url=URL)
    intro, building, flashing = (s.text for s in doc.sections if s.level)
    # Roles render their text; a same-document :ref: label renders its section title.
    assert "see Getting Started, the guide and /other/page" in intro
    assert "`/etc/hosts`" in intro and "`make`" in intro
    # code-block -> a fence with the language (options parsed, not text); note -> "Note:".
    assert "```bash\nmake -j8 all\n```" in building
    assert ":linenos:" not in building
    assert "Note: Build on a host with 16 GB of RAM." in building
    # toctree and include dropped (include counted); unknown directives keep argument and content.
    assert "child_page" not in building and "_fragment" not in building
    assert "Linux host\n\nUnknown directive content survives." in building
    assert doc.warnings == ("include_dropped",)
    # Tables become pipe rows, with a header separator and escaped pipes.
    assert flashing == "| Board | Command |\n|---|---|\n| AM62x | flash a\\|b |\n| AM64x | flash x |"


def test_rst_title_argument_wins_then_the_title_directive() -> None:
    assert extract_rst(".. title:: From Directive\n\nText.\n").title == "From Directive"
    assert extract_rst(".. title:: From Directive\n\nText.\n", title="Upload Title").title == "Upload Title"
    assert extract_rst("Text.\n").title == ""


def test_rst_substitutions_resolve_where_defined_else_stay_as_text() -> None:
    doc = extract_rst(".. |sdk| replace:: Processor SDK\n\nThe |sdk| on |undefined-sub| parts.\n")
    assert doc.sections[0].text == "The Processor SDK on |undefined-sub| parts."


# --- rst: conditions ------------------------------------------------------------------------------

COND = """
Board setup
===========

Common text.

.. ifconfig:: CONFIG_part_variant in ('AM62X')

   AM62X only text.

.. ifconfig:: CONFIG_part_variant in ('AM64X', 'J721E')

   AM64X or J721E text.

.. only:: html

   HTML build text.
"""


def test_ifconfig_with_config_keeps_the_true_branch_and_drops_the_false_one() -> None:
    doc = extract_rst(COND, ExtractConfig(rst_config={"CONFIG_part_variant": "AM62X"}, rst_tags=["html"]))
    text = doc.sections[1].text
    assert "AM62X only text." in text and "AM64X or J721E" not in text and "HTML build text." in text
    assert "Applies when" not in text and doc.sections[1].condition is None
    other = extract_rst(COND, ExtractConfig(rst_config={"CONFIG_part_variant": "J721E"}, rst_tags=["latex"]))
    assert "AM64X or J721E text." in other.sections[1].text and "AM62X only" not in other.sections[1].text
    assert "HTML build text." not in other.sections[1].text


def test_ifconfig_without_config_keeps_both_branches_and_records_the_condition() -> None:
    doc = extract_rst(COND, source_url=URL)
    section = doc.sections[1]
    assert "AM62X only text." in section.text and "AM64X or J721E text." in section.text
    assert "Applies when: CONFIG_part_variant in ('AM62X')" in section.text
    assert section.condition == (
        "CONFIG_part_variant in ('AM62X'); CONFIG_part_variant in ('AM64X', 'J721E'); html"
    )
    chunks = chunk(doc, Cfg(min_tokens=0))
    assert {c.condition for c in chunks} == {section.condition}
    # A name missing from the config is unevaluable too: kept, recorded.
    partial = extract_rst(COND, ExtractConfig(rst_config={"CONFIG_sdk": "SITARA"}))
    assert "AM62X only text." in partial.sections[1].text and partial.sections[1].condition


def test_conditions_inside_nested_sections_are_recorded_on_those_sections() -> None:
    src = "Top\n===\n\n.. ifconfig:: CONFIG_x == 'a'\n\n   Inner\n   -----\n\n   Inner text.\n\nAfter.\n"
    doc = extract_rst(src)
    inner = next(s for s in doc.sections if s.heading == "Inner")
    assert inner.condition == "CONFIG_x == 'a'" and "Inner text." in inner.text
    assert doc.sections[1].condition == "CONFIG_x == 'a'"


def test_a_long_condition_is_clipped_to_200_characters() -> None:
    names = ", ".join(f"'V{i:03d}'" for i in range(80))
    doc = extract_rst(f"T\n=\n\n.. ifconfig:: CONFIG_v in ({names})\n\n   x\n")
    assert len(doc.sections[1].condition) == 200 and doc.sections[1].condition.endswith("…")


@pytest.mark.parametrize(
    ("expr", "names", "expected"),
    [
        ("CONFIG_v in ('AM62X')", {"CONFIG_v": "AM62X"}, True),  # Sphinx: a string, substring test
        ("CONFIG_v in ('AM62X')", {"CONFIG_v": "AM64X"}, False),
        ("CONFIG_v in ('AM62X', 'AM64X')", {"CONFIG_v": "AM64X"}, True),
        ("CONFIG_v in ['AM62X']", {"CONFIG_v": "AM64X"}, False),
        ("CONFIG_v not in ('AM62LX')", {"CONFIG_v": "AM62X"}, True),
        ("CONFIG_v == 'a' and not CONFIG_w != 2", {"CONFIG_v": "a", "CONFIG_w": 2}, True),
        ("CONFIG_v == 'a' or CONFIG_w == 3", {"CONFIG_v": "b", "CONFIG_w": 3}, True),
        ("(CONFIG_flag)", {"CONFIG_flag": False}, False),
        ("1 == 1.0", {}, True),
        ("CONFIG_missing == 'a'", {"CONFIG_v": "a"}, None),
        ("CONFIG_v == 'a' or CONFIG_missing", {"CONFIG_v": "a"}, None),  # strict: every name resolves
        ("CONFIG_v.upper() == 'A'", {"CONFIG_v": "a"}, None),  # call and attribute
        ("__import__('os').system('true')", {}, None),
        ("CONFIG_v[0] == 'a'", {"CONFIG_v": "a"}, None),  # subscript
        ("CONFIG_n + 1 == 2", {"CONFIG_n": 1}, None),  # arithmetic
        ("CONFIG_n < 2", {"CONFIG_n": 1}, None),  # ordering comparisons are not in the grammar
        ("(lambda: True)()", {}, None),
        ("[x for x in 'ab']", {}, None),
        ("CONFIG_n in CONFIG_m", {"CONFIG_n": "a", "CONFIG_m": "abc"}, None),  # `in` needs a literal
        ("1 in ('1')", {}, None),  # a number in a string
        ("b'x' == b'x'", {}, None),
        ("x ==", {}, None),
        ("not " * 40 + "True", {}, None),  # deeper than the evaluator's depth cap
        ("'a' == " + "'a'" + " " * 1000, {}, None),  # over MAX_CONDITION_SOURCE
    ],
)
def test_the_condition_evaluator(expr: str, names: dict, expected: bool | None) -> None:
    def lookup(name: str) -> object:
        return names[name]

    assert evaluate_condition(expr, lookup) is expected


def test_only_uses_tags_and_without_tags_is_unevaluated() -> None:
    src = "T\n=\n\n.. only:: html and not latex\n\n   Web text.\n"
    assert "Web text." in extract_rst(src, ExtractConfig(rst_tags=["html"])).sections[1].text
    assert "Web text." not in extract_rst(src, ExtractConfig(rst_tags=["html", "latex"])).sections[1].text
    untagged = extract_rst(src, ExtractConfig(rst_config={"CONFIG_x": 1}))
    assert untagged.sections[1].condition == "html and not latex"


# --- rst: safety ----------------------------------------------------------------------------------


def test_nul_and_c0_controls_never_survive() -> None:
    src = "Ti\x00tle\n=====\n\nOne\\ two \\\\ three\\* \x00four\x01 \x0bfive \x1f\x7fsix\t*em*\n\n``lit\\ eral``\n"
    doc = extract_rst(src, title="T\x00itle")
    out = _all_text(doc) + doc.title
    assert not CONTROL.search(out)
    assert doc.sections[1].heading == "Title"
    assert doc.sections[1].text == "Onetwo \\ three* four five six *em*\n\n`lit\\ eral`"


def test_raw_include_and_csv_table_never_read_a_file_or_fetch_a_url(tmp_path: Path, monkeypatch) -> None:
    sentinel = tmp_path / "secret.txt"
    sentinel.write_text("SENTINEL-SECRET-CONTENT,1\n")
    opened: list[str] = []
    real_open = builtins.open

    def spy_open(file, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        opened.append(str(file))
        return real_open(file, *args, **kwargs)

    def no_network(*args, **kwargs):  # noqa: ANN002, ANN003
        raise AssertionError("network access")

    monkeypatch.setattr(builtins, "open", spy_open)
    monkeypatch.setattr(urllib.request, "urlopen", no_network)
    src = f"""
Doc
===

.. include:: {sentinel}

.. literalinclude:: {sentinel}

.. raw:: html
   :file: {sentinel}

.. raw:: html

   <script>alert(1)</script>

.. role:: raw-html(raw)
   :format: html

Inline :raw-html:`<b>bold</b>` text.

.. csv-table:: From file
   :file: {sentinel}

.. csv-table:: From URL
   :url: http://127.0.0.1:9/data.csv

.. csv-table:: Inline
   :file: {sentinel}

   a,b
   c,d
"""
    doc = extract_rst(src)
    text = _all_text(doc)
    assert str(sentinel) not in opened
    assert "SENTINEL" not in text
    assert "<script>" not in text and "<b>" not in text and "alert" not in text
    assert "Inline text." in text  # the raw role's text is dropped, its sentence kept
    assert "| a | b |" in text  # a csv-table keeps its inline rows, never its :file:
    assert sorted(doc.warnings) == sorted(
        ["include_dropped", "literalinclude_dropped", "raw_dropped", "raw_dropped", "raw_dropped",
         "csv_table_source_dropped", "csv_table_source_dropped", "csv_table_source_dropped"]
    )


def test_a_document_cannot_leak_roles_or_lookups_into_the_next_parse() -> None:
    from docutils.parsers.rst import directives, roles

    before = (directives.directive, roles.role, dict(roles._roles), dict(directives._directives))
    extract_rst(".. role:: leaked(literal)\n\n:leaked:`x`\n")
    after = (directives.directive, roles.role, dict(roles._roles), dict(directives._directives))
    assert before == after
    doc = extract_rst(":leaked:`plain text`\n")
    assert doc.sections[0].text == "plain text"  # unknown again: rendered as plain text


@pytest.mark.parametrize(
    "src",
    [
        "- " * 4999 + "x\n",  # lists nested 5,000 deep
        "".join(" " * i + "x\n\n" for i in range(5000)),  # block quotes nested 5,000 deep
    ],
    ids=["lists", "quotes"],
)
def test_deep_nesting_fails_extract_limit(src: str) -> None:
    with pytest.raises(ExtractionFailed) as info:
        extract_rst(src)
    assert info.value.reason == "extract_limit" == info.value.code
    assert pickle.loads(pickle.dumps(info.value)).reason == "extract_limit"
    # docutils' lookups are restored even after the failure.
    assert extract_rst(".. note:: fine\n").sections[0].text == "Note: fine"


def test_an_overlong_line_fails_extract_limit_instead_of_an_empty_document() -> None:
    with pytest.raises(ExtractionFailed) as info:
        extract_rst("Title\n=====\n\n" + "x" * (rst.LINE_LENGTH_LIMIT + 1) + "\n")
    assert info.value.reason == "extract_limit"
    assert extract_rst("x" * rst.LINE_LENGTH_LIMIT + "\n").extracted_chars == rst.LINE_LENGTH_LIMIT


# --- rst: a TI-like tree, deterministic ------------------------------------------------------------

TI_TREE = {
    "linux/Foundational_Components/Kernel/Kernel_Drivers/UART.rst": """
.. _uart-driver:

UART
####

Introduction
************

The UART driver supports the 8250 compatible controllers of |__PART_FAMILY_NAME__| devices.

.. ifconfig:: CONFIG_part_variant in ('AM62X', 'AM62PX')

   Up to seven UART instances are available on the main domain.

.. ifconfig:: CONFIG_part_variant in ('AM64X')

   Up to six UART instances are available.

Enabling the console
********************

.. code-block:: console

   root@evm:~# stty -F /dev/ttyS2 115200

Kernel config
*************

.. code-block:: text

   CONFIG_SERIAL_8250=y
   CONFIG_SERIAL_8250_OMAP=y

.. rubric:: Known issues

.. csv-table::
   :header: "ID", "Summary"

   "LCPD-1", "Break detection is unreliable."
""",
    "linux/How_to_Guides/Target/How_to_flash.rst": r"""
How to flash the eMMC
=====================

Use :command:`dd` from U-Boot or Linux; see :ref:`uart-driver` for the console.

From U-Boot
-----------

.. code-block:: console

   => mmc dev 0 1
   => fatload mmc 1 ${loadaddr} tiboot3.bin
   => mmc write ${loadaddr} 0x0 0x400

.. warning:: Writing the boot partition erases the previous bootloader.

From Linux
----------

.. code-block:: console

   root@evm:~# dd if=tiboot3.bin of=/dev/mmcblk0boot0 bs=512 seek=0

.. note::

   Unlock the boot partition first: ``echo 0 > /sys/block/mmcblk0boot0/force_ro``.
""",
}


def _tree_digest(config: object, options: ExtractConfig | None) -> str:
    hashes = []
    for path, text in sorted(TI_TREE.items()):
        doc = extract_rst(text, options, title=Path(path).stem, source_url=f"https://ti.example/{path[:-4]}.html")
        hashes.append("|".join(c.embed_text_hash for c in chunk(doc, config)))
    return hashlib.sha256("\n".join(hashes).encode()).hexdigest()


def test_a_ti_rst_tree_chunks_deterministically_across_runs_and_processes() -> None:
    options = ExtractConfig(rst_config={"CONFIG_part_variant": "AM62X"})
    first = _tree_digest(V2, options)
    assert _tree_digest(V2, options) == first
    script = (
        "import sys, json; sys.path.insert(0, sys.argv[1]);"
        "from test_p1a2_formats import _tree_digest, V2; from eap_schemas.knowledge import ExtractConfig;"
        "print(_tree_digest(V2, ExtractConfig(rst_config={'CONFIG_part_variant': 'AM62X'})))"
    )
    for seed in ("1", "12345"):
        out = subprocess.run(
            [sys.executable, "-c", script, str(Path(__file__).parent)],
            capture_output=True, text=True, check=True, env={"PYTHONHASHSEED": seed, "PATH": "/usr/bin:/bin"},
        )
        assert out.stdout.strip() == first


def test_ti_tree_v2_keeps_each_command_with_its_heading_context() -> None:
    path = "linux/How_to_Guides/Target/How_to_flash.rst"
    doc = extract_rst(TI_TREE[path], title="How_to_flash", source_url="https://ti.example/flash.html")
    chunks = chunk(doc, V2)
    dd = next(c for c in chunks if "dd if=tiboot3.bin" in c.text)
    # "From Linux" is short: v2 puts it under its parent heading, never into an unrelated section.
    assert dd.heading_path == ("How to flash the eMMC",)
    assert "### From Linux" in dd.text or "## From Linux" in dd.text
    uart = extract_rst(TI_TREE["linux/Foundational_Components/Kernel/Kernel_Drivers/UART.rst"])
    assert "Up to seven UART instances" in _all_text(uart)  # no config: both branches kept
    assert "|__PART_FAMILY_NAME__|" in _all_text(uart)  # undefined substitution kept as text


# --- YAML and JSON ---------------------------------------------------------------------------------


def _prop(name: str) -> str:
    return (
        f"  {name}:\n    description: >\n      The {name} property of the ADS1x1x binding. "
        + "It is described at length so that it stands as its own section in the knowledge base. " * 6
        + "\n    maxItems: 1\n"
    )


DT_BINDING = (
    "%YAML 1.2\n---\n$id: http://devicetree.org/schemas/iio/adc/ti,ads1x1x.yaml#\n"
    "$schema: http://devicetree.org/meta-schemas/core.yaml#\n\ntitle: TI ADS1x1x ADC\n\n"
    "maintainers:\n  - Jane Doe <jane@example.com>\n\ndescription: |\n  Acme Semiconductor ADCs.\n  Two lines.\n\n"
    "properties:\n" + _prop("compatible") + _prop("reg") + _prop("interrupts") + _prop('"#io-channel-cells"')
    + "\nrequired:\n  - compatible\n  - reg\n\nadditionalProperties: false\n"
)


def test_a_dt_binding_gives_one_chunk_per_property() -> None:
    doc = extract_yaml(DT_BINDING, source_url="https://x/ads1x1x.yaml", max_tokens=V2.max_tokens)
    assert doc.format == "yaml" and doc.title == "TI ADS1x1x ADC"
    chunks = chunk(doc, V2)
    props = [c for c in chunks if c.heading_path[:1] == ("properties",) and len(c.heading_path) == 2]
    assert [c.heading_path for c in props] == [
        ("properties", "compatible"),
        ("properties", "reg"),
        ("properties", "interrupts"),
        ("properties", "#io-channel-cells"),
    ]
    assert props[0].anchor == "https://x/ads1x1x.yaml#properties.compatible"
    assert props[3].anchor == "https://x/ads1x1x.yaml#properties.%23io-channel-cells"
    assert props[1].text.startswith("reg:\n  description: |\n    The reg property of the ADS1x1x binding.")
    # The short top-level keys after it have no parent heading and cannot go back above the
    # property chunks: `required` stays on its own and `additionalProperties` joins it.
    assert not [c for c in chunks if c.heading_path == ("properties",)]
    tail = next(c for c in chunks if c.heading_path == ("required",))
    assert tail.text == "required:\n- compatible\n- reg\n\n# additionalProperties\n\nadditionalProperties: false"
    headings = [s.heading for s in doc.sections if s.level == 1]
    assert headings == ["$id", "$schema", "title", "maintainers", "description", "properties", "required",
                        "additionalProperties"]
    description = next(s for s in doc.sections if s.heading == "description")
    assert description.text == "description: |\n  Acme Semiconductor ADCs.\n  Two lines."


def test_a_small_mapping_is_not_split() -> None:
    doc = extract_yaml("properties:\n  reg:\n    maxItems: 1\n", max_tokens=512)
    assert [(s.heading_path, s.text) for s in doc.sections if s.level] == [
        (("properties",), "properties:\n  reg:\n    maxItems: 1")
    ]


def test_title_argument_wins_and_a_non_string_title_is_ignored() -> None:
    assert extract_yaml("title: From File\n", title="Upload").title == "Upload"
    assert extract_yaml("title: [1, 2]\n").title == ""
    assert extract_json('{"title": "JSON Title", "a": 1}').title == "JSON Title"


def test_a_multi_document_stream() -> None:
    doc = extract_yaml("name: first\n---\nname: second\n---\n- a list\n---\n", source_url="https://x/s.yaml")
    assert [(s.heading_path, s.anchor, s.text) for s in doc.sections if s.level] == [
        (("Document 1",), "doc-1", ""),
        (("Document 1", "name"), "doc-1.name", "name: first"),
        (("Document 2",), "doc-2", ""),
        (("Document 2", "name"), "doc-2.name", "name: second"),
        (("Document 3",), "doc-3", "- a list"),
        (("Document 4",), "doc-4", ""),
    ]


def test_scalar_and_list_documents_and_non_string_keys() -> None:
    assert _texts(extract_yaml("- a\n- b\n")) == [((), "- a\n- b")]
    assert _texts(extract_json("[1, 2]")) == [((), "- 1\n- 2")]
    doc = extract_yaml("8: eight\ntrue: yes\n2024-01-02: date\n")
    assert [s.heading for s in doc.sections if s.level] == ["8", "true", "2024-01-02"]


def test_billion_laughs_fails_extract_limit_fast() -> None:
    lol = 'a: &a ["lol","lol","lol","lol","lol","lol","lol","lol","lol"]\n' + "".join(
        f"{c}: &{c} [" + ",".join([f"*{p}"] * 9) + "]\n" for p, c in zip("abcdefgh", "bcdefghi", strict=True)
    )
    with pytest.raises(ExtractionFailed) as info:
        extract_yaml(lol)
    assert info.value.reason == "extract_limit" and "nodes" in info.value.detail


def test_alias_count_and_node_count_limits(monkeypatch) -> None:
    many_aliases = "a: &a 1\nb: [" + ", ".join(["*a"] * (structured.MAX_YAML_ALIASES + 1)) + "]\n"
    with pytest.raises(ExtractionFailed) as info:
        extract_yaml(many_aliases)
    assert info.value.reason == "extract_limit" and "aliases" in info.value.detail
    monkeypatch.setattr(structured, "MAX_YAML_NODES", 1_000)
    with pytest.raises(ExtractionFailed) as info:
        extract_yaml("[" + ", ".join(["1"] * 1_000) + "]")
    assert info.value.reason == "extract_limit"
    assert len(extract_yaml("[" + ", ".join(["1"] * 998) + "]").sections) == 1


def test_yaml_nesting_limit() -> None:
    with pytest.raises(ExtractionFailed) as info:
        extract_yaml("[" * 65 + "]" * 65)
    assert info.value.reason == "extract_limit"
    extract_yaml("[" * 63 + "]" * 63)


def test_json_depth_65_is_rejected_and_64_accepted() -> None:
    with pytest.raises(ExtractionFailed) as info:
        extract_json("[" * 65 + "]" * 65)
    assert info.value.reason == "extract_limit"
    extract_json("[" * 64 + "]" * 64)
    # Brackets inside strings do not count.
    assert extract_json(json.dumps({"k": "[" * 500})).sections[1].heading == "k"


@pytest.mark.parametrize(
    ("fn", "src"),
    [(extract_yaml, "secret: [1,\n  b: }"), (extract_json, '{"secret": tru}'), (extract_yaml, "a: !!python/object:os.system x\n")],
)
def test_invalid_input_fails_without_echoing_content(fn, src: str) -> None:  # noqa: ANN001
    with pytest.raises(ExtractionFailed) as info:
        fn(src)
    assert info.value.reason == "extract_failed"
    assert "secret" not in str(info.value) and "os.system" not in str(info.value)


def test_structured_output_has_no_control_characters() -> None:
    doc = extract_yaml('"k\\x01ey": "v\\0al"\nb: "line\\x0bfeed"\n')
    assert not CONTROL.search(_all_text(doc))
    assert doc.sections[1].heading == "key"


# --- Doxygen Markdown ------------------------------------------------------------------------------

DOXY = r"""# IPC RPMessage {#DRIVERS_IPC_RPMESSAGE_PAGE}

[TOC]

\ingroup DRV_IPC_MODULE
\addtogroup DRV_IPC_GROUP
\{

See \ref DRIVERS_IPC_NOTIFY_PAGE, \ref UNKNOWN_ID and \ref X "the X page".

\brief Short summary.
\note Max message size is 1152 bytes.
\warning Do not block in the callback.

\section IPC_FEATURES Features

\anchor IPC_USAGE_ANCHOR
## Usage

\code{.c}
RPMessage_send(\ref NOT_RESOLVED_IN_CODE);
\endcode

```
\section NOT_A_HEADING inside a fence
```

\cond SOC_AM64X
AM64x text.
\else
Other devices text.
\endcond

\subsection IPC_SUB Sub section about \ref IPC_FEATURES
@VAR_SOC_NAME
\image html ipc.png
\snippet examples/ipc.c send_example
\}

\page DRIVERS_IPC_NOTIFY_PAGE IPC Notify

Body of the notify page.
"""


def test_doxygen_commands() -> None:
    doc = extract_doxygen_markdown(DOXY, title="IPC", source_url=URL)
    assert doc.format == "markdown-doxygen" and doc.title == "IPC"
    headed = [(s.heading_path, s.anchor) for s in doc.sections if s.level]
    assert headed == [
        (("IPC RPMessage",), "DRIVERS_IPC_RPMESSAGE_PAGE"),
        (("IPC RPMessage", "Features"), "IPC_FEATURES"),
        (("IPC RPMessage", "Usage"), "IPC_USAGE_ANCHOR"),
        (("IPC RPMessage", "Usage", "Sub section about Features"), "IPC_SUB"),
        (("IPC Notify",), "DRIVERS_IPC_NOTIFY_PAGE"),
    ]
    intro, _, usage, sub, notify = (s.text for s in doc.sections if s.level)
    assert intro == (
        "See IPC Notify, UNKNOWN_ID and the X page.\n\nShort summary.\n"
        "Note: Max message size is 1152 bytes.\nWarning: Do not block in the callback."
    )
    for dropped in ("[TOC]", "ingroup", "addtogroup", "\\{", "\\}", "@VAR_", "ipc.png"):
        assert dropped not in _all_text(doc)
    assert "```c\nRPMessage_send(\\ref NOT_RESOLVED_IN_CODE);\n```" in usage  # code is never rewritten
    assert "```\n\\section NOT_A_HEADING inside a fence\n```" in usage
    assert "Applies when: SOC_AM64X\n\nAM64x text.\n\nOtherwise:\n\nOther devices text." in usage
    assert doc.sections[3].condition == "SOC_AM64X"
    assert sub == "(code snippet send_example from examples/ipc.c, not included)"
    assert notify == "Body of the notify page."
    assert {c.anchor for c in chunk(doc, Cfg(min_tokens=0))} >= {f"{URL}#IPC_USAGE_ANCHOR", f"{URL}#IPC_SUB"}


def test_doxygen_elseif_mainpage_and_unclosed_code() -> None:
    md = doxygen_to_markdown("\\mainpage Welcome\n\\if A\nx\n\\elseif B\ny\n\\endif\n\\code\nint x;\n")
    assert md.split("\n") == [
        "", "# Welcome", "", "Applies when: A", "", "x", "", "Otherwise, when: B", "", "y", "```", "int x;", "", "```",
    ]


def test_plain_markdown_is_unchanged_by_the_doxygen_path() -> None:
    plain = "# Title\n\nSome *text* with `code` and an @mention.\n\n## Next\n\nMore.\n"
    a = extract_doxygen_markdown(plain, source_url=URL)
    b = extract(plain, "text/markdown", source_url=URL)
    assert a.sections == b.sections


# --- chunker v2 -------------------------------------------------------------------------------------

PROSE = "This paragraph explains the step in enough words to be a real section of the guide. " * 4


def _sec(level: int, path: tuple[str, ...], text: str, **meta: object) -> Section:
    return Section(heading_path=path, heading=path[-1] if path else "", level=level, text=text,
                   anchor=path[-1].lower() if path else None, **meta)


def _doc(*sections: Section) -> Document:
    return Document(title="Doc", source_url=URL, format="markdown", sections=sections)


def test_v2_a_heading_with_one_code_block_stays_with_its_parent() -> None:
    doc = extract("# Install\n\n" + PROSE + "\n\n## Run it\n\n```\nmake run\n```\n\n# Unrelated\n\n" + PROSE,
                  "text/markdown", source_url=URL)
    v1 = chunk(doc, Cfg(chunker_version=1, min_tokens=40))
    v2 = chunk(doc, Cfg())
    # v1 merges the tiny section into the NEXT one (an unrelated heading); v2 into its parent.
    assert next(c for c in v1 if "make run" in c.text).text.endswith("# Unrelated\n\n" + PROSE.strip())
    run = next(c for c in v2 if "make run" in c.text)
    assert run.heading_path == ("Install",) and run.anchor == f"{URL}#install"
    assert run.text == PROSE.strip() + "\n\n## Run it\n\n```\nmake run\n```"
    assert [c.heading_path for c in v2] == [("Install",), ("Unrelated",)]


def test_v2_without_a_parent_a_short_section_joins_its_previous_sibling_never_the_next() -> None:
    doc = _doc(
        _sec(1, ("A",), PROSE),
        _sec(1, ("B",), "short b"),
        _sec(1, ("C",), PROSE),
    )
    v2 = chunk(doc, Cfg())
    assert [(c.heading_path, c.text) for c in v2] == [
        (("A",), PROSE + "\n\n# B\n\nshort b"),
        (("C",), PROSE),
    ]
    first_short = _doc(_sec(1, ("A",), "short a"), _sec(1, ("B",), PROSE))
    assert [c.heading_path for c in chunk(first_short, Cfg())] == [("A",), ("B",)]


def test_v2_short_children_gather_under_an_empty_parent_heading_and_nest() -> None:
    doc = _doc(
        _sec(1, ("Guide",), ""),
        _sec(2, ("Guide", "Step 1"), "do one"),
        _sec(3, ("Guide", "Step 1", "Detail"), "detail"),
        _sec(2, ("Guide", "Step 2"), "do two"),
        _sec(2, ("Guide", "Big"), PROSE),
        _sec(2, ("Guide", "Step 3"), "do three"),
    )
    v2 = chunk(doc, Cfg())
    # "Step 3" comes after "Big", which started its own group: it joins "Big" (the latest group),
    # never back into "Guide" above it.
    assert [(c.heading_path, c.text) for c in v2] == [
        (("Guide",), "## Step 1\n\ndo one\n\n### Detail\n\ndetail\n\n## Step 2\n\ndo two"),
        (("Guide", "Big"), PROSE + "\n\n## Step 3\n\ndo three"),
    ]


def test_v2_keeps_document_order_and_only_same_level_siblings() -> None:
    # A short child after a long sibling's own long child: neither the parent's group nor the
    # sibling's group is the latest, so it stays on its own instead of moving back.
    doc = _doc(
        _sec(1, ("P",), PROSE),
        _sec(2, ("P", "A"), PROSE),
        _sec(3, ("P", "A", "A1"), PROSE),
        _sec(2, ("P", "B"), "short b"),
    )
    assert [(c.heading_path, c.text) for c in chunk(doc, Cfg())] == [
        (("P",), PROSE), (("P", "A"), PROSE), (("P", "A", "A1"), PROSE), (("P", "B"), "short b"),
    ]
    # Only an entry at the SAME level is a sibling: a deeper section popped off the stack is not.
    doc = _doc(_sec(3, ("X",), PROSE), _sec(1, ("Y",), "short y"))
    assert [c.heading_path for c in chunk(doc, Cfg())] == [("X",), ("Y",)]
    # An empty heading in between does not block: a short child still joins its parent.
    doc = _doc(_sec(1, ("P",), PROSE), _sec(2, ("P", "Empty"), ""), _sec(2, ("P", "C"), "short c"))
    assert [(c.heading_path, c.text) for c in chunk(doc, Cfg())] == [(("P",), PROSE + "\n\n## C\n\nshort c")]


def test_v2_preamble_is_never_a_parent_and_min_tokens_zero_merges_nothing() -> None:
    doc = _doc(_sec(0, (), "tiny preamble"), _sec(1, ("A",), "tiny a"), _sec(1, ("B",), "tiny b"))
    assert [(c.heading_path, c.text) for c in chunk(doc, Cfg())] == [
        ((), "tiny preamble"),
        (("A",), "tiny a\n\n# B\n\ntiny b"),
    ]
    assert len(chunk(doc, Cfg(min_tokens=0))) == 3


def test_v2_code_only_sections_get_the_preceding_paragraph_in_their_embed_text_only() -> None:
    code = "```console\n" + "\n".join(f"$ ./tool --step {i} --verbose --output /tmp/out{i}" for i in range(12)) + "\n```"
    doc = _doc(
        _sec(1, ("Flash",), "Intro.\n\nTo flash the board over UART, run the steps below in order.\n\n```\nx\n```"),
        _sec(1, ("Commands",), code),
    )
    cmd = chunk(doc, Cfg())[1]
    assert cmd.text == code
    assert cmd.embed_text == (
        "path: Doc > Commands\n\ntitle: Commands\n\n"
        "context: To flash the board over UART, run the steps below in order.\n\n" + code
    )
    assert cmd.embed_text_hash == hashlib.sha256(cmd.embed_text.encode()).hexdigest()
    # Prose sections get no context; v1 never does.
    assert not chunk(doc, Cfg())[0].embed_text.count("context: ")
    assert all("context: " not in c.embed_text for c in chunk(doc, Cfg(chunker_version=1)))


def test_v2_code_context_is_clipped_and_never_breaks_the_cap() -> None:
    long_para = "word " * 200
    code = "```\n" + "x = 1\n" * 300 + "```"
    doc = _doc(_sec(1, ("A",), long_para), _sec(1, ("B",), code))
    for max_tokens in (32, 64, 512):
        cfg = Cfg(max_tokens=max_tokens, overlap_tokens=4, min_tokens=0)
        for c in chunk(doc, cfg):
            assert len(c.embed_text) <= max_tokens * 4
        b = [c for c in chunk(doc, cfg) if c.heading_path == ("B",)]
        assert "".join(c.text[c.overlap:] for c in b).replace("\n", "") == code.replace("\n", "")
    context = chunk(doc, Cfg(min_tokens=0))[1].embed_text.split("context: ", 1)[1].split("\n\n", 1)[0]
    assert len(context) == 300 and context.endswith("…")


def test_pages_and_conditions_follow_merges_in_v1_and_v2_without_changing_text() -> None:
    sections = (
        _sec(1, ("A",), PROSE, page=3),
        _sec(2, ("A", "a1"), "tiny", page=5, page_end=6, condition="X == 1"),
        _sec(1, ("B",), PROSE, page=7, condition="Y"),
    )
    v2 = chunk(_doc(*sections), Cfg())
    assert [(c.page, c.page_end, c.condition) for c in v2] == [(3, 6, "X == 1"), (7, None, "Y")]
    v1 = chunk(_doc(*sections), Cfg(chunker_version=1, min_tokens=40))
    assert [(c.page, c.page_end, c.condition) for c in v1] == [(3, None, None), (5, 7, "X == 1; Y")]
    bare = tuple(Section(s.heading_path, s.heading, s.level, s.text, s.anchor) for s in sections)
    for version in (1, 2):
        cfg = Cfg(chunker_version=version, min_tokens=40)
        assert [c.embed_text_hash for c in chunk(_doc(*sections), cfg)] == [
            c.embed_text_hash for c in chunk(_doc(*bare), cfg)
        ]


def test_extract_documents_carry_no_extraction_metadata() -> None:
    doc = extract("# A\n\ntext\n", "text/markdown")
    assert doc.warnings == () and all(s.page is None and s.condition is None for s in doc.sections)
    assert all(isinstance(c, Chunk) and c.page is None and c.condition is None for c in chunk(doc))


# Golden values pin the chunker_version 2 output, as test_knowledge.py pins v1: a change here means
# the v2 algorithm changed, which needs a new chunker version.
def test_golden_hashes_for_chunker_version_2() -> None:
    md = (
        "# Guide\n\nIntro text for the guide, which explains what the commands below do.\n\n"
        "## Install\n\n```bash\npip install x\n```\n\n## Configure\n\n" + PROSE * 6 + "\n\n## Run\n\nRun it.\n"
    )
    chunks = chunk(extract(md, "text/markdown", title="Docs", source_url="https://d/x"), V2)
    digest = hashlib.sha256("|".join(c.embed_text_hash for c in chunks).encode()).hexdigest()
    assert ([c.heading_path for c in chunks], [c.overlap for c in chunks]) == GOLDEN_V2_SHAPE
    assert digest == GOLDEN_V2_DIGEST


GOLDEN_V2_SHAPE = ([("Guide",), ("Guide", "Configure"), ("Guide", "Configure")], [0, 0, 251])
# 2026-11-15 (review): changed deliberately before v2 shipped: "Run" now joins "Configure" (the
# latest group) instead of jumping back into "Guide".
GOLDEN_V2_DIGEST = "461f54261ffda6c819536a483b95d9b9466f2d2c527690d6b176407af4ff5ae7"


# --- layering and licence -------------------------------------------------------------------------


def test_importing_the_package_and_registry_never_loads_a_parser() -> None:
    code = (
        "import sys, eap_knowledge, eap_knowledge.formats;"
        "print(sorted(m for m in sys.modules if m.split('.')[0] in ('docutils', 'yaml')"
        " or m.startswith('eap_knowledge.formats.')))"
    )
    out = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "[]"


def test_extractor_modules_name_their_extractor_ids() -> None:
    from eap_knowledge import EXTRACTOR_VERSIONS

    assert {rst.EXTRACTOR, doxygen.EXTRACTOR, "yaml", "json"} <= set(EXTRACTOR_VERSIONS)
    assert extract_yaml("a: 1").format == "yaml" and extract_json('{"a": 1}').format == "json"


def test_the_installed_docutils_wheel_ships_no_gpl_code() -> None:
    """The licence check recorded in THIRD_PARTY_NOTICES.md, re-run against the installed wheel."""
    dist = metadata.distribution("docutils")
    assert dist.version == "0.23"
    files = [str(f) for f in dist.files or []]
    assert not [f for f in files if f.endswith(".el") or f.startswith("tools/")]
    package = Path(dist.locate_file("docutils"))
    gpl = [p for p in package.rglob("*") if p.is_file() and b"General Public License" in p.read_bytes()]
    assert gpl == []
    copying = Path(dist.locate_file(next(f for f in files if f.endswith("COPYING.rst")))).read_text()
    assert "tools/editors/emacs/rst.el" in copying  # the one GPL file, which the wheel leaves out
    notices = (Path(__file__).resolve().parents[3] / "THIRD_PARTY_NOTICES.md").read_text()
    # The docutils row must include version 0.23 (BSD license, no GPL code in wheel)
    # PyYAML row must include version 6.0.3 with MIT license
    assert "| docutils | 0.23 |" in notices
    assert "| PyYAML | 6.0.3 |" in notices and "MIT" in notices
    assert metadata.version("PyYAML") == "6.0.3"


# --- review fixes: expansion bombs, value errors, continuations, fences -----------------------------


def _fails_fast(fn, src: str, reason: str = "extract_limit") -> ExtractionFailed:  # noqa: ANN001
    import time

    start = time.monotonic()
    with pytest.raises(ExtractionFailed) as info:
        fn(src)
    assert info.value.reason == reason, info.value.detail
    assert time.monotonic() - start < 2.0
    return info.value


def test_yaml_text_value_alias_bomb_fails_fast() -> None:
    # 24 KB of input that would expand to 20 MB of text through 999 aliases of one string.
    src = 'a: &a "' + "x" * 20_000 + '"\nb: [' + ", ".join(["*a"] * 999) + "]\n"
    assert "expands" in _fails_fast(extract_yaml, src).detail


def test_yaml_shared_list_alias_bomb_fails_fast() -> None:
    # A 1 MB list repeated through 999 aliases (1 GB expanded).
    src = "a: &a [" + ", ".join(['"' + "y" * 1000 + '"'] * 1000) + "]\nb: [" + ", ".join(["*a"] * 999) + "]\n"
    _fails_fast(extract_yaml, src)


def test_yaml_aliases_within_the_budget_still_work() -> None:
    doc = extract_yaml('base: &b {timeout: 30, retries: 3}\ndev: *b\nprod: *b\n')
    assert [s.heading for s in doc.sections if s.level] == ["base", "dev", "prod"]


def test_rst_substitution_bomb_fails_fast() -> None:
    # One 9,000-character definition referenced 48,000 times (432 MB once replaced).
    src = ".. |x| replace:: " + "y" * 9000 + "\n\n" + "\n".join(" ".join(["|x|"] * 100) for _ in range(480)) + "\n"
    assert "substitutions" in _fails_fast(extract_rst, src).detail


def test_rst_nested_substitution_bomb_fails_fast() -> None:
    src = ".. |a| replace:: " + "z" * 100 + "\n" + "".join(
        f".. |{c}| replace:: " + f"|{p}| " * 10 + "\n" for p, c in zip("abcde", "bcdef", strict=True)
    ) + "\n|f|\n"
    _fails_fast(extract_rst, src)


def test_rst_ordinary_substitutions_are_not_limited() -> None:
    doc = extract_rst(".. |sdk| replace:: Processor SDK\n\n" + "The |sdk| is here. " * 500 + "\n")
    assert doc.sections[0].text.count("Processor SDK") == 500


def test_doxygen_ref_amplification_hits_the_output_budget(monkeypatch) -> None:
    from eap_knowledge.formats import _common

    monkeypatch.setattr(_common, "MIN_OUTPUT_BUDGET", 10_000)
    src = "\\page P " + "T" * 1000 + "\n" + "\n".join(["\\ref P"] * 100) + "\n"
    _fails_fast(extract_doxygen_markdown, src)
    assert extract_doxygen_markdown("\\page P Title\n" + "\n".join(["\\ref P"] * 100) + "\n").sections


@pytest.mark.parametrize(
    ("fn", "src", "where"),
    [
        (extract_yaml, "a: 1\nwhen: 2024-13-45\n", "line 2, column 7"),
        (extract_yaml, "a: " + "9" * 5000 + "\n", "line 1, column 4"),
        (extract_json, '{"a": ' + "9" * 5000 + "}", "JSON number"),
    ],
)
def test_impossible_values_fail_extract_failed_with_position_only(fn, src: str, where: str) -> None:  # noqa: ANN001
    error = _fails_fast(fn, src, "extract_failed")
    assert where in error.detail and "2024" not in str(error) and "9999" not in str(error)


def test_text_after_a_directive_with_sections_returns_to_the_outer_section() -> None:
    src = (
        "Top\n===\n\nBefore.\n\n.. ifconfig:: CONFIG_x == 'a'\n\n   Inner\n   -----\n\n   Inner text.\n\n"
        "After text.\n\nNext\n====\n\nNext text.\n"
    )
    doc = extract_rst(src, source_url=URL)
    assert [(s.heading_path, s.anchor, s.condition, s.text) for s in doc.sections if s.level] == [
        (("Top",), "top", "CONFIG_x == 'a'", "Before.\n\nApplies when: CONFIG_x == 'a'"),
        (("Top", "Inner"), "inner", "CONFIG_x == 'a'", "Inner text."),
        (("Top",), "top", None, "After text."),
        (("Next",), "next", None, "Next text."),
    ]
    # A directive with sections and nothing after it adds no empty continuation.
    assert len([s for s in extract_rst(src.split("After text.")[0]).sections if s.level]) == 2


def test_the_docutils_code_directive_never_puts_line_numbers_in_the_code() -> None:
    doc = extract_rst(".. code:: c\n   :number-lines: 10\n\n   int a;\n   int b;\n")
    assert doc.sections[0].text == "```c\nint a;\nint b;\n```"


def test_fences_outgrow_backticks_inside_the_code() -> None:
    rst_doc = extract_rst(".. code-block:: md\n\n   ````\n   x\n   ````\n")
    assert rst_doc.sections[0].text == "`````md\n````\nx\n````\n`````"
    md = doxygen_to_markdown("\\code{.md}\n```\nx\n```\n\\endcode\n# Real heading\n")
    assert md.split("\n")[:5] == ["````md", "```", "x", "```", "````"]
    doc = extract_doxygen_markdown("\\code{.md}\n```\nx\n```\n\\endcode\n# Real heading\n")
    assert [s.heading for s in doc.sections if s.level] == ["Real heading"]


# --- re-review fixes: guard bypasses, structured output factor, cap-derived alias budget ------------

_REFS = "\n".join(" ".join(["|x|"] * 100) for _ in range(480))


def _fails_fast_and_small(fn, src: str) -> ExtractionFailed:  # noqa: ANN001
    import tracemalloc

    tracemalloc.start()
    try:
        error = _fails_fast(fn, src)
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert peak < 64 * 1024 * 1024, peak  # stopped before building anything large
    return error


@pytest.mark.parametrize(
    "src",
    [
        ".. |x|\n   replace:: " + "y" * 9000 + "\n\n" + _REFS + "\n",  # the directive on the next line
        ".. |x| replace :: " + "y" * 9000 + "\n\n" + _REFS + "\n",  # one space before `::`
        ".. |X|  replace::  " + "y" * 9000 + "\n\n" + _REFS + "\n",  # case and spacing
        ".. |a b| replace:: " + "y" * 9000 + "\n\n" + "\n".join("|a\nb|" for _ in range(48_000)) + "\n",  # split ref
        ".. |x| replace:: " + "y" * 9000 + "\n\n| table | cell |\n" + _REFS.replace("|x| ", "| |x| ") + "\n",  # stray pipes
    ],
    ids=["two-line-definition", "space-before-colons", "case-and-spacing", "reference-over-a-line-break", "stray-pipes"],
)
def test_rst_substitution_bomb_variants_fail_before_parsing(src: str) -> None:
    assert "substitutions" in _fails_fast_and_small(extract_rst, src).detail


def test_doxygen_refs_to_a_huge_title_fail_before_the_line_is_built() -> None:
    src = "\\page P " + "T" * 50_000 + "\n" + "\n".join(" ".join(["\\ref P"] * 100) for _ in range(2000)) + "\n"
    _fails_fast_and_small(extract_doxygen_markdown, src)


def _nested_table(depth: int, width: int) -> object:
    return [i * 7 % 10 for i in range(width)] if depth == 0 else [_nested_table(depth - 1, width) for _ in range(2)]


def test_structured_output_may_grow_past_the_text_factor(monkeypatch) -> None:
    # A minified, deeply nested numeric table renders as indented YAML several times its size: that
    # is legitimate for YAML and JSON (16x), while rst keeps 4x.
    from eap_knowledge.formats import _common

    data = json.dumps({"table": _nested_table(8, 6)}, separators=(",", ":"))
    monkeypatch.setattr(_common, "MIN_OUTPUT_BUDGET", 1_000)
    doc = extract_json(data)
    rendered = sum(len(s.heading) + len(s.text) for s in doc.sections)
    assert rendered > _common.OUTPUT_FACTOR * len(data)  # over the text factor, still accepted
    assert rendered <= _common.STRUCTURED_OUTPUT_FACTOR * len(data)
    assert len(extract_yaml(data).sections) == len(doc.sections)  # JSON is YAML too


def test_the_yaml_alias_budget_follows_the_configured_structured_cap() -> None:
    # 100 aliases of a 100 KB list: about 10 MB expanded. Over 4 x the default 2 MiB cap, within
    # 4 x a raised 4 MiB cap (and dumped with YAML anchors, so the output stays small).
    src = "a: &a [" + ", ".join(['"' + "z" * 100 + '"'] * 1000) + "]\nb: [" + ", ".join(["*a"] * 100) + "]\n"
    with pytest.raises(ExtractionFailed) as info:
        extract_yaml(src)
    assert info.value.reason == "extract_limit"
    assert structured.MAX_YAML_EXPANDED_CHARS == 4 * structured.DEFAULT_STRUCTURED_CAP == 8 * 1024 * 1024
    doc = extract_yaml(src, structured_cap=4 * 1024 * 1024)
    assert [s.heading for s in doc.sections if s.level] == ["a", "b"]
    with pytest.raises(ExtractionFailed):
        extract_yaml(src, structured_cap=1024 * 1024)
