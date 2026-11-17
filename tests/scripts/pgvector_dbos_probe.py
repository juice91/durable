"""Helper for pgvector-upgrade-test.sh: leave a PENDING DBOS workflow behind, then recover it.

usage: pgvector_dbos_probe.py start|recover <system database url> <workflow id>
"""

import os
import sys
import time

from dbos import DBOS, DBOSConfig, SetWorkflowID

os.environ.setdefault("EAP_MCP_CATALOG_SCHEDULE", "off")

mode, url, wf_id = sys.argv[1:4]
config: DBOSConfig = {
    "name": "pgvector-upgrade-probe",
    "system_database_url": url,
    "application_version": "pgv-probe",
    "executor_id": "pgv-probe-executor",
    "run_admin_server": False,
}


@DBOS.workflow()
def probe() -> str:
    DBOS.sleep(5)
    return "recovered"


DBOS(config=config)
DBOS.launch()
if mode == "start":
    with SetWorkflowID(wf_id):
        DBOS.start_workflow(probe)
    time.sleep(1.5)
    os._exit(0)  # a crash: no shutdown, the workflow stays PENDING in dbos.workflow_status
handle = DBOS.retrieve_workflow(wf_id)
deadline = time.monotonic() + 45  # get_result() blocks forever if recovery never runs
while handle.get_status().status in ("PENDING", "ENQUEUED"):
    if time.monotonic() > deadline:
        print("result=timeout", flush=True)
        print("status=" + handle.get_status().status, flush=True)
        os._exit(1)
    time.sleep(0.5)
print("result=" + str(handle.get_result()))
print("status=" + handle.get_status().status, flush=True)
os._exit(0)
