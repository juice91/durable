"""The `init` service of the Compose quick start (compose.yaml): per-install secrets, generated once.

usage: compose-init.py SECRETS_DIR [BLOB_DIR [INITDB_DIR [POSTGRES_DIR]]]

Each secret is a file in SECRETS_DIR, written only when it is absent, so a second `docker compose up` changes no value.
Values come from the operating system's CSPRNG. Neither a value, a name nor a path of a secret is printed.

Nothing is readable by everyone or by a group: each directory is a private volume that belongs to the one user that
reads it, and so does each file (0400). SECRETS_DIR is the platform's (10001) and the source of every value; the files
only Postgres reads are root's there, and POSTGRES_DIR, Postgres's volume, receives a copy of them for its user (999).
Owner, mode and copies are applied again on every run, so a volume made by an earlier version is narrowed. BLOB_DIR,
when given, is handed to the platform's user. INITDB_DIR, when given, receives Postgres's first-start script
(compose-postgres-init.sh beside this file), so that the stack mounts no file of the checkout."""

from __future__ import annotations

import base64
import os
import secrets
import stat
import sys
from collections.abc import Callable
from pathlib import Path

PLATFORM_ID = 10001  # the user of the platform image (deploy/docker/python.Dockerfile)
POSTGRES_ID = 999  # the user `postgres` of the pgvector image
ROOT_ID = 0
FILE_MODE = 0o400
DIRECTORY_MODE = 0o500  # the owner alone; only root (this service) writes
# The files Postgres and its first-start script read; the platform reads every other one.
POSTGRES_FILES = frozenset({"postgres_password", "app_db_password", "dbos_db_password"})
POSTGRES_SCRIPT = Path(__file__).with_name("compose-postgres-init.sh")


def _hex() -> str:
    return secrets.token_hex(32)


def generators(read: Callable[[str], str]) -> dict[str, Callable[[], str]]:
    """File name to value, in the order they are made. The two URLs are built from the passwords before them."""
    return {
        "postgres_password": _hex,
        "app_db_password": _hex,
        "dbos_db_password": _hex,
        "database_url": lambda: f"postgresql+psycopg://eap_app:{read('app_db_password')}@postgres:5432/eap_app",
        "dbos_url": lambda: f"postgresql://eap_dbos:{read('dbos_db_password')}@postgres:5432/eap_dbos",
        # Base64 of exactly 32 bytes (eap_secrets). Losing it makes the stored secrets unreadable: back the volume up.
        "secrets_key": lambda: base64.b64encode(secrets.token_bytes(32)).decode(),
        "audit_hash_salt": _hex,
        # 64 hex characters: 256 bits, and it can never look like a placeholder to the sign-in plug-in.
        "local_auth_signing_key": _hex,
    }


def _restrict(path: Path, owner: int, mode: int) -> None:
    if os.geteuid() == ROOT_ID:  # only root can give a file away; the tests run as an ordinary user
        os.chown(path, owner, owner)
    os.chmod(path, mode)


def _open_directory(directory: Path) -> None:
    directory.mkdir(mode=stat.S_IRWXU, parents=True, exist_ok=True)
    if not os.access(directory, os.W_OK):  # never true for root; an ordinary owner gets it back until the end
        os.chmod(directory, stat.S_IRWXU)


def _write_new(path: Path, value: str) -> None:
    """Create or replace `path` atomically: a crash leaves either what was there or the whole value."""
    partial = path.with_name(f".{path.name}.partial")
    partial.unlink(missing_ok=True)
    descriptor = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as handle:
        handle.write(value)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(partial, path)


def fill(directory: Path) -> int:
    """Make the missing files of SECRETS_DIR and return how many were made. An existing value is never rewritten."""
    _open_directory(directory)

    def read(name: str) -> str:
        return (directory / name).read_text()

    made = 0
    for name, make in generators(read).items():
        path = directory / name
        if not path.exists():
            _write_new(path, make())
            made += 1
        elif not path.stat().st_size:
            # Deliberately without the name: `ls -l` on the volume shows the file of size 0.
            raise SystemExit("init: a file in the secrets volume is empty: remove it to have it generated again")
        _restrict(path, ROOT_ID if name in POSTGRES_FILES else PLATFORM_ID, FILE_MODE)
    _restrict(directory, PLATFORM_ID, DIRECTORY_MODE)
    return made


def copy_for_postgres(source: Path, target: Path) -> None:
    """Postgres's own copy of the files it reads, brought in step with SECRETS_DIR."""
    _open_directory(target)
    for name in sorted(POSTGRES_FILES):
        value = (source / name).read_text()
        path = target / name
        if not path.exists() or path.read_text() != value:
            _write_new(path, value)
        _restrict(path, POSTGRES_ID, FILE_MODE)
    _restrict(target, POSTGRES_ID, DIRECTORY_MODE)


def main(argv: list[str]) -> int:
    if len(argv) not in (1, 2, 3, 4):
        print("usage: compose-init.py SECRETS_DIR [BLOB_DIR [INITDB_DIR [POSTGRES_DIR]]]", file=sys.stderr)
        return 2
    made = fill(Path(argv[0]))
    print(f"init: generated {made} secrets" if made else "init: secrets present, nothing generated")
    if len(argv) >= 2:
        blobs = Path(argv[1])
        blobs.mkdir(parents=True, exist_ok=True)
        if os.geteuid() == 0:
            os.chown(blobs, PLATFORM_ID, PLATFORM_ID)
    if len(argv) >= 3:
        script = Path(argv[2]) / "01-setup-databases.sh"
        script.parent.mkdir(parents=True, exist_ok=True)
        script.unlink(missing_ok=True)
        script.write_bytes(POSTGRES_SCRIPT.read_bytes())
        script.chmod(0o555)
    if len(argv) == 4:
        copy_for_postgres(Path(argv[0]), Path(argv[3]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
