"""Benchmark PostgreSQL and MongoDB services for the DAB harness.

A dedicated internal Docker network holds the servers, and a sandbox joins only that network: no internet, no
product metadata database. Each case gets a fresh database restored from the pinned dump and a read-only benchmark
identity whose credentials reach the sandbox through the policy environment. Every restore is dropped after the
case, so runs never share state.

The services are evaluator infrastructure. They never receive model credentials, and the harness never prints a
password.
"""

from __future__ import annotations

import hashlib
import secrets
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from .suite import Case

NETWORK = "keel-dab-internal"
POSTGRES = "keel-dab-postgres"
MONGO = "keel-dab-mongo"
POSTGRES_IMAGE = "postgres:17"
MONGO_IMAGE = "mongo:7"
PG_SUPERUSER = "postgres"
READER = "dab_reader"
RESTORE_TIMEOUT = 900


class ServicesError(RuntimeError):
    """A benchmark database could not be started or restored."""


def _run(args: list[str], *, timeout: float = 120.0, check: bool = True) -> subprocess.CompletedProcess:
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        raise ServicesError(f"docker command failed: {exc}") from exc
    if check and result.returncode != 0:
        raise ServicesError(f"{args[0]} {args[1] if len(args) > 1 else ''} failed: {result.stderr.strip()[:400]}")
    return result


