"""RAG P1.A2 acceptance: golden behaviour on real TI excerpts, parity with the external import tool,
hostile inputs, the condition evaluator table, chunker v2 rules and the import-linter contract.

The TI excerpts are snapshotted from ~/ti-rag/repos (processor-sdk-doc, mcupsdk-core,
simplelink-zephyr) so nothing here reads that tree.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
import time
from dataclasses import dataclass, replace
from pathlib import Path

import pytest
from eap_schemas.knowledge import ExtractConfig

from eap_knowledge import Chunk, Document, ExtractionFailed, Section, chunk
from eap_knowledge.formats.doxygen import doxygen_to_markdown, extract_doxygen_markdown
from eap_knowledge.formats.rst import evaluate_condition, extract_rst
from eap_knowledge.formats.structured import extract_json, extract_yaml

URL = "https://ti.example/page.html"
CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
WORDS = re.compile(r"[A-Za-z0-9_]+")


@dataclass(frozen=True)
class Cfg:
    max_tokens: int = 512
    overlap_tokens: int = 64
    min_tokens: int = 64
    chunker_version: int = 2


def _by_path(doc: Document) -> dict[tuple[str, ...], Section]:
    return {s.heading_path: s for s in doc.sections}


def _no_controls(doc: Document) -> None:
    for s in doc.sections:
        assert not CONTROL.search(s.heading + s.text), s.heading_path
        assert not CONTROL.search(s.condition or ""), s.heading_path


# --- snapshots from processor-sdk-doc (rst) -------------------------------------------------------

UART_RST = """\
##############
Flash via UART
##############

This tool uses UART as the transport or interface to send the file to flash to the EVM.

The UART Flash tool is part of MCU+SDK installer and can be downloaded from |__SDK_DOWNLOAD_URL__|.

.. ifconfig:: CONFIG_part_variant in ('AM62X')

    `MCU+SDK Flash Tools Documentation  <https://software-dl.ti.com/mcu-plus-sdk/esd/AM62X/latest/exports/docs/api_guide_am62x/TOOLS_FLASH.html>`__.

.. ifconfig:: CONFIG_part_variant in ('AM64X')

   `MCU+SDK Flash Tools Documentation  <https://software-dl.ti.com/mcu-plus-sdk/esd/AM64X/latest/exports/docs/api_guide_am62x/TOOLS_FLASH.html>`__.

.. ifconfig:: CONFIG_part_variant in ('AM62PX')

   `MCU+SDK Flash Tools Documentation  <https://software-dl.ti.com/mcu-plus-sdk/esd/AM62PX/11_01_01_08/exports/docs/api_guide_am62px/TOOLS_FLASH.html>`__.
"""

SETUP_RST = """\
.. _u-boot-build-guide-setup-k3:

#####
Setup
#####

*************************
Install host dependencies
*************************

To install host dependencies for building TI U-boot source (standalone)
on Ubuntu 22.04+, run the following command in the terminal prompt:

.. code-block:: console

   sudo apt install git xz-utils build-essential autoconf flex bison libssl-dev bc libncurses-dev \\
   swig yamllint libgnutls28-dev

.. note::

   The recommended host machine for building U-Boot is Ubuntu 22.04.

   .. code-block:: console

      pip install dataclasses pyelftools jsonschema yamllint importlib-resources

.. _Getting the U-Boot Source Code-label:

***********************
Getting the Source Code
***********************

.. ifconfig:: CONFIG_part_variant in ('AM62LX')

   .. note::

      AM62L devices use TF-A BL-1 boot loader to configure LPDDR4 to
      enable secondary program loader.

The easiest way to get access to the source code is by
downloading and installing the Processor SDK Linux. Once installed,
the source code is in the SDK at the path :file:`<path to tisdk>/board-support`.

*******************************
Setup Cross Compile Environment
*******************************

.. include:: ../../Overview/GCC_ToolChain.rst
   :start-after: .. start_include_yocto_toolchain_host_setup
   :end-before: .. end_include_yocto_toolchain_host_setup
"""

MAKEFILE_RST = """\
##################
Top Level Makefile
##################

Please run the following command to install all packages required by the
makefile targets.

.. ifconfig:: CONFIG_part_variant in ('AM62X')

   .. important::

        * AM62x installer supports ``am62xx-evm`` and ``am62xx-lp-evm`` platforms.
        * No special arguments are needed to build for ``am62xxsip-evm``.

.. ifconfig:: CONFIG_part_variant not in ('AM62X', 'AM64X', 'AM62PX')

   .. code-block:: console

      host# sudo apt-get install build-essential autoconf automake bison flex

.. ifconfig:: CONFIG_part_variant in ('AM62PX', 'AM62X', 'AM64X')

   .. code-block:: console

      host# sudo apt-get install build-essential libgnutls28-dev python3

   Following pip packages are also needed for jailhouse

   .. code-block:: console

      host# pip3 install jsonschema pyelftools

.. ifconfig:: CONFIG_sdk in ('JACINTO','j7_foundational')

    .. rubric:: Compiler toolchain
       :name: compiler-toolchain

    Download and extract them into your home directory.
"""

BEAGLE_RST = """\
##########
BeaglePlay
##########

The BeaglePlay development board from `BeagleBoard
<https://www.beagleboard.org/>`__ uses the |__PART_FAMILY_DEVICE_NAMES__|. It is
a low-cost, primarily community-supported development platform.

.. list-table:: Supported Distributions
   :header-rows: 1
   :widths: 15, 15

   * - Component
     - Branch
   * - Arago
     - `Scarthgap <https://git.yoctoproject.org/meta-ti/tree/meta-beagle/README?h=scarthgap>`__

.. toctree::

   android/Application_Notes_BeaglePlay

For more information see
`<https://docs.beagle.cc/boards/beagleplay/index.html>`__.
"""

VULKAN_RST = """\
.. include:: <isonum.txt>

.. _vulkan-api-overview:

######
Vulkan
######

The Vulkan\\ |reg| libraries packaged with |__SDK_FULL_NAME__| offer acceleration
for graphics stacks such as Qt and GTK.

Vulkan\\ |reg| is a relatively new API. Similar to OpenCL\\ |trade|, Vulkan\\ |reg| has an
Installable Client Driver (ICD) Loader. A useful tool is :command:`vulkaninfo`.
"""

# --- snapshots from mcupsdk-core (Doxygen Markdown) -----------------------------------------------

BOOTLOADER_MD = """\
# BOOTLOADER {#DRIVERS_BOOTLOADER_PAGE}

[TOC]

The Bootloader module provides APIs to write bootloader applications.

## Features Supported

- OSPI Boot
- MEM Boot (Boot media is SOC memory)

## SysConfig Features

@VAR_SYSCFG_USAGE_NOTE

- Bootloader instance name

## Example Usage

Include the below file to access the APIs
\\snippet Bootloader_sample.c include

Booting Cores Example
\\if SOC_AM65X
\\snippet Bootloader_sample.c bootcores_am65x
\\else
\\snippet Bootloader_sample.c bootcores
\\endif

## API

\\ref DRV_BOOTLOADER_MODULE
"""

TASKP_MD = """\
# Task {#KERNEL_DPL_TASK_PAGE}

[TOC]

\\attention This module is not applicable for NO RTOS environment

## Important Usage Guidelines
\\if SOC_AM65X
- On R5F, make sure memory provided as stack is 32b aligned
\\else
- On R5F, and M4F, make sure memory provided as stack is 32b aligned
\\endif
- TaskP_loadResetAll() needs to be called in the application.
"""

FLASH_MD = """\
# Flashing Tools {#TOOLS_FLASH}

\\cond SOC_AM263X || SOC_AM243X
- \\ref TOOLS_TI_UNIFLASH_TOOL
\\endcond
- \\ref TOOLS_FLASH_UART_UNIFLASH

### UART Uniflash {#TOOLS_FLASH_UART_UNIFLASH}

#### Flashing the files

- Set EVM in \\ref BOOTMODE_UART and power on the EVM

- Run below python command on the Windows command prompt (`cmd.exe`) or Linux bash shell to flash the files.

        cd ${SDK_INSTALL_PATH}/tools/boot
        python uart_uniflash.py -p {name of your UART com port} --cfg={path to your edited config file}

- At each step in the flashing your will see success or error messages.

\\cond SOC_AM243X || SOC_AM64X || SOC_AM65X

- If flashing is successful, power OFF the EVM, set the EVM to \\ref BOOTMODE_OSPI and power ON the EVM.
\\endcond
"""

# --- snapshots from simplelink-zephyr (YAML) ------------------------------------------------------

SPI_YAML = """\
# Copyright (c) 2019 Brett Witherspoon
# SPDX-License-Identifier: Apache-2.0

description: TI SimpleLink CC35xx

