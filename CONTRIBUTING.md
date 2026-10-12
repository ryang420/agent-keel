# Contributing

Thanks for helping. keel is small on purpose; focused changes with tests are the easiest to review.

## Set up

```bash
python3.12 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/python -m pytest -q
```

The default run needs no Docker, database or network. Four suites are opt-in:

- `KEEL_TEST_DOCKER=1` runs the Docker sandbox tests (image from `KEEL_TEST_DOCKER_IMAGE`, default `python:3.12-slim`).
- `KEEL_TEST_POSTGRES_DSN=postgresql://…` runs the store and event-bus tests on Postgres. It drops and recreates
  keel's tables, so point it at a scratch database.
- `KEEL_LIVE_BEDROCK=1` and `KEEL_LIVE_OPENAI=1` call a real model with your credentials.

See [Configuration](docs/guide/configuration.md) for the variables.

## Code

- Match the surrounding code: naming, typing, comment style, line length.
- Keep modules small and focused. A new provider is one small class (see [Providers](docs/guide/providers.md)).
- Every change comes with tests. A bug fix starts with a test that fails.
- Test credentials must be built from pieces, for example `"hunter" + "2"` or `"AKIA" + "ABCDEFGH…"`, so that no
  secret-looking literal sits in the source and secret scanners stay quiet.
- Never put real credentials, hostnames or data of any deployment in code, tests, docs or examples.

## Design changes

A change to behaviour, the API, the schema or the sandbox starts as a design doc in `docs/design/`, named
`YYYY-MM-DD-topic.md`. Look at the existing ones ([architecture](docs/design/2026-10-11-architecture.md),
[agent loop](docs/design/2026-10-12-m2-agent-loop.md)) for the shape. Open an issue or a draft PR with the doc
before writing the code.

## Commits and pull requests

- One logical change per commit. Messages follow `type(scope): summary` in the imperative, for example
  `fix(sandbox): remove the env file on failure`. Common types: `feat`, `fix`, `docs`, `test`, `refactor`, `chore`.
- Update the docs under `docs/guide/` and add a line to [CHANGELOG.md](CHANGELOG.md) when behaviour changes.
- Make sure `.venv/bin/python -m pytest -q` passes, then fill in the pull request template.
- A DCO-style sign-off is not required. By contributing you agree your work is released under the [MIT license](LICENSE).

## Code adapted from hermes-agent

Some files are adapted from [hermes-agent](https://github.com/NousResearch/hermes-agent) (MIT). Keep the source
header comment on those files, and keep [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) accurate when you adapt
more code from it or from another project.

## Security issues

Do not open a public issue for a vulnerability. See [SECURITY.md](SECURITY.md).

## Conduct

Be kind and constructive. See the [Code of Conduct](CODE_OF_CONDUCT.md).
