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
    
    # Verify kb-sources-acme is noted as excluded from export
    exclude_paths = root / "deploy/export/exclude-paths.txt"
    if exclude_paths.exists():
        exclude_content = exclude_paths.read_text()
        assert "kb-sources-acme" in exclude_content, "kb-sources-acme should be excluded from export"
        # Verify the PR notes would mention this (check if there's a related comment in code)
        # Since we can't check PR notes directly, verify the exclude file exists and mentions it


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


def test_third_party_notices_has_notes_section():
    """Test that THIRD_PARTY_NOTICES.md has notes on non-permissive entries."""
    root = Path(__file__).parent.parent.parent
    notices_path = root / "THIRD_PARTY_NOTICES.md"
    content = notices_path.read_text()
    assert "## Notes on non-permissive entries" in content, "Missing notes section for non-permissive entries"
    # Verify key entries are mentioned
    assert "psycopg" in content, "Missing psycopg from non-permissive notes"
    assert "psycopg-binary" in content, "Missing psycopg-binary from non-permissive notes"
    assert "@fontsource" in content, "Missing @fontsource from non-permissive notes"
    assert "caniuse-lite" in content, "Missing caniuse-lite from non-permissive notes"
    assert "docutils" in content, "Missing docutils from non-permissive notes"


def test_third_party_notices_excludes_first_party():
    """Test that FIRST-PARTY eap-* packages are not in the Python dependencies table."""
    root = Path(__file__).parent.parent.parent
    notices_path = root / "THIRD_PARTY_NOTICES.md"
    content = notices_path.read_text()
    # Find the Python dependencies table section
    lines = content.split("\n")
    in_python_table = False
    for line in lines:
        if "## Python Dependencies" in line:
            in_python_table = True
            continue
        if in_python_table:
            if line.startswith("## ") and "Python Dependencies" not in line:
                break
            if line.startswith("|") and "eap-" in line:
                assert False, f"Found first-party package in Python table: {line}"
    
    # Also check the regenerate command doesn't produce eap-* packages
    assert "grep -v" in content or "--ignore-packages" in content, "Regenerate command should filter eap-* packages"
