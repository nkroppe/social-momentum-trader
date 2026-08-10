FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    SMT_CONFIG_DIR=/app/config

WORKDIR /app

# Install live + LLM extras so Coinbase/Reddit/Postgres and the text-only Cursor
# SDK judge are available on the VPS.
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --upgrade pip && pip install ".[live,llm]"

COPY config ./config

# Run as non-root.
RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /app/data /app/logs \
    && chown -R appuser:appuser /app
USER appuser

CMD ["smt", "run"]
