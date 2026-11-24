"""The plug-in Alembic branch runner (IAM design 5.3): each plug-in that owns tables ships its own revision directory
and its own version table, in the same database, and never touches core's `alembic_version`.

A plug-in declares `[project.entry-points."eap.db.migrations"] <name> = "<module>:<attr>"` where the attribute is the
path (str or Path) of its script location, a directory of Alembic revision files with no `env.py`. Its version table is
`alembic_version_<name>`. `upgrade_plugin_branches` runs after core's `upgrade head` (so a plug-in revision may rely on
a core table, such as `api_keys` from `0017_api_keys`), for every plug-in that is installed AND listed in
configuration; an unlisted plug-in's branch is not touched. `downgrade_plugin_branch` never touches core.
Core migrations never reference a plug-in table, and a plug-in table never has a foreign key into a core table.

Run it as `eap-db plugins upgrade` (the migrate entrypoint does after `alembic upgrade head`) or `eap-db plugins
downgrade <name> [revision]`; with no plug-in installed it does nothing.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from importlib.metadata import entry_points
from pathlib import Path

from alembic.operations import Operations
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, pool
from sqlalchemy.engine import Connection

ENTRY_POINT_GROUP = "eap.db.migrations"
VERSION_TABLE_PREFIX = "alembic_version_"


@dataclass(frozen=True)
class PluginBranch:
    name: str
    script_location: Path

    @property
    def version_table(self) -> str:
        return f"{VERSION_TABLE_PREFIX}{self.name}"


def configured_plugins(env: Mapping[str, str] | None = None) -> set[str]:
    """Names of the IAM plug-ins listed in configuration (identity, policy, membership): the ones whose branch runs."""
    env = os.environ if env is None else env
    names: set[str] = set()
    for var in ("EAP_IAM_IDENTITY", "EAP_IAM_POLICY", "EAP_IAM_MEMBERSHIP"):
        names.update(part.strip() for part in env.get(var, "").split(",") if part.strip())
    return names


def installed_branches() -> list[PluginBranch]:
    branches: list[PluginBranch] = []
    for ep in sorted(entry_points(group=ENTRY_POINT_GROUP), key=lambda e: e.name):
        branches.append(PluginBranch(name=ep.name, script_location=Path(ep.load()).resolve()))
    return branches


def _script(branch: PluginBranch) -> ScriptDirectory:
    return ScriptDirectory(str(branch.script_location))


def _context(connection: Connection, branch: PluginBranch, fn: Callable) -> MigrationContext:
    return MigrationContext.configure(connection, opts={"fn": fn, "version_table": branch.version_table})


def upgrade_branch(connection: Connection, branch: PluginBranch, target: str = "head") -> None:
    script = _script(branch)
    ctx = _context(connection, branch, lambda rev, _ctx: script._upgrade_revs(target, rev))
    with Operations.context(ctx), ctx.begin_transaction():  # Operations.context makes `alembic.op` work in revisions
        ctx.run_migrations()


def downgrade_branch(connection: Connection, branch: PluginBranch, target: str = "base") -> None:
    script = _script(branch)
    ctx = _context(connection, branch, lambda rev, _ctx: script._downgrade_revs(target, rev))
    with Operations.context(ctx), ctx.begin_transaction():  # Operations.context makes `alembic.op` work in revisions
        ctx.run_migrations()


def upgrade_plugin_branches(
    connection: Connection, listed: Iterable[str] | None = None, branches: Iterable[PluginBranch] | None = None
) -> list[str]:
    """Upgrade every installed, listed plug-in branch to head; the names upgraded, in order."""
    wanted = configured_plugins() if listed is None else set(listed)
    done: list[str] = []
    for branch in installed_branches() if branches is None else branches:
        if branch.name not in wanted:
            continue
        upgrade_branch(connection, branch)
        done.append(branch.name)
    return done


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args[:2] not in (["plugins", "upgrade"],) and not (len(args) >= 3 and args[:2] == ["plugins", "downgrade"]):
        print("usage: eap-db plugins upgrade | eap-db plugins downgrade <name> [revision]", file=sys.stderr)
        return 2
    url = os.environ.get("EAP_DATABASE_URL")
    if not url:
        print("EAP_DATABASE_URL is not set", file=sys.stderr)
        return 2
    engine = create_engine(url, poolclass=pool.NullPool)
    try:
        with engine.begin() as conn:
            if args[1] == "upgrade":
                done = upgrade_plugin_branches(conn)
                print("plug-in branches upgraded: " + (", ".join(done) or "none"))
            else:
                branch = next((b for b in installed_branches() if b.name == args[2]), None)
                if branch is None:
                    print(f"no installed plug-in branch {args[2]!r}", file=sys.stderr)
                    return 2
                downgrade_branch(conn, branch, args[3] if len(args) > 3 else "base")
    finally:
        engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
