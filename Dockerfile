FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /srv
COPY --from=docker:27.3.1-cli /usr/local/bin/docker /usr/local/bin/docker
COPY pyproject.toml LICENSE THIRD_PARTY_NOTICES.md ./
COPY src ./src
RUN pip install . && useradd -u 10001 -M -s /usr/sbin/nologin keel
USER 10001
EXPOSE 8700
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8700/healthz', timeout=3)"
CMD ["python", "-m", "keel"]
