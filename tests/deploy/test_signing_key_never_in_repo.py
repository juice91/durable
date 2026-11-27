"""No value of EAP_LOCAL_AUTH_SIGNING_KEY is ever in the repository: no default, no example, no test literal. Every
assignment in a tracked file is empty or a reference (a shell expansion, a format placeholder, or, in Python, a name)."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
NAME = "EAP_LOCAL_AUTH_SIGNING_KEY"
# NAME, then `=` or `:` (with the quotes, escapes and subscript brackets a language puts around the name), then the value.
ASSIGNMENT = re.compile(NAME + r"""\\?["'`]?\]?\s*(?:=(?!=)|:)\s*(?P<value>.*)""")
# `-}` is the empty default of `${NAME:-}`; any other default in an expansion is a value.
EMPTY = re.compile(r"""^(?:$|(?:\\?["'])(?:\\?["'])|[,)}\]|`]|-\})""")
REFERENCE = re.compile(r"""^\\?["'`]?(?:\$|%s|\{|<)""")
PYTHON_NAME = re.compile(r"^(?:[A-Za-z_][\w.]*(?:[(\[]|\s*(?:[,)}\]#]|$)))")


def literal_value(line: str, python: bool) -> str | None:
    for match in ASSIGNMENT.finditer(line):
        value = match["value"].strip()
        if EMPTY.match(value) or REFERENCE.match(value) or (python and PYTHON_NAME.match(value)):
            continue
        return value
    return None


def test_the_check_sees_a_literal_and_passes_references():
    assigned = NAME  # built from the name, so this file holds no assignment itself
    for bad in (f"{assigned}=abc123", f'{assigned}: "abc123"', f'"{assigned}": "k" * 32', f"export {assigned}='abc'",
                f'env["{assigned}"] = "abc"', f"- {assigned}=abcdef",
                f'"${{{assigned}:-abc123}}"'):
        assert literal_value(bad, python=bad.startswith(('"', "env"))) is not None, bad
    for good in (f"{assigned}=", f'  {assigned}: ""', f"# {assigned}=", f'{assigned}="$(openssl rand -base64 48)"',
                 f'{assigned}=${{{assigned}:-}}', f"{assigned}=%s\\n", f'{assigned}: ${{{{ secrets.KEY }}}}',
                 f'if [[ -z "${{{assigned}:-}}" ]]; then'):
        assert literal_value(good, python=False) is None, good
    for good in (f'"{assigned}": KEY,', f'"{assigned}": secrets.token_hex(32)', f'{assigned}={{os.urandom(32).hex()}}'):
        assert literal_value(good, python=True) is None, good


def test_no_tracked_file_assigns_the_signing_key_a_value():
    names = subprocess.run(
        ["git", "-C", str(ROOT), "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        check=True, capture_output=True, text=True,
    ).stdout.split("\0")
    found = []
    for name in filter(None, names):
        try:
            text = (ROOT / name).read_text()
        except (UnicodeDecodeError, OSError):
            continue
        if NAME not in text:
            continue
        for number, line in enumerate(text.splitlines(), 1):
            if literal_value(line.replace("#|", ""), python=name.endswith(".py")) is not None:
                found.append(f"{name}:{number}")  # the place only: never the value
    assert found == [], "a signing key value must never be in the repository"
