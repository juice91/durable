"""`python -m eap_worker`: launch DBOS (recovering this executor's pending runs) and serve the queue."""

from __future__ import annotations

import logging
import signal
import sys
import threading

from dbos import DBOS
from eap_observability import shutdown_telemetry

from eap_worker.app import launch
# Registers eap.cancel_reconcile and its steps before DBOS.launch() (loops D12 step 4, LP.8).
from eap_worker import cancel_reconcile


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    stop = threading.Event()

    def _stop(signum: int, _frame: object) -> None:
        logging.getLogger("eap_worker").info("signal %s: shutting down", signum)
        stop.set()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    # An invalid EAP_CANCEL_RECONCILE_* setting fails here, before DBOS launches or recovers anything.
    reconcile_config = cancel_reconcile.ReconcileConfig.from_env()
    settings = launch()
    # Every worker applies the same schedule (an upsert by name); one workflow runs per tick.
    cancel_reconcile.apply_cancel_reconcile_schedule(reconcile_config)
    logging.getLogger("eap_worker").info(
        "eap-worker ready: executor_id=%s app_version=%s schema=%s",
        settings.executor_id,
        settings.app_version,
        settings.dbos_system_schema,
    )
    print("EAP_WORKER_READY", flush=True)
    stop.wait()
    DBOS.destroy(destroy_registry=False)
    shutdown_telemetry()  # flush the last spans and log records
    return 0


if __name__ == "__main__":
    sys.exit(main())
