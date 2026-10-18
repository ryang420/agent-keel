# syntax=docker/dockerfile:1
#
# Build-only packaging image: it exists to compile the static supervisor natively for the target platform and install
# keel with the matching helper already packaged under `keel/sandbox/bin`. It is never deployed and nothing references
# it at run time; the runtime image is the repository's root Dockerfile. Build from the repository root:
#
#   docker build -f native/supervisor/Dockerfile .
#   docker buildx build --platform linux/amd64,linux/arm64 -f native/supervisor/Dockerfile .
#
# With buildx each target platform compiles in its own stage, so no architecture is hardcoded. Nothing is downloaded
# at run time; the toolchain exists only in the builder stage.

FROM python:3.12-slim AS toolchain
RUN apt-get update \
 && apt-get install -y --no-install-recommends musl-tools gcc make \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /build
COPY native/supervisor ./native/supervisor
RUN make -C native/supervisor BUILD=/out CC=musl-gcc CFLAGS="-O2 -g -static" all

FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /srv
COPY pyproject.toml README.md LICENSE THIRD_PARTY_NOTICES.md ./
COPY src ./src
COPY native/supervisor ./native/supervisor
COPY scripts/build_supervisor.py ./scripts/build_supervisor.py
COPY --from=toolchain /out/keel-supervisor /srv/keel-supervisor
RUN python scripts/build_supervisor.py --binary /srv/keel-supervisor --output src/keel/sandbox/bin \
 && pip install . \
 && rm -f /srv/keel-supervisor