compatible: "ti,cc35xx-spi"

include: [spi-controller.yaml, pinctrl-device.yaml, base.yaml]

properties:
  reg:
    required: true
"""

TIMER_YAML = """\
description: TI SimpleLink CC13xx/CC26xx Timer Node

compatible: "ti,cc13xx-cc26xx-timer"

include: base.yaml

properties:
  reg:
    required: true

  interrupts:
    required: true
"""

BOARD_YAML = """\
identifier: cc1352r_sensortag
name: TI SimpleLink CC1352R SensorTag
type: mcu
arch: arm
ram: 80
flash: 352
toolchain:
  - zephyr
  - gnuarmemb
supported:
  - gpio
  - i2c
vendor: ti
"""

# Output of ~/ti-rag/import/ti_import.py rst_to_markdown for the snapshots above (frozen).
ORACLE = {
    'UART_RST': "# Flash via UART\n\nThis tool uses UART as the transport or interface to send the file to flash to the EVM.\n\nThe UART Flash tool is part of MCU+SDK installer and can be downloaded from {SDK_DOWNLOAD_URL}.\n\n*Applies when: CONFIG_part_variant in ('AM62X')*\n\n[MCU+SDK Flash Tools Documentation](https://software-dl.ti.com/mcu-plus-sdk/esd/AM62X/latest/exports/docs/api_guide_am62x/TOOLS_FLASH.html).\n\n*Applies when: CONFIG_part_variant in ('AM64X')*\n\n[MCU+SDK Flash Tools Documentation](https://software-dl.ti.com/mcu-plus-sdk/esd/AM64X/latest/exports/docs/api_guide_am62x/TOOLS_FLASH.html).\n\n*Applies when: CONFIG_part_variant in ('AM62PX')*\n\n[MCU+SDK Flash Tools Documentation](https://software-dl.ti.com/mcu-plus-sdk/esd/AM62PX/11_01_01_08/exports/docs/api_guide_am62px/TOOLS_FLASH.html).\n",
    'SETUP_RST': "# Setup\n\n## Install host dependencies\n\nTo install host dependencies for building TI U-boot source (standalone) on Ubuntu 22.04+, run the following command in the terminal prompt:\n\n```console\nsudo apt install git xz-utils build-essential autoconf flex bison libssl-dev bc libncurses-dev \\\nswig yamllint libgnutls28-dev\n```\n\n> **Note:** The recommended host machine for building U-Boot is Ubuntu 22.04.\n>\n> ```console\n> pip install dataclasses pyelftools jsonschema yamllint importlib-resources\n> ```\n\n## Getting the Source Code\n\n*Applies when: CONFIG_part_variant in ('AM62LX')*\n\n> **Note:** AM62L devices use TF-A BL-1 boot loader to configure LPDDR4 to enable secondary program loader.\n\nThe easiest way to get access to the source code is by downloading and installing the Processor SDK Linux. Once installed, the source code is in the SDK at the path `<path to tisdk>/board-support`.\n\n## Setup Cross Compile Environment\n\n*[include not available: ../../Overview/GCC_ToolChain.rst]*\n",
    'BEAGLE_RST': '# BeaglePlay\n\nThe BeaglePlay development board from [BeagleBoard](https://www.beagleboard.org/) uses the {PART_FAMILY_DEVICE_NAMES}. It is a low-cost, primarily community-supported development platform.\n\n**Supported Distributions**\n\n| Component | Branch |\n|---|---|\n| Arago | [Scarthgap](https://git.yoctoproject.org/meta-ti/tree/meta-beagle/README?h=scarthgap) |\n\nFor more information see https://docs.beagle.cc/boards/beagleplay/index.html.\n',
    'VULKAN_RST': '# Vulkan\n\nThe Vulkan® libraries packaged with {SDK_FULL_NAME} offer acceleration for graphics stacks such as Qt and GTK.\n\nVulkan® is a relatively new API. Similar to OpenCL™, Vulkan® has an Installable Client Driver (ICD) Loader. A useful tool is `vulkaninfo`.\n',
    'MAKEFILE_RST': "# Top Level Makefile\n\nPlease run the following command to install all packages required by the makefile targets.\n\n*Applies when: CONFIG_part_variant in ('AM62X')*\n\n> **Important:** - AM62x installer supports `am62xx-evm` and `am62xx-lp-evm` platforms.\n> - No special arguments are needed to build for `am62xxsip-evm`.\n\n*Applies when: CONFIG_part_variant not in ('AM62X', 'AM64X', 'AM62PX')*\n\n```console\nhost# sudo apt-get install build-essential autoconf automake bison flex\n```\n\n*Applies when: CONFIG_part_variant in ('AM62PX', 'AM62X', 'AM64X')*\n\n```console\nhost# sudo apt-get install build-essential libgnutls28-dev python3\n```\n\nFollowing pip packages are also needed for jailhouse\n\n```console\nhost# pip3 install jsonschema pyelftools\n```\n\n*Applies when: CONFIG_sdk in ('JACINTO','j7_foundational')*\n\n## Compiler toolchain\n\nDownload and extract them into your home directory.\n",
}


# --- 1. golden behaviour on real TI rst ---------------------------------------------------------


def test_uart_flash_page_surfaces_every_ifconfig_condition_when_unconfigured() -> None:
    doc = extract_rst(UART_RST, title="Flash_via_UART", source_url=URL)
    _no_controls(doc)
    page = _by_path(doc)[("Flash via UART",)]
    assert page.anchor == "flash-via-uart"
    assert page.condition == (
        "CONFIG_part_variant in ('AM62X'); CONFIG_part_variant in ('AM64X'); CONFIG_part_variant in ('AM62PX')"
    )
    assert page.text.count("Applies when: CONFIG_part_variant in (") == 3
    assert "|__SDK_DOWNLOAD_URL__|" in page.text  # an undefined substitution stays as text
    assert doc.warnings == ()


@pytest.mark.parametrize(
    ("variant", "kept", "dropped"),
    [("AM62X", "esd/AM62X/", ("esd/AM64X/", "esd/AM62PX/")), ("AM64X", "esd/AM64X/", ("esd/AM62X/", "esd/AM62PX/"))],
)
def test_uart_flash_page_with_a_variant_keeps_only_that_branch(variant: str, kept: str, dropped: tuple) -> None:
    doc = extract_rst(UART_RST, ExtractConfig(rst_config={"CONFIG_part_variant": variant}), source_url=URL)
    page = _by_path(doc)[("Flash via UART",)]
    assert kept in page.text
    assert not any(d in page.text for d in dropped)
    assert page.condition is None and "Applies when" not in page.text


def test_setup_page_nested_headings_anchors_fences_notes_and_dropped_include() -> None:
    doc = extract_rst(SETUP_RST, source_url=URL)
    _no_controls(doc)
    got = [(s.heading_path, s.level, s.anchor) for s in doc.sections if s.heading]
    assert got == [
        (("Setup",), 1, "setup"),
        (("Setup", "Install host dependencies"), 2, "install-host-dependencies"),
        (("Setup", "Getting the Source Code"), 2, "getting-the-source-code"),
        (("Setup", "Setup Cross Compile Environment"), 2, "setup-cross-compile-environment"),
    ]
    assert doc.warnings == ("include_dropped",)
    deps = _by_path(doc)[("Setup", "Install host dependencies")].text
    assert deps.count("```console") == 2  # the code-block and the one nested in the note
    assert "Note: The recommended host machine" in deps
    src = _by_path(doc)[("Setup", "Getting the Source Code")]
    assert src.condition == "CONFIG_part_variant in ('AM62LX')"
    assert "Note: AM62L devices use TF-A BL-1" in src.text and "`<path to tisdk>/board-support`" in src.text
    assert _by_path(doc)[("Setup", "Setup Cross Compile Environment")].text == ""


def test_setup_page_ifconfig_false_branch_is_dropped_with_its_nested_note() -> None:
    cfg = ExtractConfig(rst_config={"CONFIG_part_variant": "AM62X"})
    doc = extract_rst(SETUP_RST, cfg, source_url=URL)
    assert "AM62L devices" not in "".join(s.text for s in doc.sections)
    assert all(s.condition is None for s in doc.sections)


def _makefile(variant: str | None, **extra: object) -> Section:
    cfg = {"CONFIG_part_variant": variant, **extra} if variant else None
    doc = extract_rst(MAKEFILE_RST, ExtractConfig(rst_config=cfg) if cfg else None, source_url=URL)
    _no_controls(doc)
    return _by_path(doc)[("Top Level Makefile",)]


def test_makefile_page_unconfigured_records_all_four_conditions_in_order() -> None:
    sec = _makefile(None)
    assert sec.condition == (
        "CONFIG_part_variant in ('AM62X'); CONFIG_part_variant not in ('AM62X', 'AM64X', 'AM62PX'); "
        "CONFIG_part_variant in ('AM62PX', 'AM62X', 'AM64X'); CONFIG_sdk in ('JACINTO','j7_foundational')"
    )
    assert sec.text.count("Applies when:") == 4
    assert "Important: " in sec.text and "**Compiler toolchain**" in sec.text


@pytest.mark.parametrize(
    ("variant", "has_apt_basic", "has_apt_gnutls", "has_important"),
    [("AM62X", False, True, True), ("AM64X", False, True, False), ("AM335X", True, False, False)],
)
def test_makefile_page_variant_matrix(variant: str, has_apt_basic: bool, has_apt_gnutls: bool, has_important: bool) -> None:
    sec = _makefile(variant)
    assert ("autoconf automake bison flex" in sec.text) is has_apt_basic
    assert ("libgnutls28-dev" in sec.text) is has_apt_gnutls
    assert ("am62xx-lp-evm" in sec.text) is has_important
    # CONFIG_sdk is not configured, so that one branch stays, unevaluated, and is the only condition.
    assert sec.condition == "CONFIG_sdk in ('JACINTO','j7_foundational')"


def test_makefile_page_fully_configured_has_no_condition_left() -> None:
    sec = _makefile("AM64X", CONFIG_sdk="PROCESSOR")
    assert sec.condition is None and "Compiler toolchain" not in sec.text
    assert "Applies when" not in sec.text


def test_beagle_page_table_toctree_and_links() -> None:
    doc = extract_rst(BEAGLE_RST, source_url=URL)
    _no_controls(doc)
    sec = _by_path(doc)[("BeaglePlay",)]
    assert sec.anchor == "beagleplay" and doc.warnings == ()  # toctree is dropped silently
    assert "| Component | Branch |\n|---|---|\n| Arago | [Scarthgap](" in sec.text
    assert "**Supported Distributions**" in sec.text
    assert "Application_Notes_BeaglePlay" not in sec.text
    assert "[BeagleBoard](https://www.beagleboard.org/)" in sec.text
    assert "|__PART_FAMILY_DEVICE_NAMES__|" in sec.text


def test_vulkan_page_nul_escapes_leave_no_control_characters() -> None:
    doc = extract_rst(VULKAN_RST, source_url=URL)
    _no_controls(doc)
    sec = _by_path(doc)[("Vulkan",)]
    assert sec.anchor == "vulkan" and doc.warnings == ("include_dropped",)  # `<isonum.txt>` is dropped
    assert "Vulkan|reg| libraries" in sec.text  # `\ ` is removed with its NUL, the substitution stays
    assert "OpenCL|trade|," in sec.text and ":command:" not in sec.text and "`vulkaninfo`" in sec.text


def test_every_real_snapshot_is_free_of_nul_and_c0_through_the_chunker() -> None:
    for src in (UART_RST, SETUP_RST, MAKEFILE_RST, BEAGLE_RST, VULKAN_RST):
        doc = extract_rst(src, source_url=URL)
        for c in chunk(doc, Cfg()):
            assert not CONTROL.search(c.text + c.embed_text + (c.condition or ""))


def test_include_only_pages_extract_nothing_and_count_the_dropped_include() -> None:
    sorte = ".. _industrial-protocols-sorte:\n\n.. include:: Industrial_Protocols/_SORTE.rst\n"
    for src in (".. include:: /common/Tools/_SYSFW_Trace_Parser.rst\n", sorte):
        doc = extract_rst(src, source_url=URL)
        assert doc.warnings == ("include_dropped",)
        assert doc.extracted_chars == 0 and chunk(doc, Cfg()) == []


def test_dropped_directive_warnings_are_counted_once_per_occurrence() -> None:
    src = (
        "Title\n=====\n\nText.\n\n"
        ".. include:: a.rst\n\n.. include:: b.rst\n   :start-after: x\n\n"
        ".. literalinclude:: ../src/main.c\n   :language: c\n\n"
        ".. raw:: html\n\n   <script>alert(1)</script>\n\n"
        ".. csv-table:: T\n   :url: https://evil.example/data.csv\n\n"
        ".. toctree::\n\n   other\n\nTail.\n"
    )
    doc = extract_rst(src, source_url=URL)
    assert sorted(doc.warnings) == sorted(
        ["include_dropped", "include_dropped", "literalinclude_dropped", "raw_dropped", "csv_table_source_dropped"]
    )
    text = "".join(s.text for s in doc.sections)
    assert "script" not in text and "evil.example" not in text and "main.c" not in text
    assert text.startswith("Text.") and text.endswith("Tail.")


# --- 1. golden behaviour on real TI Doxygen Markdown and YAML -------------------------------------


def test_bootloader_page_headings_anchors_toc_vars_and_else_branch() -> None:
    doc = extract_doxygen_markdown(BOOTLOADER_MD, source_url=URL)
    _no_controls(doc)
    got = [(s.heading_path, s.anchor) for s in doc.sections if s.heading]
    assert got == [
        (("BOOTLOADER",), "DRIVERS_BOOTLOADER_PAGE"),  # {#ID} wins over the slug
        (("BOOTLOADER", "Features Supported"), "features-supported"),
        (("BOOTLOADER", "SysConfig Features"), "sysconfig-features"),
        (("BOOTLOADER", "Example Usage"), "example-usage"),
        (("BOOTLOADER", "API"), "api"),
    ]
    sec = _by_path(doc)
    assert "[TOC]" not in doc.sections[1].text and "{#" not in "".join(s.heading for s in doc.sections)
    assert sec[("BOOTLOADER", "Example Usage")].condition == "SOC_AM65X"
    usage = sec[("BOOTLOADER", "Example Usage")].text
    assert "Applies when: SOC_AM65X" in usage and "Otherwise:" in usage
    assert usage.count("not included)") == 3 and "\\if" not in usage and "\\endif" not in usage
    assert sec[("BOOTLOADER", "API")].text == "DRV_BOOTLOADER_MODULE"  # \ref to an unknown id: its id


def test_taskp_page_attention_label_and_if_else_condition() -> None:
    doc = extract_doxygen_markdown(TASKP_MD, source_url=URL)
    top = _by_path(doc)[("Task",)]
    assert top.text == "Attention: This module is not applicable for NO RTOS environment"
    sec = _by_path(doc)[("Task", "Important Usage Guidelines")]
    assert sec.condition == "SOC_AM65X"
    assert sec.text.index("Applies when: SOC_AM65X") < sec.text.index("Otherwise:") < sec.text.index("M4F")
    assert doc.warnings == ()


def test_flash_page_cond_blocks_refs_and_indented_code_survive() -> None:
    doc = extract_doxygen_markdown(FLASH_MD, source_url=URL)
    _no_controls(doc)
    by = _by_path(doc)
    assert by[("Flashing Tools",)].condition == "SOC_AM263X || SOC_AM243X"
    assert by[("Flashing Tools", "UART Uniflash")].anchor == "TOOLS_FLASH_UART_UNIFLASH"
    step = by[("Flashing Tools", "UART Uniflash", "Flashing the files")]
    assert step.anchor == "flashing-the-files" and step.level == 4
    assert step.condition == "SOC_AM243X || SOC_AM64X || SOC_AM65X"
    assert "Set EVM in BOOTMODE_UART and power on" in step.text and "\\ref" not in step.text
    assert "        python uart_uniflash.py -p {name of your UART com port}" in step.text  # indented code kept
    assert "\\cond" not in step.text and "\\endcond" not in step.text


def test_doxygen_conversion_is_stable_and_leaves_no_commands() -> None:
    for src in (BOOTLOADER_MD, TASKP_MD, FLASH_MD):
        md = doxygen_to_markdown(src)
        assert doxygen_to_markdown(src) == md
        assert not re.search(r"^\s*[\\@](cond|endcond|if|else|endif|ref|snippet|attention)\b", md, re.M)
        assert "@VAR_" not in md or "BOOTLOADER" in src  # placeholder lines are the only `@` left


def test_device_tree_binding_yaml_one_section_per_top_level_key() -> None:
    doc = extract_yaml(SPI_YAML, source_url=URL)
    _no_controls(doc)
    assert [(s.heading, s.anchor) for s in doc.sections if s.heading] == [
        ("description", "description"), ("compatible", "compatible"), ("include", "include"), ("properties", "properties"),
    ]
    by = _by_path(doc)
    assert by[("compatible",)].text == "compatible: ti,cc35xx-spi"
    assert by[("include",)].text == "include:\n- spi-controller.yaml\n- pinctrl-device.yaml\n- base.yaml"
    assert by[("properties",)].text == "properties:\n  reg:\n    required: true"
    assert doc.format == "yaml" and "Copyright" not in "".join(s.text for s in doc.sections)  # comments are dropped


def test_timer_binding_is_not_split_below_max_tokens_and_title_comes_from_the_argument() -> None:
    doc = extract_yaml(TIMER_YAML, title="ti,cc13xx-cc26xx-timer", source_url=URL)
    assert doc.title == "ti,cc13xx-cc26xx-timer"
    assert _by_path(doc)[("properties",)].text == "properties:\n  reg:\n    required: true\n  interrupts:\n    required: true"
    chunks = chunk(doc, Cfg(min_tokens=0))
    assert [c.anchor.rsplit("#", 1)[1] for c in chunks] == ["description", "compatible", "include", "properties"]
    assert "ti,cc13xx-cc26xx-timer" in chunks[0].embed_text


def test_board_yaml_scalars_and_lists_keep_their_keys() -> None:
    doc = extract_yaml(BOARD_YAML, source_url=URL)
    by = _by_path(doc)
    assert by[("ram",)].text == "ram: 80" and by[("flash",)].text == "flash: 352"
    assert by[("toolchain",)].text == "toolchain:\n- zephyr\n- gnuarmemb"
    assert by[("name",)].text == "name: TI SimpleLink CC1352R SensorTag"
    assert [s.anchor for s in doc.sections if s.heading][0] == "identifier"


# --- 2. parity with ~/ti-rag/import/ti_import.py (frozen output, see ORACLE above) -----------------


def _oracle_headings(md: str) -> list[tuple[int, str]]:
    return [(len(m.group(1)), m.group(2)) for m in re.finditer(r"^(#{1,6}) (.+)$", md, re.M)]


def _words(text: str) -> list[str]:
    text = text.replace("®", "|reg|").replace("™", "|trade|")
    text = re.sub(r"\{(__[A-Z_]+__|[A-Z_]+)\}", lambda m: f"|__{m.group(1).strip('_')}__|", text)  # {X} -> |__X__|
    text = re.sub(r"\*\[include not available:[^\]]*\]\*", "", text)  # the oracle's own placeholder
    text = re.sub(r"\b(Note|Important|Warning|Applies when)\b:?", "", text)
    return WORDS.findall(text.replace("**", ""))


@pytest.mark.parametrize("name", ["UART_RST", "SETUP_RST", "BEAGLE_RST", "VULKAN_RST"])
def test_headings_and_nesting_match_the_external_tool(name: str) -> None:
    doc = extract_rst(globals()[name], source_url=URL)
    ours = [(s.level, s.heading) for s in doc.sections if s.heading]
    assert ours == _oracle_headings(ORACLE[name])


@pytest.mark.parametrize("name", ["UART_RST", "SETUP_RST", "BEAGLE_RST", "VULKAN_RST"])
def test_body_words_match_the_external_tool_in_order(name: str) -> None:
    doc = extract_rst(globals()[name], source_url=URL)
    ours = _words("\n\n".join(s.heading + "\n\n" + s.text for s in doc.sections))
    assert ours == _words(ORACLE[name])


def test_the_external_tool_flattened_ifconfig_and_we_record_the_same_condition_text() -> None:
    conds = re.findall(r"^\*Applies when: (.+)\*$", ORACLE["UART_RST"], re.M)
    doc = extract_rst(UART_RST, source_url=URL)
    assert "; ".join(conds) == _by_path(doc)[("Flash via UART",)].condition
    assert conds == re.findall(r"^Applies when: (.+)$", _by_path(doc)[("Flash via UART",)].text, re.M)


def test_makefile_conditions_match_the_external_tool() -> None:
    conds = re.findall(r"^\*Applies when: (.+)\*$", ORACLE["MAKEFILE_RST"], re.M)
    assert len(conds) == 4
    assert "; ".join(conds) == _makefile(None).condition


def test_the_known_differences_from_the_external_tool_are_the_intended_ones() -> None:
    # The tool resolves isonum (|reg| -> the registered sign) because it reads includes; the product
    # never reads files, so the substitution stays as text. The tool also keeps a placeholder for an
    # unreadable include where the product drops it and counts include_dropped.
    assert "Vulkan® libraries" in ORACLE["VULKAN_RST"]
    ours = extract_rst(VULKAN_RST, source_url=URL)
    assert "Vulkan|reg| libraries" in ours.sections[1].text and "®" not in ours.sections[1].text
    assert "include not available" in ORACLE["SETUP_RST"]
    assert "include not available" not in "".join(s.text for s in extract_rst(SETUP_RST).sections)


# Frozen from ti_import.doxygen_clean (its `{#ID}` stripped headings, and the `*Applies to:*` lines).
DOXYGEN_ORACLE = {
    "BOOTLOADER_MD": (
        [(1, "BOOTLOADER"), (2, "Features Supported"), (2, "SysConfig Features"), (2, "Example Usage"), (2, "API")],
        ["SOC_AM65X"],
    ),
    "TASKP_MD": ([(1, "Task"), (2, "Important Usage Guidelines")], ["SOC_AM65X"]),
    "FLASH_MD": (
        [(1, "Flashing Tools"), (3, "UART Uniflash"), (4, "Flashing the files")],
        ["SOC_AM263X || SOC_AM243X", "SOC_AM243X || SOC_AM64X || SOC_AM65X"],
    ),
}


@pytest.mark.parametrize("name", sorted(DOXYGEN_ORACLE))
def test_doxygen_headings_and_conditions_match_the_external_tool(name: str) -> None:
    headings, conditions = DOXYGEN_ORACLE[name]
    doc = extract_doxygen_markdown(globals()[name], source_url=URL)
    assert [(s.level, s.heading) for s in doc.sections if s.heading] == headings
    assert [s.condition for s in doc.sections if s.condition] == conditions
    # The tool keeps the commands it cannot read as raw text; we leave none behind.
    assert "\\attention" not in "".join(s.text for s in doc.sections)


def test_doxygen_text_content_matches_the_external_tool_modulo_labels() -> None:
    doc = extract_doxygen_markdown(FLASH_MD, source_url=URL)
    text = "\n".join(s.text for s in doc.sections)
    for needle in (
        "Set EVM in BOOTMODE_UART and power on the EVM",
        "At each step in the flashing your will see success or error messages.",
        "python uart_uniflash.py -p {name of your UART com port} --cfg={path to your edited config file}",
        "If flashing is successful, power OFF the EVM, set the EVM to BOOTMODE_OSPI and power ON the EVM.",
    ):
        assert needle in text


# --- 3. hostile inputs ----------------------------------------------------------------------------

MARK = "TOPSECRETMARK"


def _laughs(depth: int, fan: int) -> str:
    names = "abcdefghijklmnopqrstuvwxyz"
    out = [f"a: &a [{','.join(['x'] * fan)}]"]
    for p, c in zip(names[: depth - 1], names[1:depth], strict=True):
        out.append(f"{c}: &{c} [{','.join('*' + p for _ in range(fan))}]")
    return "\n".join(out) + "\n"


@pytest.mark.parametrize(("depth", "fan"), [(9, 9), (11, 10), (20, 10)])
def test_yaml_billion_laughs_fails_extract_limit_in_well_under_a_second(depth: int, fan: int) -> None:
    started = time.perf_counter()
    with pytest.raises(ExtractionFailed) as info:
        extract_yaml(_laughs(depth, fan))
    assert time.perf_counter() - started < 2.0
    assert info.value.code == "extract_limit"
    assert info.value.user_message == "The document is over an extraction limit (pages, nodes, nesting or image size)."


def test_yaml_laughs_inside_a_later_document_of_a_stream_is_caught_too() -> None:
    with pytest.raises(ExtractionFailed) as info:
        extract_yaml("ok: 1\n---\n" + _laughs(11, 10))
    assert info.value.reason == "extract_limit"


def test_yaml_merge_key_chains_do_not_blow_up() -> None:
    src = "a: &a {k: 1}\nb: &b {<<: [*a, *a, *a, *a], j: 2}\nc: &c {<<: [*b, *b, *b, *b]}\n"
    started = time.perf_counter()
    doc = extract_yaml(src)
    assert time.perf_counter() - started < 2.0
    assert [s.heading for s in doc.sections if s.heading] == ["a", "b", "c"]


@pytest.mark.parametrize("fn", [extract_yaml, extract_json])
def test_deeply_nested_objects_fail_extract_limit_quickly(fn) -> None:  # noqa: ANN001
    src = "".join('{"a":' for _ in range(200)) + "1" + "}" * 200
    if fn is extract_yaml:
        src = "".join(" " * i + "a:\n" for i in range(200)) + " " * 200 + "1\n"
    started = time.perf_counter()
    with pytest.raises(ExtractionFailed) as info:
        fn(src)
    assert info.value.reason == "extract_limit" and time.perf_counter() - started < 2.0


MALFORMED_YAML = {
    "tab": f"{MARK}:\n\t- x",
    "unclosed": f'a: "{MARK}',
    "python_tag": f"a: !!python/object/apply:os.system [{MARK}]",
    "undefined_alias": f"a: &x 1\nb: *{MARK}",
    "unhashable_key": f"? [{MARK}]\n: 1",
    "bad_binary": f"a: !!binary {MARK}!!",
}


@pytest.mark.parametrize("key", sorted(MALFORMED_YAML))
def test_malformed_yaml_fails_extract_failed_and_the_error_carries_no_content(key: str) -> None:
    with pytest.raises(ExtractionFailed) as info:
        extract_yaml(MALFORMED_YAML[key])
    err = info.value
    assert err.code == "extract_failed"
    for text in (str(err), err.detail, err.user_message, repr(err), repr(err.args)):
        assert MARK not in text and "os.system" not in text


@pytest.mark.parametrize("src", [f'{{"{MARK}": 1,}}', f'{{"a": {MARK}}}', f'{{"{MARK}":', f"[{MARK}]"])
def test_malformed_json_fails_extract_failed_and_the_error_carries_no_content(src: str) -> None:
    with pytest.raises(ExtractionFailed) as info:
        extract_json(src)
    assert info.value.code == "extract_failed"
    assert MARK not in str(info.value) + info.value.detail + info.value.user_message + repr(info.value.args)


def test_rst_limit_errors_carry_no_content() -> None:
    long_line = "Title\n=====\n\n" + MARK * 1500 + "\n"
    nested = "- " * 4999 + MARK + "\n"
    for src in (long_line, nested):
        with pytest.raises(ExtractionFailed) as info:
            extract_rst(src)
        assert info.value.code == "extract_limit"
        assert MARK not in str(info.value) + info.value.detail + repr(info.value.args)


def test_a_huge_rst_line_over_10000_characters_fails_extract_limit_fast_even_amid_valid_text() -> None:
    src = "Title\n=====\n\nfine\n\n" + "word " * 4000 + "\n\nmore fine\n"
    started = time.perf_counter()
    with pytest.raises(ExtractionFailed) as info:
        extract_rst(src)
    assert info.value.reason == "extract_limit" and time.perf_counter() - started < 2.0
    # the same text wrapped into lines under the limit is fine
    wrapped = "Title\n=====\n\n" + ("word " * 100 + "\n") * 40
    assert extract_rst(wrapped).extracted_chars > 0


def test_malformed_rst_never_raises_and_never_leaks_docutils_diagnostics() -> None:
    src = (
        "Title\n==\n\n  unexpected indent\n\n.. unknowndir::\n   :opt: x\n\n`unclosed *emph\n\n"
        "+---+\n| a |\n+--+\n\n.. code-block::\n\n.. note::\n\nTail\n"
    )
    doc = extract_rst(src, source_url=URL)
    _no_controls(doc)
    text = "\n".join(s.heading + "\n" + s.text for s in doc.sections)
    for diagnostic in ("Unexpected indentation", "underline too short", "Unknown directive", "System Message", "ERROR"):
        assert diagnostic not in text
    assert "Tail" in text and "unexpected indent" in text


def test_malformed_rst_inputs_do_not_hang_or_throw() -> None:
    nasty = [
        "\x00\x01\x02\n", "``" * 5000 + "\n", "*" * 9000 + "\n", ".. " * 3000 + "\n", "|" * 9000 + "\n",
        "=" * 9000 + "\nT\n" + "=" * 9000 + "\n", ":" * 9000 + "\n", "`" + "a" * 9000 + "`_\n",
    ]
    started = time.perf_counter()
    for src in nasty:
        _no_controls(extract_rst(src))
    assert time.perf_counter() - started < 5.0


def _registry() -> tuple:
    from docutils.parsers.rst import directives, roles

    return (directives.directive, roles.role, dict(roles._roles), dict(directives._directives), roles._role_registry.copy())


def test_role_directive_substitution_and_target_definitions_never_leak_between_documents() -> None:
    before = _registry()
    extract_rst(
        ".. role:: leaked(literal)\n.. default-role:: literal\n.. |sub| replace:: LEAKEDSUB\n"
        ".. _tgt: http://evil.example/\n.. role:: rawish(raw)\n   :format: html\n\n"
        "use `x` |sub| tgt_ :leaked:`q` :rawish:`<b>r</b>`\n"
    )
    assert _registry() == before
    doc = extract_rst("use `x` |sub| tgt_ :leaked:`q` :rawish:`r`\n")
    text = doc.sections[0].text
    assert "LEAKEDSUB" not in text and "evil.example" not in text
    assert text.startswith("use x |sub| tgt_ ")  # default role back to the plain default, sub and target unknown
    assert "`q`" not in text and "q" in text


def test_registry_is_restored_after_a_failed_parse_and_after_a_hostile_one() -> None:
    before = _registry()
    for src in (".. role:: boom(literal)\n\n" + "- " * 4999 + "x\n", ".. role:: boom2(literal)\n\n" + "x" * 10_001 + "\n"):
        with pytest.raises(ExtractionFailed):
            extract_rst(src)
        assert _registry() == before
    assert extract_rst(":boom:`z`\n").sections[0].text == "z"


def test_concurrent_extractions_do_not_see_each_others_roles() -> None:
    import threading

    results: dict[int, str] = {}

    def worker(i: int) -> None:
        for _ in range(20):
            doc = extract_rst(f".. role:: r{i}(literal)\n\n:r{i}:`v{i}` and :r{(i + 1) % 6}:`w`\n")
            results[i] = doc.sections[0].text

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert all(not t.is_alive() for t in threads)
    for i in range(6):
        assert results[i] == f"`v{i}` and w"  # own role literal, a neighbour's role is plain text
    assert extract_rst(":r0:`z`\n").sections[0].text == "z"


@pytest.mark.parametrize(
    "src",
    [
        ".. raw:: html\n\n   <script>alert(1)</script>\n\nafter\n",
        ".. raw:: latex\n\n   \\input{/etc/passwd}\n\nafter\n",
        ".. raw:: html\n   :file: /etc/hosts\n\nafter\n",
        ".. role:: r(raw)\n   :format: html\n\n:r:`<script>alert(1)</script>` after\n",
        ":raw:`<script>alert(1)</script>` after\n",
        ".. default-role:: raw\n\n`<script>alert(1)</script>` after\n",
    ],
    ids=["html", "latex", "file", "defined-role", "builtin-role", "default-role"],
)
def test_raw_directive_and_raw_role_are_dropped_and_counted(src: str) -> None:
    doc = extract_rst(src)
    text = "".join(s.text for s in doc.sections)
    assert "script" not in text and "alert" not in text and "/etc" not in text and "root:" not in text
    assert text.strip() == "after" and doc.warnings == ("raw_dropped",)


# --- 4. the evaluate_condition table ----------------------------------------------------------------

NAMES = {"CONFIG_v": "AM62X", "CONFIG_t": True, "CONFIG_f": False, "CONFIG_n": 3, "CONFIG_s": "abc"}

EVALUATOR_TABLE = [
    # the conditions the TI corpus actually uses
    ("CONFIG_v in ('AM62X')", True),
    ("CONFIG_v in ('AM62X', 'AM64X', 'AM62PX', 'AM335X')", True),
    ("CONFIG_v not in ('AM62X', 'AM64X', 'AM62PX', 'AM335X', 'AM437X', 'AM65X', 'AM57X', 'AM62AX')", False),
    ("CONFIG_v not in ('AM62LX')", True),
    # tuples, lists and strings
    ("CONFIG_v in ()", False), ("CONFIG_v not in ()", True), ("CONFIG_v in ('AM62X',)", True),
    ("CONFIG_v in ['AM62X']", True), ("CONFIG_v in ('AM62X', 1)", True), ("CONFIG_v in 'AM62X'", True),
    ("CONFIG_v in (('AM62X',),)", None), ("CONFIG_v in {'AM62X'}", None), ("'62' in CONFIG_v", None),
    # comparison and literals
    ("CONFIG_v == 'AM62X'", True), ("'AM62X' == CONFIG_v", True), ("CONFIG_v != 'AM64X'", True),
    ("CONFIG_n == 3", True), ("CONFIG_n == 3.0", True), ("CONFIG_v == None", False), ("'a' 'b' == 'ab'", True),
    ("True", True), ("False", False), ("''", False), ("CONFIG_v", True), ("CONFIG_n", True),
    # not, and, or and precedence
    ("not CONFIG_t", False), ("not not CONFIG_t", True), ("not CONFIG_f and CONFIG_t", True),
    ("CONFIG_t and CONFIG_f", False), ("CONFIG_t or CONFIG_f", True),
    ("CONFIG_f or CONFIG_t and CONFIG_f", False), ("(CONFIG_f or CONFIG_t) and CONFIG_f", False),
    ("  CONFIG_v == 'AM62X'  ", True), ("CONFIG_v == 'AM62X' # comment", True),
    # unknown names give None (no guessing), also under short-circuiting operators
    ("CONFIG_missing", None), ("CONFIG_missing == 'x'", None), ("not CONFIG_missing", None),
    ("CONFIG_t or CONFIG_missing", None), ("CONFIG_f and CONFIG_missing", None),
    ("CONFIG_missing in ('a')", None), ("CONFIG_v in CONFIG_missing", None),
    # outside the grammar
    ("1 < 2", None), ("1 < 2 < 3", None), ("CONFIG_v is CONFIG_v", None), ("CONFIG_v is not None", None),
    ("CONFIG_n if CONFIG_t else 0", None), ("-CONFIG_n == -3", None), ("CONFIG_n + 1 == 4", None),
    ("f'{CONFIG_v}'", None), ("{1: 2}", None), ("('AM62X',)", None), ("*CONFIG_s", None), ("10**1000 == 1", None),
    ("", None), ("   ", None), ("not in", None), ("CONFIG_v == 'AM62X' and", None), ("\\", None),
    ("CONFIG_v ==\n'AM62X'", None),
]


@pytest.mark.parametrize(("expr", "expected"), EVALUATOR_TABLE)
def test_condition_table(expr: str, expected: bool | None) -> None:
    assert evaluate_condition(expr, NAMES.__getitem__) is expected


def test_condition_lookup_contract_is_keyerror_for_an_unknown_name() -> None:
    assert evaluate_condition("CONFIG_v == 'x'", NAMES.get) is False  # a lookup returning None is a value
    assert evaluate_condition("CONFIG_v == 'x'", lambda n: {}[n]) is None  # KeyError means unknown


INJECTIONS = [
    "__import__('os').environ.update(P1A2_PWNED='1') or True",
    "open('/tmp/p1a2_pwned_file', 'w') or True",
    "eval('1') == 1", "exec('x = 1')", "compile('1', '', 'eval')",
    "().__class__.__bases__[0].__subclasses__()", "CONFIG_v.__class__.__mro__", "getattr(CONFIG_v, 'upper')()",
    "[c for c in ().__class__.__base__.__subclasses__()]", "(lambda: __import__('os'))()",
    "CONFIG_v == 'a' or __import__('os').system('true')", "(x := __import__('os')) and True",
    "'%s' % __builtins__", "f'{__import__(\"os\").getcwd()}'", "CONFIG_v == 'AM62X'\nimport os",
    "CONFIG_v == 'AM62X'; import os", "1 if __import__('os') else 2", "await __import__('asyncio').sleep(0)",
]


@pytest.mark.parametrize("expr", INJECTIONS)
def test_injection_attempts_are_never_evaluated_as_python(expr: str, monkeypatch) -> None:
    import builtins
    import os

    def forbidden(*args: object, **kwargs: object) -> object:
        raise AssertionError("the evaluator executed code")

    monkeypatch.setattr(builtins, "eval", forbidden)
    monkeypatch.setattr(builtins, "exec", forbidden)
    monkeypatch.setattr(builtins, "open", forbidden)
    assert evaluate_condition(expr, NAMES.__getitem__) is None
    assert "P1A2_PWNED" not in os.environ
    from pathlib import Path

    assert not Path("/tmp/p1a2_pwned_file").exists()


@pytest.mark.parametrize("expr", INJECTIONS[:4])
def test_injection_conditions_in_rst_are_kept_unevaluated_and_recorded(expr: str) -> None:
    import os

    doc = extract_rst(f"T\n=\n\n.. ifconfig:: {expr}\n\n   inside\n\n.. only:: {expr}\n\n   inside2\n")
    sec = _by_path(doc)[("T",)]
    assert "inside" in sec.text and "inside2" in sec.text  # kept: unevaluable never drops text
    assert sec.condition is not None and sec.condition.startswith(expr[:20])
    assert "P1A2_PWNED" not in os.environ


# --- 5. chunker v2 ------------------------------------------------------------------------------------


def _prose(n: int, sentence: str) -> str:
    return (sentence + " ") * n


MCU_FLASH_MD = f"""\
# Flashing Tools {{#TOOLS_FLASH}}

