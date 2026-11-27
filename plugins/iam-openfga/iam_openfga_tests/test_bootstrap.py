"""The bootstrap against the fake: it creates the store and model once, a second run writes nothing, a changed model is
written, a dry run writes nothing, and the key never reaches the output."""

import asyncio
import io

from eap_iam_openfga.bootstrap import amain
from eap_iam_openfga.model import model_text

from .fakes import KEY, URL, FakeOpenFga


def run_cli(fake, *extra, env=None):
    out = io.StringIO()
    code = asyncio.run(
        amain(["--url", URL, *extra], env={"EAP_OPENFGA_PRESHARED_KEY": KEY, **(env or {})}, out=out, http=__import__("httpx2").AsyncClient(transport=fake.transport()))
    )
    return code, out.getvalue()


def test_first_run_creates_the_store_and_writes_the_model():
    fake = FakeOpenFga()
    code, text = run_cli(fake)
    assert code == 0
    assert fake.calls["create_store"] == 1 and fake.calls["write_model"] == 1
    assert "EAP_OPENFGA_STORE_ID=01STORE0" in text and "EAP_OPENFGA_MODEL_ID=01MODEL0" in text
    assert KEY not in text and fake.auth_ok


def test_a_second_run_writes_nothing_and_prints_the_same_ids():
    fake = FakeOpenFga()
    _, first = run_cli(fake)
    fake.calls.clear()
    code, second = run_cli(fake)
    assert code == 0 and first.splitlines()[-2:] == second.splitlines()[-2:]
    assert fake.calls["create_store"] == 0 and fake.calls["write_model"] == 0
    assert "(unchanged)" in second


def test_a_changed_model_is_written_as_a_new_model_in_the_same_store():
    fake = FakeOpenFga()
    run_cli(fake)
    fake.calls.clear()
    changed = model_text().replace("define can_read: viewer\n    define can_write", "define can_read: viewer or admin\n    define can_write", 1)
    path = __import__("pathlib").Path(__import__("tempfile").mkdtemp()) / "m.fga"
    path.write_text(changed)
    code, text = run_cli(fake, "--model-file", str(path))
    assert code == 0 and fake.calls["create_store"] == 0 and fake.calls["write_model"] == 1
    assert "(written)" in text and "EAP_OPENFGA_MODEL_ID=01MODEL1" in text


def test_dry_run_writes_nothing_at_all():
    fake = FakeOpenFga()
    code, text = run_cli(fake, "--dry-run")
    assert code == 0 and fake.calls["create_store"] == 0 and fake.calls["write_model"] == 0 and not fake.stores
    assert "would be" in text
    run_cli(fake)
    fake.calls.clear()
    code, text = run_cli(fake, "--dry-run")
    assert fake.calls["write_model"] == 0 and "(unchanged)" in text


def test_a_named_store_that_exists_twice_is_not_guessed():
    fake = FakeOpenFga()
    fake.stores = [{"id": "a", "name": "durable"}, {"id": "b", "name": "durable"}]
    code, _ = run_cli(fake)
    assert code == 1 and fake.calls["write_model"] == 0


def test_an_explicit_store_is_used_as_given():
    fake = FakeOpenFga()
    code, text = run_cli(fake, "--store-id", "01GIVEN")
    assert code == 0 and fake.calls["create_store"] == 0 and "EAP_OPENFGA_STORE_ID=01GIVEN" in text


def test_a_server_error_exits_nonzero_and_leaks_nothing(capsys):
    import httpx2

    fake = FakeOpenFga()
    fake.fail = lambda name: httpx2.Response(500, json={"code": "internal_error", "message": KEY})
    code, text = run_cli(fake)
    err = capsys.readouterr().err
    assert code == 1 and KEY not in err and KEY not in text and "HTTP 500" in err


def test_no_url_is_a_usage_error(capsys):
    out = io.StringIO()
    assert asyncio.run(amain([], env={}, out=out)) == 2
    assert "EAP_OPENFGA_URL" in capsys.readouterr().err
