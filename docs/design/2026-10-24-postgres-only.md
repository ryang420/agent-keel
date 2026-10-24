# PostgreSQL-only storage

Keel is a server-side, multi-tenant runtime. PostgreSQL is its only supported storage engine in production,
local development and database integration tests. SQLite support is removed; there are no deployed SQLite
instances requiring data conversion.

## Decision

Maintaining two engines requires two concurrency models, two migration paths and two testing paths. Keeping
SQLite solely for tests would preserve that cost and would not exercise PostgreSQL row/advisory locks or
cross-instance notifications. PostgreSQL-only storage keeps one behavior for claims, leases, deletion fences
and sandbox lifecycle coordination.

Keep the Store interface and asyncpg adapter, including its placeholder conversion. Remove SQLite-specific code,
configuration, dependencies and runtime event-bus selection. The service always uses PostgreSQL notifications;
the in-memory event bus remains a local delivery implementation and a test double.

## Compatibility

Existing PostgreSQL databases retain their numbered migrations and upgrade in place. The PostgreSQL SQL emitted
by released migrations is unchanged; no schema reset or new migration is needed for removing a backend.
Both `postgresql://` and `postgres://` URLs remain supported. SQLite URLs are rejected.

This supersedes the SQLite development/test option described in earlier design documents. Those documents and
released changelog entries remain historical records.

## Testing

Use Docker Compose for local PostgreSQL test infrastructure and PostgreSQL service containers in CI and release
validation. Testcontainers is unnecessary for the current workflow. PostgreSQL 17 is used by development and test
Compose configurations and CI.

A shared pytest fixture creates a unique empty database for each integration test and drops only that database
when finished. The configured PostgreSQL role needs CREATEDB. The supplied server database is not cleared.
Independent databases isolate parallel test runs without changing production advisory-lock keys or notification
channels. Connections, processes and workers inside a single scenario share that scenario's DSN and commit real
transactions; no outer rollback transaction changes visibility or lock behavior.

Database integration tests require KEEL_TEST_POSTGRES_DSN and fail clearly when it is missing. Pure unit tests
remain available with `pytest -m 'not integration'`. Model and real Docker suites keep their explicit opt-ins.
CI discovers the whole ordinary suite rather than maintaining a module allowlist. Existing migration, concurrency,
subprocess crash/recovery and Docker lifecycle scenarios are retained.
