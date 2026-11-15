"""AgentSpec.tools with non-string input is a validation error (an API 422), never a TypeError (500)."""

import pytest
from pydantic import ValidationError

from eap_schemas import AgentSpec

BASE = {"name": "a", "model": {"provider": "test", "name": "echo"}}


@pytest.mark.parametrize("tools", [[1], ["ok", 2], [None], [["x"]], [{"id": "x"}], "calculator", {"calculator": 1}, 5])
def test_non_string_tools_are_a_validation_error(tools):
    with pytest.raises(ValidationError) as exc:
        AgentSpec.model_validate({**BASE, "tools": tools})
    assert "tool" in str(exc.value)


def test_valid_tools_still_validate():
    assert AgentSpec.model_validate({**BASE, "tools": ["calculator", "http.get"]}).tools == ("calculator", "http.get")
    assert AgentSpec.model_validate({**BASE, "tools": []}).tools == ()
