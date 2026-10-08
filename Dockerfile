# Odoo MCP Multi — remote (HTTP) image for use as a claude.ai connector.
#
#   docker build -t odoo-mcp .
#   docker run -p 8000:8000 --env-file .env odoo-mcp
#
# All configuration is via env vars; see .env.example.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    MCP_TRANSPORT=http \
    MCP_HOST=0.0.0.0 \
    MCP_PORT=8000

WORKDIR /app
COPY pyproject.toml README.md server.py odoo_auth.py ./
RUN pip install --no-cache-dir ".[remote]"

# Run as an unprivileged user; nothing here needs root.
RUN useradd --create-home --uid 1000 mcp
USER mcp

EXPOSE 8000
CMD ["odoo-mcp"]
