from .loki_gate import SKIP_REASON, should_skip_live

URL = "http://127.0.0.1:3100"


def test_reachable_loki_runs_the_live_tests():
    assert should_skip_live(URL, {}, probe=lambda _u: True) is False


def test_unreachable_loki_skips_the_live_tests():
    assert should_skip_live(URL, {}, probe=lambda _u: False) is True


def test_unreachable_loki_with_require_flag_runs_and_fails_as_before():
    assert should_skip_live(URL, {"EAP_REQUIRE_LOKI": "1"}, probe=lambda _u: False) is False


def test_require_flag_does_not_probe():
    def boom(_u):
        raise AssertionError("must not probe")

    assert should_skip_live(URL, {"EAP_REQUIRE_LOKI": "1"}, probe=boom) is False


def test_skip_reason_text():
    assert SKIP_REASON == "Loki is not reachable at 127.0.0.1:3100 (set EAP_REQUIRE_LOKI=1 to fail instead)"
