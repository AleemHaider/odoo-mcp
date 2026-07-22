# Multi-user deployment (employees use it, can't change the code)

Host the MCP once on a cloud VM. Employees connect to a URL with a personal
token — they never get the code, the credentials, or the ability to change
anything. Everyone is **read-only** and **audited by name**.

```
Employee Claude ──HTTPS──► Caddy (TLS) ──► MCP server (token auth, read-only) ──► Odoo
                           one VM you control · code + credentials live here
```

Security layers, all verified:
- **Caddy** terminates TLS (automatic Let's Encrypt cert).
- **Per-employee bearer tokens** — the server rejects anyone without a valid
  token (HTTP 401) and records the employee's name on every action.
- **Read-only** (`ODOO_READONLY=1`) — create / write / unlink / write-methods
  are refused server-side; only reads and reporting work.
- **Least privilege** — point it at a dedicated Odoo user with read-only ACLs so
  even the service account can't do damage.

---

## Prerequisites
- A cloud VM (Ubuntu/Debian) with a **public IP** and Docker + Docker Compose.
- A **domain** (e.g. `odoo-mcp.yourcompany.com`) with a DNS **A record** pointing
  at the VM's IP. (Caddy needs this to issue the TLS certificate.)
- Ports **80** and **443** open to the internet.

## Step 1 — get the code onto the VM
```bash
git clone -b production-hardening https://github.com/AleemHaider/odoo-mcp.git
cd odoo-mcp
```

## Step 2 — Odoo credentials
```bash
cp .env.example .env
nano .env      # set ODOO_URL / ODOO_DB / ODOO_USER / ODOO_PASSWORD
```
Use a **dedicated read-only Odoo user** and an **API key** (not a real password).

## Step 3 — create employee tokens
Generate one random token per employee:
```bash
openssl rand -hex 20      # run once per person
```
Put them in `tokens.json` (map token → employee name):
```json
{
  "ea7e57587fbdb4b1ee79474b348a79c7f5d2d01d": "Alice Martinez",
  "665cd5c69974bb92644576c1f31c54ed4a96af1a": "Bob Chen"
}
```
To revoke someone: delete their line and `docker compose restart mcp`.

## Step 4 — set your domain
Edit `Caddyfile` — replace `odoo-mcp.yourcompany.com` with your real domain.

## Step 5 — launch
```bash
docker compose up -d --build
docker compose logs -f mcp     # watch for "http_serving" + the employee list
```
Caddy fetches a TLS cert automatically (may take ~30s on first run).

## Step 6 — give each employee their connect command
One line per person (their own token):
```bash
claude mcp add odoo --transport http https://odoo-mcp.yourcompany.com/mcp \
  --header "Authorization: Bearer <THEIR-TOKEN>"
```
Then they verify with `claude mcp list` (shows `odoo … ✔ Connected`) and start
asking Claude about Odoo. They cannot see the code or credentials, and cannot
write to Odoo.

---

## Operations
| Task | Command |
|---|---|
| View audit log (who did what) | `docker compose logs mcp \| grep mutation` |
| See denied attempts | `docker compose logs mcp \| grep auth_denied` |
| Add / remove an employee | edit `tokens.json` → `docker compose restart mcp` |
| Update to latest code | `git pull` → `docker compose up -d --build` |
| Health check | `curl https://your-domain/healthz` → `{"status":"ok"}` |
| Stop everything | `docker compose down` |

## What each employee can and cannot do
- ✅ Query records, run reports/aggregations, export data (read-only).
- ❌ Create / edit / delete anything (blocked server-side).
- ❌ See the source code, the Odoo credentials, or other employees' tokens.
- ❌ Reach Odoo without a valid token (401).

## Honest limits
- All employees share **one Odoo service account**, so Odoo's own logs show that
  account. Individual attribution lives in **this server's** audit log (by token
  name) — keep those logs.
- Tokens are bearer secrets: anyone holding one can act as that employee. Deliver
  them privately and rotate periodically.
- If you later need employees to **write**, set `ODOO_READONLY=0` and give the
  service account scoped write ACLs — but understand the shared-account trade-off
  first.
