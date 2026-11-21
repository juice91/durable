"""Architecture rule: only `app.py` (the composition root) and `deps.py` (the providers) touch `app.state`.

Every other module gets what it needs from a `…Dep` parameter or a `get_<name>(request)` provider in
`eap_api.deps`. The check parses each source file, so a mention in a docstring or a comment is fine.
"""

from __future__ import annotations

import ast
from pathlib import Path

import eap_api

SRC = Path(eap_api.__file__).parent
ALLOWED = {"app.py", "deps.py"}


def app_state_uses(source: str) -> list[int]:
    """Line numbers of every `app.state` / `<expr>.app.state` attribute access in `source`."""
    lines = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Attribute) and node.attr == "state":
            owner = node.value
            if (isinstance(owner, ast.Name) and owner.id == "app") or (
                isinstance(owner, ast.Attribute) and owner.attr == "app"
            ):
                lines.append(node.lineno)
    return lines


def test_only_app_and_deps_touch_app_state():
    offenders = []
    for path in sorted(SRC.rglob("*.py")):
        relative = path.relative_to(SRC)
        if relative.parts in {("app.py",), ("deps.py",)}:
            continue
        for line in app_state_uses(path.read_text()):
            offenders.append(f"{relative}:{line}")
    assert not offenders, "app.state is read outside app.py and deps.py (use a Dep from eap_api.deps): " + ", ".join(offenders)


def test_detector_catches_each_form_of_a_violation():
    assert app_state_uses("def f(request):\n    return request.app.state.run_stores\n") == [2]
    assert app_state_uses("def f(app):\n    return app.state.database_engine\n") == [2]
    assert app_state_uses("def f(request):\n    return getattr(request.app.state, 'x', None)\n") == [2]
    assert app_state_uses("def f(self):\n    return self.app.state.x\n") == [2]


def test_detector_ignores_docstrings_and_unrelated_state():
    assert app_state_uses('"""Reads app.state.x."""\n# request.app.state.y\nx = job.state\ny = self.state.app\n') == []
