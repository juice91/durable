"""audit_log retention (eap.audit_retention): config, the sweep loop and the schedule decisions. No database."""

from datetime import datetime, timezone

import pytest

from eap_worker.audit_retention import (
    DEFAULT_SCHEDULE,
    AuditRetentionConfig,
    AuditRetentionConfigError,
    sweep,
)

TICK = datetime(2026, 11, 23, 4, 15, tzinfo=timezone.utc)


def test_config_defaults_to_400_days_and_off_keeps_everything():
    cfg = AuditRetentionConfig.from_env({})
    assert (cfg.days, cfg.schedule, cfg.off) == (400, DEFAULT_SCHEDULE, False)
    assert AuditRetentionConfig.from_env({"EAP_AUDIT_RETENTION_DAYS": "30"}).days == 30
    assert AuditRetentionConfig.from_env({"EAP_AUDIT_RETENTION_DAYS": "off"}).off
    assert AuditRetentionConfig.from_env({"EAP_AUDIT_RETENTION_SCHEDULE": "off"}).off


@pytest.mark.parametrize("bad", ["0", "-5", "x", "36501", "1.5"])
def test_an_invalid_setting_refuses_at_start(bad):
    with pytest.raises(AuditRetentionConfigError):
        AuditRetentionConfig.from_env({"EAP_AUDIT_RETENTION_DAYS": bad})


def test_the_sweep_stops_at_a_short_batch_and_at_the_bound():
    sizes = iter([5, 5, 3])
    result = sweep(400, TICK, lambda cutoff, limit: next(sizes), batch_size=5)
    assert result["complete"] and result["deleted"] == 13 and result["batches"] == 3
    assert result["cutoff"].startswith("2025-10-19")  # 400 days before the tick
    bounded = sweep(400, TICK, lambda cutoff, limit: limit, batch_size=5, max_batches=4)
    assert not bounded["complete"] and bounded["deleted"] == 20 and bounded["batches"] == 4


def test_the_context_carries_the_days():
    cfg = AuditRetentionConfig.from_env({"EAP_AUDIT_RETENTION_DAYS": "90"})
    assert AuditRetentionConfig.days_from_context(cfg.to_context()) == 90
    assert AuditRetentionConfig.days_from_context(None) is None
