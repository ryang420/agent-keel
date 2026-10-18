# syntax=docker/dockerfile:1

# Compile the static supervisor for this image's own platform before installing keel. Under buildx every target
# platform builds its own stage, so no architecture is hardcoded; with plain docker build the stage uses the host's
# platform. The toolchain is only in the builder stage.
FROM python:3.12-slim AS supervisor
RUN apt-get update \
 && apt-get install -y --no-install-recommends musl-tools gcc make \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /build
COPY native/supervisor ./native/supervisor
COPY scripts/build_supervisor.py ./scripts/build_supervisor.py
RUN make -C native/supervisor BUILD=/out CC=musl-gcc CFLAGS="-O2 -g -static" all \
 && python scripts/build_supervisor.py --binary /out/keel-supervisor --output /out/bin

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /srv
COPY --from=docker:27.3.1-cli /usr/local/bin/docker /usr/local/bin/docker
COPY pyproject.toml README.md LICENSE THIRD_PARTY_NOTICES.md ./
COPY src ./src
# The source tree's bin directory is .dockerignored: only the binary built above for this platform is installed.
COPY --from=supervisor /out/bin ./src/keel/sandbox/bin
RUN pip install . && useradd -u 10001 -M -s /usr/sbin/nologin keel
USER 10001
EXPOSE 8700
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8700/healthz', timeout=3)"
CMD ["python", "-m", "keel"]
