FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PMX_CONFIG=/app/config.yaml \
    PMX_DATA_DIR=/data \
    TZ=UTC

RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install .

COPY config.yaml ./config.yaml
COPY config ./config

VOLUME ["/data"]
CMD ["pmx", "run"]
