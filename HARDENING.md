# Production hardening

This branch (`production-hardening`) turns the single-user desktop MCP into a
service you can host. It is intentionally **kept off `main`** — `main` stays the
simple, local, stdio version.

## What this branch adds

| Concern | Hardening |
|---|---|
| **Transport** | `MCP_TRANSPORT=http` runs streamable HTTP (multi-user / remote). Default stays `stdio`. |
| **Thread safety** | A fresh `ServerProxy` is built per call (xmlrpc.client is not thread-safe); the uid is cached, the connection is not. |
| **Timeouts** | Every XML-RPC call is bounded by `ODOO_TIMEOUT` (default 30s) — no more hung requests. |
| **Retries** | Transient network errors retry `ODOO_MAX_RETRIES` times with exponential backoff. |
| **Session refresh** | A dead Odoo session is detected, the uid is invalidated, and the call re-authenticates once automatically. |
| **Audit log** | Every mutation (create/write/unlink/import/execute_kw-non-read) emits a structured JSON line: who, what model/method, which ids, when. |
| **Structured logging** | JSON logs to **stderr** (stdout is reserved for the MCP protocol); level via `ODOO_LOG_LEVEL`. |
| **Containerization** | `Dockerfile` runs the HTTP server as a non-root user; credentials injected at runtime. |

## Configuration (env vars)

| Var | Default | Purpose |
|---|---|---|
| `MCP_TRANSPORT` | `stdio` | `stdio` or `http` |
| `MCP_HOST` | `0.0.0.0` | HTTP bind host |
| `MCP_PORT` | `8000` | HTTP bind port |
| `ODOO_TIMEOUT` | `30` | per-call network timeout (s) |
| `ODOO_MAX_RETRIES` | `2` | retries on transient failure |
| `ODOO_RETRY_BACKOFF` | `0.5` | base backoff (s), doubles each retry |
| `ODOO_LOG_LEVEL` | `INFO` | `DEBUG`/`INFO`/`WARNING`/… |
| `ODOO_URL` / `ODOO_DB` / `ODOO_USER` / `ODOO_PASSWORD` | — | single-profile credentials |

## Run it

### Local HTTP
```bash
MCP_TRANSPORT=http ODOO_URL=https://your.odoo.com ODOO_DB=db \
ODOO_USER=you@co.com ODOO_PASSWORD=key python server.py
# serves MCP at http://localhost:8000
```

### Docker
```bash
docker build -t odoo-mcp .
docker run -p 8000:8000 \
  -e ODOO_URL=https://your.odoo.com -e ODOO_DB=db \
  -e ODOO_USER=you@co.com -e ODOO_PASSWORD=key \
  odoo-mcp
```

## What is still NOT production-grade (deliberately out of scope)

Be honest with yourself before exposing this publicly:

- **No authentication on the MCP endpoint itself.** Anyone who can reach the HTTP
  port gets full ORM access under the configured Odoo user. Put it behind an
  authenticating reverse proxy / API gateway, or a private network.
- **Secrets are env vars**, not a managed vault (KMS / Vault / Secrets Manager).
- **The read-only guard is a method-name heuristic**, not a real permission model.
  For true least-privilege, give each profile an Odoo user with scoped ACLs.
- **No dry-run / transactional rollback / rate limiting** (see the roadmap in
  `Odoo_MCP_Tools_Guide.md` §8).
- **Single shared credential set per profile** — not per-end-user identity.

Treat this branch as "hostable with care behind your own auth + network
controls", not "safe to expose on the open internet as-is".