{_prose(30, "Flashing tools put binaries into the flash of an EVM.")}

### UART Uniflash {{#UART}}

{_prose(20, "The UART uniflash tool sends files over a serial port to the bootloader.")}

#### Flashing the files

- Set EVM in \\ref BOOTMODE_UART and power on the EVM

        python uart_uniflash.py -p {{port}} --cfg={{cfg}}

#### Troubleshooting {{#TROUBLE}}

{_prose(40, "If flashing fails check the cable, the boot mode and the serial port permissions.")}
"""


def _mcu() -> Document:
    return extract_doxygen_markdown(MCU_FLASH_MD, source_url=URL, title="MCU+ SDK")


def test_mcu_plus_flashing_the_files_v1_glues_the_command_to_the_next_unrelated_section() -> None:
    chunks = chunk(_mcu(), Cfg(chunker_version=1))
    cmd = next(c for c in chunks if "uart_uniflash.py" in c.text)
    assert "serial port permissions" in cmd.text  # the TI finding: the command rides with Troubleshooting prose
    assert cmd.heading_path[-1] == "Flashing the files"


def test_mcu_plus_flashing_the_files_v2_keeps_the_command_with_its_parent_section() -> None:
    chunks = chunk(_mcu(), Cfg(chunker_version=2))
    cmd = next(c for c in chunks if "uart_uniflash.py" in c.text)
    assert cmd.heading_path == ("Flashing Tools", "UART Uniflash")
    assert "#### Flashing the files" in cmd.text and "BOOTMODE_UART" in cmd.text
    assert "UART uniflash tool sends files" in cmd.text
    others = [c for c in chunks if c is not cmd]
    assert all("uart_uniflash.py" not in c.text for c in others)
    trouble = [c for c in chunks if c.heading_path[-1] == "Troubleshooting"]
    assert trouble and all("BOOTMODE_UART" not in c.text for c in trouble)
    assert cmd.anchor == f"{URL}#UART" and trouble[0].anchor == f"{URL}#TROUBLE"


def test_v2_never_merges_a_short_section_into_the_next_unrelated_one() -> None:
    sections = (
        Section(("A",), "A", 1, _prose(30, "Alpha prose about the first topic of the guide."), "a"),
        Section(("B",), "B", 1, "short command: make all", "b"),
        Section(("C",), "C", 1, _prose(30, "Gamma prose about the third topic of the guide."), "c"),
    )
    chunks = chunk(Document("T", URL, "x", sections), Cfg())
    assert [c.heading_path for c in chunks] == [("A",), ("C",)]
    assert "make all" in chunks[0].text and "B" in chunks[0].text.split("make all")[0]  # joined to the previous sibling
    assert "make all" not in chunks[1].text


def test_v2_ti_snapshots_never_lose_a_word_to_merging() -> None:
    for src in (UART_RST, SETUP_RST, MAKEFILE_RST, BEAGLE_RST):
        doc = extract_rst(src, source_url=URL, title="d")
        text = " ".join(" ".join(c.embed_text.split()) for c in chunk(doc, Cfg(overlap_tokens=0)))  # the heading is in embed_text
        for s in (s for s in doc.sections if s.text.strip()):  # a body-less heading is dropped by design
            for word in WORDS.findall(s.heading + " " + s.text):
                assert word in text, (word, s.heading_path)


def _corpus() -> dict[str, Document]:
    docs = {n: extract_rst(globals()[n], source_url=URL, title=n) for n in ("UART_RST", "SETUP_RST", "MAKEFILE_RST", "BEAGLE_RST", "VULKAN_RST")}
    docs |= {n: extract_doxygen_markdown(globals()[n], source_url=URL, title=n) for n in ("BOOTLOADER_MD", "TASKP_MD", "FLASH_MD")}
    docs |= {n: extract_yaml(globals()[n], source_url=URL, title=n) for n in ("SPI_YAML", "TIMER_YAML", "BOARD_YAML")}
    docs["MCU_FLASH_MD"] = _mcu()
    long_code = Section(("L",), "L", 1, "intro text. " * 5 + "\n\n```c\n" + "int x = 0; // " + "y" * 90 + "\n" * 1 * 1 + "```\n" * 1, "l")
    docs["LONG"] = Document("T" * 300, URL, "x", (long_code, Section(("L", "M" * 400), "M" * 400, 2, "word " * 3000, "m")))
    return docs


@pytest.mark.parametrize("version", [1, 2])
@pytest.mark.parametrize(("max_tokens", "overlap", "min_tokens"), [(512, 64, 64), (128, 16, 32), (48, 8, 16), (24, 0, 8), (16, 15, 0)])
def test_size_caps_hold_for_every_chunk_of_every_snapshot(version: int, max_tokens: int, overlap: int, min_tokens: int) -> None:
    cfg = Cfg(max_tokens=max_tokens, overlap_tokens=overlap, min_tokens=min_tokens, chunker_version=version)
    for name, doc in _corpus().items():
        chunks = chunk(doc, cfg)
        assert [c.ordinal for c in chunks] == list(range(len(chunks))), name
        for c in chunks:
            assert len(c.embed_text) <= max_tokens * 4, (name, c.ordinal, len(c.embed_text))
            assert c.approx_tokens == max(1, (len(c.embed_text) + 3) // 4) <= max_tokens, name
            assert c.embed_text.endswith(c.text) and 0 <= c.overlap < max(1, len(c.text)) + 1
            assert c.condition is None or len(c.condition) <= 200


@pytest.mark.parametrize("version", [1, 2])
def test_nothing_in_a_snapshot_is_dropped_when_chunks_are_small(version: int) -> None:
    cfg = Cfg(max_tokens=40, overlap_tokens=0, min_tokens=0, chunker_version=version)
    for name, doc in _corpus().items():
        if name == "LONG":
            continue
        got = "".join(c.text for c in chunk(doc, cfg))
        want = "".join(s.text for s in doc.sections)
        assert re.sub(r"\s+", "", got) == re.sub(r"\s+", "", want), name  # with no overlap and no merging


def _sec(path: tuple[str, ...], text: str, **meta: object) -> Section:
    return Section(path, path[-1], len(path), text, path[-1].lower(), **meta)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("kids", "page", "page_end", "condition"),
    [
        ([dict(page=3), dict(page=4)], 3, 4, None),  # first page .. last page
        ([dict(page=5, page_end=7), dict(page=6)], 5, 7, None),  # page_end beats a later single page
        ([dict(page=2), dict(page=2)], 2, None, None),  # one page: no page_end
        ([dict(page=9), dict()], 9, None, None),  # a member without pages is ignored
        ([dict(), dict()], None, None, None),
        ([dict(page=4, page_end=4), dict(page=2, page_end=3)], 2, 4, None),  # the range spans every member
        ([dict(condition="A"), dict(condition="B")], None, None, "A; B"),
        ([dict(condition="A"), dict(condition="A")], None, None, "A"),  # distinct, in first-seen order
        ([dict(condition="B"), dict(), dict(condition="A"), dict(condition="B")], None, None, "B; A"),
        ([dict(page=1, condition="X"), dict(page=2, condition="Y")], 1, 2, "X; Y"),
    ],
)
@pytest.mark.parametrize("version", [1, 2])
def test_page_and_condition_merge_rules(kids: list[dict], page, page_end, condition, version: int) -> None:  # noqa: ANN001
    sections = [_sec(("P", f"K{i}"), f"cmd {i}", **k) for i, k in enumerate(kids)]
    doc = Document("T", URL, "x", (_sec(("P",), "Parent intro text. " * 3), *sections))
    chunks = chunk(doc, Cfg(chunker_version=version, min_tokens=64))
    assert len(chunks) == 1  # everything short enough to merge into one group (v1: one run, v2: the parent)
    c = chunks[0]
    assert (c.page, c.page_end, c.condition) == (page, page_end, condition)
    plain = chunk(Document("T", URL, "x", tuple(replace_meta(s) for s in doc.sections)), Cfg(chunker_version=version))
    assert [x.embed_text_hash for x in chunks] == [x.embed_text_hash for x in plain]  # metadata never moves a hash


def replace_meta(section: Section) -> Section:
    return replace(section, page=None, page_end=None, condition=None)


@pytest.mark.parametrize("version", [1, 2])
def test_merged_conditions_are_clipped_to_200_characters(version: int) -> None:
    kids = [_sec(("P", f"K{i}"), "x", condition=f"CONFIG_variant == '{i:03d}-" + "v" * 40 + "'") for i in range(8)]
    doc = Document("T", URL, "x", (_sec(("P",), "intro " * 10), *kids))
    (c,) = chunk(doc, Cfg(chunker_version=version))
    assert c.condition is not None and len(c.condition) == 200 and c.condition.startswith("CONFIG_variant == '000-")


@pytest.mark.parametrize("version", [1, 2])
def test_every_split_chunk_of_a_section_carries_its_page_and_condition(version: int) -> None:
    sec = _sec(("Big",), "sentence about flashing. " * 400, page=7, page_end=9, condition="SOC_AM64X")
    chunks = chunk(Document("T", URL, "x", (sec,)), Cfg(max_tokens=64, overlap_tokens=8, chunker_version=version))
    assert len(chunks) > 5
    assert {(c.page, c.page_end, c.condition) for c in chunks} == {(7, 9, "SOC_AM64X")}


def test_chunks_of_a_conditioned_doxygen_page_expose_the_condition_but_not_in_the_hash_input() -> None:
    doc = extract_doxygen_markdown(FLASH_MD, source_url=URL, title="d")
    chunks = chunk(doc, Cfg(min_tokens=0))
    assert [c.condition for c in chunks if c.condition] == ["SOC_AM263X || SOC_AM243X", "SOC_AM243X || SOC_AM64X || SOC_AM65X"]
    assert all("condition" not in c.embed_text.lower().split("\n\n")[0] for c in chunks)  # prefix is path/title only


# --- determinism and the v1 goldens -------------------------------------------------------------------


def _digest(version: int) -> dict[str, str]:
    out = {}
    for name, doc in _corpus().items():
        hashes = [c.embed_text_hash for c in chunk(doc, Cfg(chunker_version=version, min_tokens=32))]
        out[name] = hashlib.sha256("|".join(hashes).encode()).hexdigest()
    return out


@pytest.mark.parametrize("version", [1, 2])
def test_rerunning_extraction_and_chunking_gives_identical_hashes(version: int) -> None:
    first = _digest(version)
    assert _digest(version) == first and _digest(version) == first
    a = [c.embed_text_hash for c in chunk(_mcu(), Cfg(chunker_version=version))]
    assert a == [c.embed_text_hash for c in chunk(_mcu(), Cfg(chunker_version=version))]
    assert len(set(a)) == len(a)  # distinct chunks, distinct hashes


def test_hashes_do_not_depend_on_the_python_hash_seed() -> None:
    code = (
        "import sys, json; sys.path.insert(0, sys.argv[1]);"
        "from test_p1a2_formats_acceptance import _digest; print(json.dumps([_digest(1), _digest(2)]))"
    )
    outs = set()
    for seed in ("0", "7", "4242"):
        run = subprocess.run(
            [sys.executable, "-c", code, str(Path(__file__).parent)],
            capture_output=True, text=True, check=True, env={"PYTHONHASHSEED": seed, "PATH": "/usr/bin:/bin"}, timeout=120,
        )
        outs.add(run.stdout.strip())
    assert len(outs) == 1
    assert json.loads(outs.pop()) == [_digest(1), _digest(2)]


# Frozen from the committed (HEAD) chunker before P1.A2 and checked equal to the working tree: a change here
# means every existing knowledge base would re-embed.
V1_GOLDEN = {
    "UART_RST": "e8aee068694a44657ce3df22f75583eaa3f87bfda3e7520340b2f83e2f2be5b4",
    "SETUP_RST": "bede5961808df72d51abc87e059feccc03dacd5bd3e725e69029fe570e03a114",
    "MAKEFILE_RST": "9e1998c7083a8d18449b402b88b145f0c14cb1e061d527e58ce4a7417d384164",
    "BEAGLE_RST": "b184d92cd635571667ebf328d0d48d86f1e2be1eb8f47eb162e4b2d7ca316540",
    "VULKAN_RST": "14f12c31dc92010b66bd1c6d3bf15e940524ee03614f6394ea84a4206d0b4de3",
    "BOOTLOADER_MD": "fc122443a98625309d17b613fcab8642c4150c9b4344d7c2a1b761ddff13215f",
    "TASKP_MD": "479ddde0ed46b0797c83174a519d43c8050d6633c939b013566cb59dcdc10111",
    "FLASH_MD": "d991966cbf77157693b9a58381cb9ac6af254ecca9bab49d47803bd2b8c1fd5a",
    "SPI_YAML": "afc9bc0eb2833fdceee8b1256b587352f5235ba31f746f73aad57a3aa84a06e9",
    "TIMER_YAML": "994cea15ba72fde73a32d987ce090e9dbb3955bccb03f4fd07eb02fda04f1d73",
    "BOARD_YAML": "d27e26fa26abe6f1de47386b3e9adbcb34519dd15f417f4d9494772b397508f4",
    "MCU_FLASH_MD": "d16a33db0e5ca9bd8d9c497c5082660176b36d492e7871584d2e4d9eb7275054",
    "LONG": "5b181493f91abea33d4e7a4f8a664dd42cae81861938e4a9b8535fd441def6ec",
}

# Pins the v2 output of the same corpus: a change means the v2 algorithm changed (needs a new version).
V2_GOLDEN = {
    "UART_RST": "e8aee068694a44657ce3df22f75583eaa3f87bfda3e7520340b2f83e2f2be5b4",
    "SETUP_RST": "bede5961808df72d51abc87e059feccc03dacd5bd3e725e69029fe570e03a114",
    "MAKEFILE_RST": "9e1998c7083a8d18449b402b88b145f0c14cb1e061d527e58ce4a7417d384164",
    "BEAGLE_RST": "b184d92cd635571667ebf328d0d48d86f1e2be1eb8f47eb162e4b2d7ca316540",
    "VULKAN_RST": "14f12c31dc92010b66bd1c6d3bf15e940524ee03614f6394ea84a4206d0b4de3",
    "BOOTLOADER_MD": "ed67869246243bc7ab25d855c3af3c8819bc7fe5cc4cae94ce2ba9fb4e830ac4",
    "TASKP_MD": "9dc26edbafb746bb675b6d75d9aa8b0ec156560290036f7b6389e35d28f86616",
    "FLASH_MD": "6ebf3b9a07121a5252062c6193ee7560eb2454cb6164ebee84986c3cb7bd4413",
    "SPI_YAML": "afc9bc0eb2833fdceee8b1256b587352f5235ba31f746f73aad57a3aa84a06e9",
    "TIMER_YAML": "994cea15ba72fde73a32d987ce090e9dbb3955bccb03f4fd07eb02fda04f1d73",
    "BOARD_YAML": "d27e26fa26abe6f1de47386b3e9adbcb34519dd15f417f4d9494772b397508f4",
    "MCU_FLASH_MD": "98f41870d79d53d460d9a35408a0b1975705cc3a0157c8bf6ffeaeb80236d611",
    "LONG": "5b181493f91abea33d4e7a4f8a664dd42cae81861938e4a9b8535fd441def6ec",
}


def test_v1_output_is_byte_identical_to_the_pre_p1a2_chunker() -> None:
    assert _digest(1) == V1_GOLDEN


def test_v2_golden_hashes_for_the_ti_corpus() -> None:
    assert _digest(2) == V2_GOLDEN


# --- 6. the import-linter contract ---------------------------------------------------------------------

ROOT = Path(__file__).resolve().parents[3]
CONTRACT_NAME = "The API and the run store never import a document parser"


def _contract() -> dict:
    import tomllib

    contracts = tomllib.loads((ROOT / "pyproject.toml").read_text())["tool"]["importlinter"]["contracts"]
    (found,) = [c for c in contracts if c["name"] == CONTRACT_NAME]
    return found


def test_the_contract_names_the_api_and_the_run_store_and_every_parser() -> None:
    contract = _contract()
    assert contract["type"] == "forbidden"
    assert sorted(contract["source_modules"]) == ["eap_api", "eap_run_store"]
    assert {"docutils", "pypdf", "pypdfium2", "PIL"} <= set(contract["forbidden_modules"])
    assert {"eap_knowledge.formats.rst", "eap_knowledge.formats.structured", "eap_knowledge.formats.doxygen"} <= set(
        contract["forbidden_modules"]
    )
    assert not contract.get("allow_indirect_imports", False)  # a chain through eap_knowledge counts


def test_the_parser_free_modules_the_api_needs_are_not_forbidden() -> None:
    forbidden = set(_contract()["forbidden_modules"])
    assert not forbidden & {"eap_knowledge", "eap_knowledge.formats", "eap_knowledge.extract", "eap_knowledge.chunking", "markdown_it"}


def test_no_api_or_run_store_source_imports_a_parser_directly() -> None:
    import ast

    banned = ("docutils", "pypdf", "pypdfium2", "PIL", "eap_knowledge.formats.rst", "eap_knowledge.formats.structured",
              "eap_knowledge.formats.doxygen")
    files = [*ROOT.glob("apps/api/src/**/*.py"), *ROOT.glob("packages/run-store/src/**/*.py")]
    assert files
    for path in files:
        for node in ast.walk(ast.parse(path.read_text())):
            names = [a.name for a in node.names] if isinstance(node, ast.Import) else (
                [node.module or ""] + [f"{node.module}.{a.name}" for a in node.names] if isinstance(node, ast.ImportFrom) else []
            )
            for name in names:
                assert not any(name == b or name.startswith(b + ".") for b in banned), (path, name)


def _lint(tmp_path: Path, api: str, run_store: str = "import hashlib\n", worker: str = "import hashlib\n") -> subprocess.CompletedProcess:
    contract = _contract()
    lines = ["[tool.importlinter]", 'root_packages = ["eap_api", "eap_run_store", "eap_worker", "eap_knowledge"]',
             "include_external_packages = true", "", "[[tool.importlinter.contracts]]"]
    lines += [f"{key} = {json.dumps(value)}" for key, value in contract.items()]
    (tmp_path / "pyproject.toml").write_text("\n".join(lines) + "\n")
    for package, source in (("eap_api", api), ("eap_run_store", run_store), ("eap_worker", worker)):
        (tmp_path / package).mkdir(exist_ok=True)
        (tmp_path / package / "__init__.py").write_text(source)
    knowledge = ROOT / "packages" / "knowledge" / "src"
    schemas = ROOT / "packages" / "shared-schemas" / "src"
    env = {"PATH": "/usr/bin:/bin", "PYTHONPATH": f"{tmp_path}:{knowledge}:{schemas}"}
    return subprocess.run(
        [str(Path(sys.executable).parent / "lint-imports")], cwd=tmp_path, capture_output=True, text=True, env=env, timeout=120
    )


@pytest.mark.parametrize(
    "api",
    [
        "from eap_knowledge.formats import rst\n",
        "from eap_knowledge.formats import structured\n",
        "from eap_knowledge.formats import doxygen\n",
        "from eap_knowledge.formats.rst import extract_rst\n",
        "import docutils\n",
        "from docutils.parsers.rst import Parser\n",
    ],
    ids=["rst", "structured", "doxygen", "extract_rst", "docutils", "docutils-submodule"],
)
def test_the_api_cannot_import_a_parser_even_through_eap_knowledge(tmp_path: Path, api: str) -> None:
    run = _lint(tmp_path, api)
    assert run.returncode != 0 and "BROKEN" in run.stdout and CONTRACT_NAME in run.stdout
    assert "eap_api" in run.stdout


@pytest.mark.parametrize("run_store", ["import docutils\n", "from eap_knowledge.formats import rst\n", "from docutils import nodes\n"])
def test_the_run_store_cannot_import_a_parser(tmp_path: Path, run_store: str) -> None:
    run = _lint(tmp_path, "import hashlib\n", run_store=run_store)
    assert run.returncode != 0 and "BROKEN" in run.stdout and "eap_run_store" in run.stdout


@pytest.mark.parametrize(
    "api",
    [
        "from eap_knowledge import extract, chunk, ChunkingConfig\n",
        "from eap_knowledge.formats import format_for_mime, site_for, detect_binary_type, has_pdf_signature\n",
        "import markdown_it\n",
    ],
    ids=["extract-chunk", "registry", "markdown-it"],
)
def test_the_api_may_use_the_parser_free_parts_of_eap_knowledge(tmp_path: Path, api: str) -> None:
    run = _lint(tmp_path, api, run_store=api)
    assert run.returncode == 0, run.stdout + run.stderr
    assert "KEPT" in run.stdout and "BROKEN" not in run.stdout


def test_the_worker_may_import_the_parsers(tmp_path: Path) -> None:
    run = _lint(tmp_path, "import hashlib\n", worker="from eap_knowledge.formats.rst import extract_rst\nimport docutils, yaml\n")
    assert run.returncode == 0, run.stdout + run.stderr


@pytest.mark.parametrize("version", [0, 3, -1, 99])
def test_only_chunker_versions_1_and_2_are_supported(version: int) -> None:
    from eap_knowledge import InvalidChunkingConfig

    doc = Document("T", URL, "x", (_sec(("A",), "text"),))
    with pytest.raises(InvalidChunkingConfig) as info:
        chunk(doc, Cfg(chunker_version=version))
    assert info.value.field == "chunker_version" and "1, 2" in str(info.value)
    assert chunk(doc, Cfg(chunker_version=1)) and chunk(doc, Cfg(chunker_version=2))
