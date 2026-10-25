# Pinned evaluation sandbox for the DAB scoring harness (protocol dab-score-v1).
#
# The evaluator builds this image and records its ID; the product's ordinary sandbox image is untouched. DuckDB is
# pinned to the version that wrote the benchmark's DuckDB files. PostgreSQL and MongoDB drivers are present for the
# service-backed datasets; no cloud client is needed.
FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends sqlite3 \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir \
        "duckdb==1.3.1" \
        "psycopg[binary]==3.2.3" \
        "pymongo==4.11.1" \
        "pandas==2.2.3"

WORKDIR /work
