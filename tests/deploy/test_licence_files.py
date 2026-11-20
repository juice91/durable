"""Test license compliance files and fields."""

import re
from pathlib import Path


def test_license_file_header():
    """Test that LICENSE file starts with the Apache header line."""
    root = Path(__file__).parent.parent.parent
    license_path = root / "LICENSE"
    content = license_path.read_text()
    assert content.startswith("Apache License\nVersion 2.0, January 2004\nhttp://www.apache.org/licenses/")


def test_notice_file_exists_and_names_durable():
    """Test that NOTICE file exists and names Durable."""
    root = Path(__file__).parent.parent.parent
    notice_path = root / "NOTICE"
    content = notice_path.read_text()
    assert "Durable" in content
    assert "Copyright 2026" in content


def test_all_pyproject_toml_declare_apache_license():
    """Test that all pyproject.toml files declare Apache-2.0 license."""
    root = Path(__file__).parent.parent

    # Find all pyproject.toml files
    pyproject_files = []
    for pattern in ["apps/*/pyproject.toml", "packages/*/pyproject.toml"]:
        pyproject_files.extend(root.glob(pattern))
    
    # Root pyproject.toml
    pyproject_files.append(root / "pyproject.toml")
    
    # Check spikes too
    pyproject_files.extend(root.glob("spikes/*/pyproject.toml"))

    apache_license_pattern = re.compile(r'^license = "(Apache-2.0)"', re.MULTILINE)
    apache_text_pattern = re.compile(r'^license = \{text = "Apache-2.0"\}', re.MULTILINE)

    for pyproject_path in pyproject_files:
        if not pyproject_path.exists():
            continue
        content = pyproject_path.read_text()
        has_license = apache_license_pattern.search(content) or apache_text_pattern.search(content)
        assert has_license, f"{pyproject_path} does not declare license = \"Apache-2.0\""


def test_all_package_json_declare_apache_license():
    """Test that all package.json files under apps/* declare Apache-2.0 license."""
    root = Path(__file__).parent.parent

    # Find all package.json files in apps and other publishable areas
    package_json_files = []
    for pattern in ["apps/*/package.json", "package.json"]:
        package_json_files.extend(root.glob(pattern))

    # Also check examples and tests that have package.json
    package_json_files.extend(root.glob("examples/*/package.json"))
    package_json_files.extend(root.glob("tests/*/package.json"))

    apache_license_pattern = re.compile(r'"license": "Apache-2.0"')

    for package_json_path in package_json_files:
        if not package_json_path.exists():
            continue
        content = package_json_path.read_text()
        has_license = apache_license_pattern.search(content)
        assert has_license, f"{package_json_path} does not declare \"license\": \"Apache-2.0\""
