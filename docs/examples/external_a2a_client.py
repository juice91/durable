"""Run an exported workflow over A2A with the official A2A Python SDK (docs/external-access.md, "The A2A server").

    pip install "a2a-sdk==1.3.1"
    export DURABLE_A2A_URL="https://<public-host>/api/v1/external/a2a/<endpoint-id>"   # Studio > External access
    export DURABLE_RUN_KEY="..."                                                        # a run key of your client
    python external_a2a_client.py                      # lists the skills this key may run
    python external_a2a_client.py <skill> '{"q": "x"}'  # runs one and prints its result

Inside an agent (a LangGraph node, for example) call `run_workflow` and keep `message_id` in the node's state: sending
the same id again answers the same task, so a retried node never starts a second run.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import uuid
from typing import Any

import httpx
from a2a.client import A2ACardResolver, Client, ClientConfig, ClientFactory
from a2a.types import (
    GetExtendedAgentCardRequest,
    GetTaskRequest,
    Message,
    Part,
    Role,
    SendMessageConfiguration,
    SendMessageRequest,
    Task,
    TaskState,
)
from google.protobuf import json_format

# A task in one of these will not move by waiting. INPUT_REQUIRED: a person in the platform has to decide.
SETTLED = {
    TaskState.TASK_STATE_COMPLETED, TaskState.TASK_STATE_FAILED, TaskState.TASK_STATE_CANCELED,
    TaskState.TASK_STATE_REJECTED, TaskState.TASK_STATE_INPUT_REQUIRED,
}


async def connect(http: httpx.AsyncClient, base_url: str) -> Client:
    """A client for the endpoint: the public Agent Card, then JSON-RPC. `http` carries the bearer key."""
    card = await A2ACardResolver(http, base_url).get_agent_card()
    return ClientFactory(ClientConfig(httpx_client=http, streaming=False)).create(card)


async def skills(client: Client) -> list[Any]:
    """The workflows this key may run (the authenticated extended card)."""
    return list((await client.get_extended_agent_card(GetExtendedAgentCardRequest())).skills)


async def run_workflow(
    client: Client, skill: str, workflow_input: dict[str, Any], *, message_id: str | None = None,
    poll_seconds: float = 2.0,
) -> Task:
    """Start the workflow `skill` and follow its task until it ends or waits for a person."""
    part = json_format.ParseDict({"data": {"skill": skill, "input": workflow_input}}, Part())
    request = SendMessageRequest(
        message=Message(message_id=message_id or uuid.uuid4().hex, role=Role.ROLE_USER, parts=[part]),
        # Do not hold the connection for a long workflow: a blocking send answers a working task after 60 s.
        configuration=SendMessageConfiguration(return_immediately=True),
    )
    task: Task | None = None
    async for reply in client.send_message(request):
        task = reply.task
    assert task is not None
    while task.status.state not in SETTLED:
        await asyncio.sleep(poll_seconds)
        task = await client.get_task(GetTaskRequest(id=task.id))
    return task


def result_of(task: Task) -> Any:
    """The workflow's return value, or None (not completed, a status-only export, or too large)."""
    for artifact in task.artifacts:
        return json_format.MessageToDict(artifact.parts[0].data)
    return None


async def main(argv: list[str]) -> int:
    base_url, key = os.environ["DURABLE_A2A_URL"], os.environ["DURABLE_RUN_KEY"]
    async with httpx.AsyncClient(headers={"Authorization": f"Bearer {key}"}, timeout=90) as http:
        client = await connect(http, base_url)
        if not argv:
            for skill in await skills(client):
                print(f"{skill.id}: {skill.name}")
            return 0
        task = await run_workflow(client, argv[0], json.loads(argv[1]) if len(argv) > 1 else {})
        print(task.id, TaskState.Name(task.status.state))
        if task.status.state != TaskState.TASK_STATE_COMPLETED:
            print(" ".join(part.text for part in task.status.message.parts))
            return 1
        print(json.dumps(result_of(task), indent=2))
        return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1:])))
