"""`python -m eap_iam_openfga.bootstrap`: find or create the OpenFGA store and write the model when it changed.

Idempotent: a second run on an unchanged model writes nothing and prints the same ids. `--dry-run` reads and plans, and
writes nothing (not even the store). It prints the store id and model id, which are not secrets, and the settings to put
in the deployment; the preshared key comes from `EAP_OPENFGA_PRESHARED_KEY` (or `--key-file`) and is never printed."""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, TextIO

import httpx2
from eap_iam import PolicyUnavailable

from eap_iam_openfga.client import OpenFgaClient
from eap_iam_openfga.dsl import ModelSyntaxError, parse_model, semantic
from eap_iam_openfga.model import model_text
from eap_iam_openfga.settings import (
    DEFAULT_TIMEOUT,
    ENV_KEY,
    ENV_KEY_FALLBACK,
    ENV_MODEL_ID,
    ENV_STORE_ID,
    ENV_URL,
    OpenFgaSettings,
    server_url,
)

DEFAULT_STORE_NAME = "durable"


class BootstrapError(Exception):
    """A step cannot continue; the message names what to fix and carries no credential."""


@dataclass
class Result:
    store_id: str = ""
    model_id: str = ""
    store_created: bool = False
    model_written: bool = False
    dry_run: bool = False


async def run(
    base: OpenFgaSettings, *, store_name: str, model_text_: str, store_id: str | None, dry_run: bool,
    http: httpx2.AsyncClient | None = None,
) -> Result:
    try:
        desired = parse_model(model_text_)
    except ModelSyntaxError as exc:
        raise BootstrapError(f"the model file does not parse: {exc}") from None
    result = Result(dry_run=dry_run)
    top = OpenFgaClient(replace(base, store_id=""), http)
    try:
        if store_id:
            result.store_id = store_id
        else:
            matches = [s for s in await top.list_stores() if s.get("name") == store_name and not s.get("deleted_at")]
            if len(matches) > 1:
                raise BootstrapError(f"{len(matches)} stores are named {store_name!r}; pass --store-id to choose one")
            if matches:
                result.store_id = str(matches[0]["id"])
            elif dry_run:
                result.store_created = True  # would be
                return result
            else:
                result.store_id = await top.create_store(store_name)
                result.store_created = True
        client = OpenFgaClient(replace(base, store_id=result.store_id, model_id=None), http)
        latest = await client.latest_model()
        if latest is not None and semantic(latest) == semantic(desired):
            result.model_id = str(latest["id"])
        elif dry_run:
            result.model_written = True  # would be
        else:
            result.model_id = await client.write_model(desired)
            result.model_written = True
        return result
    finally:
        if http is None:
            await top.aclose()


def render(result: Result) -> str:
    verb = "would be " if result.dry_run else ""
    lines = [
        f"store: {'(not created: dry run)' if result.dry_run and result.store_created and not result.store_id else result.store_id}"
        f"{f' ({verb}created)' if result.store_created else ''}",
        f"model: {result.model_id or '(none yet)'}{f' ({verb}written)' if result.model_written else ' (unchanged)'}",
        "",
    ]
    if not (result.dry_run and not result.model_id):
        lines += [
            "Set in the deployment (not secrets):",
            f"  {ENV_STORE_ID}={result.store_id}",
            f"  {ENV_MODEL_ID}={result.model_id}",
        ]
    return "\n".join(lines) + "\n"


def _args(argv: list[str] | None, env: Mapping[str, str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="python -m eap_iam_openfga.bootstrap", description=__doc__.split("\n\n")[0])
    p.add_argument("--url", default=env.get(ENV_URL, ""), help=f"OpenFGA base URL (default: ${ENV_URL})")
    p.add_argument("--store-name", default=DEFAULT_STORE_NAME)
    p.add_argument("--store-id", default=env.get(ENV_STORE_ID, "") or None, help="use this store instead of finding one by name")
    p.add_argument("--model-file", type=Path, default=None, help="the model (default: the one this plug-in ships)")
    p.add_argument("--key-file", type=Path, default=None, help=f"file holding the preshared key (default: ${ENV_KEY})")
    p.add_argument("--dry-run", action="store_true", help="read and plan; write nothing")
    return p.parse_args(argv)


async def amain(
    argv: list[str] | None = None, *, env: Mapping[str, str] | None = None, out: TextIO | None = None,
    http: httpx2.AsyncClient | None = None,
) -> int:
    env = os.environ if env is None else env
    out = out or sys.stdout
    args = _args(argv, env)
    if not args.url:
        print(f"no OpenFGA URL: pass --url or set {ENV_URL}", file=sys.stderr)
        return 2
    key = ""
    if args.key_file:
        try:
            key = args.key_file.read_text(encoding="utf-8").strip()
        except OSError as exc:
            print(f"cannot read the key file: {type(exc).__name__}", file=sys.stderr)
            return 2
    key = key or (env.get(ENV_KEY, "") or env.get(ENV_KEY_FALLBACK, "")).strip()
    try:
        base = OpenFgaSettings(url=server_url(args.url), store_id="", preshared_key=key or None, timeout=max(DEFAULT_TIMEOUT, 10.0))
        text = args.model_file.read_text(encoding="utf-8") if args.model_file else model_text()
        result = await run(base, store_name=args.store_name, model_text_=text, store_id=args.store_id, dry_run=args.dry_run, http=http)
    except (BootstrapError, PolicyUnavailable) as exc:
        print(f"bootstrap failed: {getattr(exc, 'detail', None) or exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # a bad URL or an unreadable model file; never echo a value
        print(f"bootstrap failed: {type(exc).__name__}", file=sys.stderr)
        return 2
    out.write(render(result))
    return 0


def main() -> None:
    sys.exit(asyncio.run(amain()))


if __name__ == "__main__":
    main()
