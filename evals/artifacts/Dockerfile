# Evaluation-only tools; the runtime's default sandbox image is unchanged.
FROM python:3.12-slim
RUN apt-get update \
 && apt-get install -y --no-install-recommends ripgrep tzdata \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /work