def _wait(probe, *, timeout: float, interval: float = 1.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if probe():
            return
        time.sleep(interval)
    raise ServicesError("a benchmark database did not become ready in time")


def _remove_container(name: str) -> None:
    _run(["docker", "rm", "-f", name], check=False, timeout=60)


def _container_exists(name: str) -> bool:
    return _run(["docker", "inspect", name], check=False, timeout=30).returncode == 0


@dataclass
class Services:
    """Started benchmark servers plus the per-case restores they serve."""

    network: str | None = None
    read_password: str = field(default_factory=lambda: secrets.token_hex(16))
    admin_password: str = field(default_factory=lambda: secrets.token_hex(16))
    postgres: bool = False
    mongo: bool = False
    _restored: dict[str, dict] = field(default_factory=dict)

    @classmethod
    def start(cls, kinds: tuple[str, ...]) -> "Services":
        """Start the requested servers on one internal network and create the read-only roles."""
        services = cls()
        if "postgres" not in kinds and "mongo" not in kinds:
            return services
        if _run(["docker", "network", "inspect", NETWORK], check=False, timeout=30).returncode != 0:
            _run(["docker", "network", "create", "--internal", NETWORK])
        services.network = NETWORK
        try:
            if "postgres" in kinds:
                services._start_postgres()
            if "mongo" in kinds:
                services._start_mongo()
        except Exception:
            services.stop()
            raise
        return services

    # -- startup -------------------------------------------------------------------------------------------------

    def _start_postgres(self) -> None:
        _remove_container(POSTGRES)
        _run(["docker", "run", "-d", "--name", POSTGRES, "--network", NETWORK,
              "-e", f"POSTGRES_PASSWORD={self.admin_password}", "-e", "POSTGRES_DB=postgres",
              POSTGRES_IMAGE], timeout=120)
        _wait(self._pg_ready, timeout=120)
        self._psql(["postgres"], "-c", f"""
            DO $$ BEGIN
                IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '{READER}') THEN
                    CREATE ROLE {READER} LOGIN PASSWORD '{self.read_password}';
                END IF;
            END $$;
        """)
        self._psql(["postgres"], "-c", f"ALTER ROLE {READER} SET default_transaction_read_only = on;")
        self.postgres = True

    def _start_mongo(self) -> None:
        _remove_container(MONGO)
        _run(["docker", "run", "-d", "--name", MONGO, "--network", NETWORK, MONGO_IMAGE, "--auth"], timeout=120)
        _wait(self._mongo_ping, timeout=120)
        if not self._mongo_admin_exists():
            # The localhost exception allows the first user only on an unauthenticated local connection.
            self._mongo_eval(
                "db.getSiblingDB('admin').createUser({user:'dab_admin',pwd:%r,roles:['root']})"
                % self.admin_password)
        self.mongo = True

    def _pg_ready(self) -> bool:
        return _run(["docker", "exec", POSTGRES, "pg_isready", "-U", PG_SUPERUSER, "-d", "postgres"],
                    check=False, timeout=30).returncode == 0

    def _mongo_ping(self) -> bool:
        result = _run(["docker", "exec", MONGO, "mongosh", "--quiet", "--eval",
                       "db.runCommand({ping:1}).ok"], check=False, timeout=30)
        return result.returncode == 0 and "1" in result.stdout

    def _mongo_admin_exists(self) -> bool:
        result = self._mongo_eval("db.getSiblingDB('admin').getUsers().length", password=self.admin_password,
                                  check=False)
        return result.returncode == 0 and result.stdout.strip().isdigit() and int(result.stdout.strip()) > 0

    # -- clients -------------------------------------------------------------------------------------------------

    def _psql(self, args: list[str], *flags: str) -> subprocess.CompletedProcess:
        return _run(["docker", "exec", POSTGRES, "psql", "-v", "ON_ERROR_STOP=1", "-U", PG_SUPERUSER,
                     "-d", args[0], *flags], timeout=RESTORE_TIMEOUT)

    def _mongo_eval(self, script: str, *, username: str | None = None, password: str | None = None,
                    check: bool = True) -> subprocess.CompletedProcess:
        command = ["docker", "exec", MONGO, "mongosh", "--quiet"]
        if username:
            command += ["--username", username, "--password", password or "", "--authenticationDatabase", "admin"]
        return _run(command + ["--eval", script], timeout=RESTORE_TIMEOUT, check=check)

    def _mongo_admin_eval(self, script: str) -> subprocess.CompletedProcess:
        return self._mongo_eval(script, username="dab_admin", password=self.admin_password)

    # -- per case ------------------------------------------------------------------------------------------------

    def prepare(self, case: Case, repo: Path) -> dict[str, str]:
        """Restore a fresh database for the case's service stores and return the sandbox environment."""
        kinds = case.dataset.service_dbms
        if not kinds:
            return {}
        nonce = hashlib.sha256(f"{case.case_id}:{secrets.token_hex(8)}".encode()).hexdigest()[:16]
        state: dict = {"postgres": [], "mongo": []}
        env: dict[str, str] = {}
        dataset_dir = case.dataset.directory(repo)
        if "postgres" in kinds:
            database = f"dab_{nonce}"
            self._psql(["postgres"], "-c", f'CREATE DATABASE "{database}"')
            state["postgres"].append(database)
            for client in case.dataset.db_clients.values():
                if client["db_type"] != "postgres":
                    continue
                self._restore_postgres(dataset_dir / client["sql_file"], database)
            self._psql([database], "-c", f'GRANT CONNECT ON DATABASE "{database}" TO {READER};')
            self._psql([database], "-c", f"GRANT USAGE ON SCHEMA public TO {READER};")
            self._psql([database], "-c", f"GRANT SELECT ON ALL TABLES IN SCHEMA public TO {READER};")
            env.update({"PGHOST": POSTGRES, "PGPORT": "5432", "PGDATABASE": database,
                        "PGUSER": READER, "PGPASSWORD": self.read_password})
        if "mongo" in kinds:
            uri = None
            for client in case.dataset.db_clients.values():
                if client["db_type"] != "mongo":
                    continue
                self._restore_mongo(dataset_dir / client["dump_folder"], client["db_name"])
                self._mongo_admin_eval(
                    f"db.getSiblingDB({client['db_name']!r}).createUser({{user:{READER!r},"
                    f"pwd:{self.read_password!r},roles:[{{role:'read',db:{client['db_name']!r}}}]}})")
                state["mongo"].append(client["db_name"])
                uri = (f"mongodb://{READER}:{self.read_password}@{MONGO}:27017/{client['db_name']}"
                       f"?authSource={client['db_name']}")
            if uri is None:
                raise ServicesError(f"{case.case_id}: the dataset has no MongoDB store")
            env["MONGO_URI"] = uri
        self._restored[case.case_id] = state
        return env

    def _restore_postgres(self, sql_file: Path, database: str) -> None:
        target = "/tmp/dab_restore.sql"
        _run(["docker", "cp", str(sql_file), f"{POSTGRES}:{target}"], timeout=RESTORE_TIMEOUT)
        try:
            self._psql([database], "-f", target)
        finally:
            _run(["docker", "exec", POSTGRES, "rm", "-f", target], check=False, timeout=60)

    def _restore_mongo(self, dump_dir: Path, database: str) -> None:
        target = "/tmp/dab_restore"
        _run(["docker", "exec", MONGO, "rm", "-rf", target], check=False, timeout=60)
        _run(["docker", "exec", MONGO, "mkdir", "-p", target], timeout=60)
        _run(["docker", "cp", str(dump_dir), f"{MONGO}:{target}/"], timeout=RESTORE_TIMEOUT)
        try:
            found = _run(["docker", "exec", MONGO, "sh", "-c",
                          f"find {target} -name '*.bson' | head -1"], timeout=60).stdout.strip()
            if not found:
                raise ServicesError(f"{dump_dir}: the dump contains no .bson file")
            source = str(Path(found).parent)
            _run(["docker", "exec", MONGO, "mongorestore", "--username", "dab_admin",
                  "--password", self.admin_password, "--authenticationDatabase", "admin",
                  "--db", database, "--drop", source], timeout=RESTORE_TIMEOUT)
        finally:
            _run(["docker", "exec", MONGO, "rm", "-rf", target], check=False, timeout=60)

    def finish(self, case: Case) -> None:
        """Drop everything this case restored; a failed drop is reported, never left to the next case."""
        state = self._restored.pop(case.case_id, None)
        if state is None:
            return
        for database in state["postgres"]:
            self._psql(["postgres"], "-c", f'DROP DATABASE IF EXISTS "{database}" WITH (FORCE);')
        for database in state["mongo"]:
            self._mongo_admin_eval(f"db.getSiblingDB({database!r}).dropDatabase()")
            self._mongo_admin_eval(f"db.getSiblingDB({database!r}).dropUser({READER!r})")

    def stop(self) -> None:
        """Remove the containers and the internal network."""
        for name in (POSTGRES, MONGO):
            _remove_container(name)
        if self.network or self.postgres or self.mongo:
            _run(["docker", "network", "rm", NETWORK], check=False, timeout=60)
        self.postgres = self.mongo = False

    def images(self) -> list[str]:
        return [image for image, used in ((POSTGRES_IMAGE, self.postgres), (MONGO_IMAGE, self.mongo)) if used]


def available() -> dict[str, bool]:
    """Which service images are present locally; used by ``validate``."""
    return {kind: _run(["docker", "image", "inspect", image], check=False, timeout=30).returncode == 0
            for kind, image in (("postgres", POSTGRES_IMAGE), ("mongo", MONGO_IMAGE))}
