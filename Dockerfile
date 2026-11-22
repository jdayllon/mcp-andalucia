FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim AS builder

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev

COPY README.md ./
COPY src/ ./src/
RUN uv sync --frozen --no-dev


FROM python:3.13-slim-bookworm

RUN useradd --create-home --shell /bin/bash appuser \
    && mkdir -p /data \
    && chown -R appuser:appuser /data

WORKDIR /app
COPY --from=builder --chown=appuser:appuser /app /app

ENV PATH="/app/.venv/bin:$PATH" \
    MCP_ANDALUCIA_HOST="0.0.0.0" \
    MCP_ANDALUCIA_PORT="8000" \
    MCP_ANDALUCIA_DATA_DIR="/data" \
    MCP_ANDALUCIA_LOG_LEVEL="INFO" \
    MCP_ANDALUCIA_LOG_FORMAT="text"

USER appuser
EXPOSE 8000
VOLUME ["/data"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3)"

CMD ["mcp-andalucia", "--transport", "http", "--host", "0.0.0.0", "--port", "8000"]
