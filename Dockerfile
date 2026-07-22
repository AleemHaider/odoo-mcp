# Production image — runs the MCP over streamable HTTP for hosted/multi-user use.
FROM python:3.12-slim

# Don't buffer stdout/stderr; no .pyc files.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    MCP_TRANSPORT=http \
    MCP_HOST=0.0.0.0 \
    MCP_PORT=8000

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY server.py ./

# Run as an unprivileged user.
RUN useradd -m appuser
USER appuser

EXPOSE 8000

# Credentials are injected at runtime as env vars (never baked into the image):
#   docker run -p 8000:8000 \
#     -e ODOO_URL=... -e ODOO_DB=... -e ODOO_USER=... -e ODOO_PASSWORD=... \
#     odoo-mcp
CMD ["python", "server.py"]
