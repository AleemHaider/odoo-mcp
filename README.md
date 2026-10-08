# Odoo MCP Multi (own build)

A from-scratch, open MCP server that turns Claude (or any MCP client) into an
operator of the **Odoo ORM** — exactly the 12 tools described in
[`Odoo_MCP_Tools_Guide.md`](./Odoo_MCP_Tools_Guide.md).

It is a thin, ergonomic wrapper around Odoo's **external API (XML-RPC)**:

```
Claude ──(MCP tool)──► server.py ──(XML-RPC /xmlrpc/2/object)──► Odoo (PostgreSQL)
       ◄──(structured JSON)──               ◄──(ORM data)──
```

Everything ultimately routes through one call:
`models.execute_kw(db, uid, key, model, method, args, kwargs)`.

---

## The 12 tools

| Tool | Group | Risk | Purpose |
|---|---|---|---|
| `list_available_profiles` | Discover | — | See configured environments (call FIRST) |
| `get_version` | Discover | — | Odoo server version |
| `list_models` | Discover | — | Find tables (models) |
| `list_fields` | Discover | — | See a model's schema |
| `search_read` | Read | — | **Query data** (returns pagination envelope) |
| `get_financial_report` | Read | — | Native reports (Odoo 17+) |
| `create` | Write | ⚠️ | Create a record |
| `write` | Write | ⚠️ | Update records |
| `unlink` | Write | ⚠️⚠️ | Delete records |
| `execute_kw` | Write | ⚠️⚠️ | **Run ANY model method** |
| `export_records` | Bulk | — | Export (backup/migration) |
| `import_records` | Bulk | ⚠️ | Import/upsert in bulk |

---

## Quick install — one command, no clone (recommended)

Requires [`uv`](https://docs.astral.sh/uv/) (`pip install uv`). This fetches the
code straight from GitHub, installs it in an isolated env, and registers it with
Claude — credentials are passed inline as env vars, so there's no file to edit:

```bash
claude mcp add odoo -s user \
  -e ODOO_URL=https://your-instance.odoo.com \
  -e ODOO_DB=your-db-name \
  -e ODOO_USER=you@company.com \
  -e ODOO_PASSWORD=your-api-key \
  -- uvx --from git+https://github.com/AleemHaider/odoo-mcp odoo-mcp
```

That's the whole install. Start Claude and ask *"List my Odoo profiles"*.
For multiple Odoo instances, use the `profiles.json` flow below instead.

---

## Setup (manual / multi-profile)

### 1. Install
```bash
pip install -r requirements.txt      # installs fastmcp
```

### 2. Configure profiles
Copy the example and fill in your Odoo credentials:
```bash
cp profiles.example.json profiles.json
```
```json
{
  "default": "Interhi",
  "profiles": {
    "Interhi": {
      "url": "https://your-instance.odoo.com",
      "database": "your-db-name",
      "username": "admin",
      "password": "your-api-key-or-password"
    }
  }
}
```

> **Tip:** In Odoo, generate an **API key** (Settings → Users → API Keys) and use
> it as the `password`. Mark production profiles with `"readonly": true` to block
> all mutations from that profile.

**Alternative (single profile via env vars, no file):**
```bash
export ODOO_URL="https://your-instance.odoo.com"
export ODOO_DB="your-db-name"
export ODOO_USER="admin"
export ODOO_PASSWORD="your-api-key"
```

### 3. Run
```bash
python server.py          # speaks MCP over stdio
```

---

## Connect it to Claude

### Claude Desktop
Add to `claude_desktop_config.json`
(macOS: `~/Library/Application Support/Claude/`, Windows: `%APPDATA%\Claude\`):
```json
{
  "mcpServers": {
    "odoo": {
      "command": "python",
      "args": ["/absolute/path/to/odoo mcp/server.py"]
    }
  }
}
```

### Claude Code (CLI)
```bash
claude mcp add odoo -- python "/absolute/path/to/odoo mcp/server.py"
```

Restart the client; the 13 tools appear automatically.

---

## Public connector: anyone signs in with their own Odoo (`MCP_AUTH=odoo`)

The modes above protect *one* Odoo with *your* GitHub/Google login. `odoo`
mode is different: it is a **public, multi-tenant connector**. Any claude.ai
user adds the URL, and the OAuth login page asks for **their** Odoo URL,
database, username and API key. No Odoo credentials live on the server.

```
claude.ai ─OAuth─► /login (Odoo creds form) ─validate─► user's Odoo
          ─bearer─► /mcp ─(creds from token)─► user's Odoo
```

- Each user's credentials are validated against their Odoo, **encrypted** with
  `MCP_SECRET_KEY`, and stored in Redis under an opaque subject. Tokens are
  stored hashed; the store never holds a usable token or plaintext key.
- A **read-only** checkbox on the login page blocks create/write/unlink/import
  for that connection.
- Access tokens last 1 hour and refresh silently for 30 days. Users revoke by
  deleting the API key in Odoo or removing the connector in Claude.
- Failed logins are rate-limited per IP.

### Deploy on Vercel (container)

[`Dockerfile.vercel`](./Dockerfile.vercel) is auto-detected by Vercel and runs
the server as a scale-to-zero container. Because every request may hit a fresh
instance, OAuth state **must** live in Redis.

1. Create the project: `vercel link` (or import the repo in the dashboard).
2. Add **Upstash Redis** from the Vercel Marketplace (Storage tab). It injects a
   `REDIS_URL`/`KV_URL`-style variable; copy its value into `MCP_STORAGE_URL`.
3. Set env vars (Project → Settings → Environment Variables):

   | Var | Value |
   |---|---|
   | `MCP_AUTH` | `odoo` |
   | `MCP_PUBLIC_URL` | `https://<your-project>.vercel.app` (or your domain) |
   | `MCP_SECRET_KEY` | output of `openssl rand -hex 32` |
   | `MCP_STORAGE_URL` | the Upstash `rediss://…` URL |
   | `MCP_SERVER_NAME` | name shown on the login page (optional) |

4. `vercel deploy --prod`.
5. Check `https://<domain>/.well-known/oauth-protected-resource/mcp` returns JSON.

Users then add `https://<domain>/mcp` as a custom connector in claude.ai,
click Connect, and fill in their Odoo details once.

The same image runs anywhere Docker runs (`docker run --env-file .env
-p 8000:8000 odoo-mcp`); only the Redis requirement is Vercel-specific.

### Getting into the claude.ai connector directory

Working by URL as a custom connector needs nothing from Anthropic. Appearing
in the browsable directory is a separate partnership/listing process run by
Anthropic; the server already meets the technical requirements (remote
Streamable HTTP, OAuth 2.1 with PKCE and dynamic client registration).

---

## Use it as a claude.ai connector (remote HTTP + OAuth)

Claude Desktop and Claude Code launch the server locally over stdio. A
**claude.ai custom connector** is different: Anthropic's servers connect to
*your* server over HTTPS, so it must be reachable on a public URL and must
authenticate the caller. The same `server.py` does this when
`MCP_TRANSPORT=http`.

```
claude.ai ──HTTPS + OAuth──► https://odoo-mcp.example.com/mcp ──XML-RPC──► Odoo
```

### 1. Pick an auth mode

| `MCP_AUTH` | Works in claude.ai? | What it is |
|---|---|---|
| `github` | ✅ | Users log in with GitHub. Needs a GitHub OAuth App. |
| `google` | ✅ | Users log in with Google. Needs a Google OAuth client. |
| `auth0` / `workos` | ✅ | Hosted identity providers. |
| `jwt` | ✅ (with your own IdP) | Verifies tokens minted elsewhere via JWKS. |
| `odoo` | ✅ | **Public multi-tenant**: users sign in with their own Odoo. See the section above. |
| `token` | ❌ (Claude Code / curl only) | Static bearer tokens. |
| `none` | ❌ never deploy this | Only with `MCP_ALLOW_UNAUTHENTICATED=1`, local testing. |

> **Always set `MCP_ALLOWED_USERS`** for `github`/`google`. OAuth proves *who*
> someone is; the allowlist decides whether they may touch your Odoo. Without
> it, any GitHub/Google account can log in.

For `github`: create an OAuth App at <https://github.com/settings/developers>
with **Authorization callback URL** = `https://odoo-mcp.example.com/auth/callback`.
For `google`: create an OAuth client (Web application) in Google Cloud Console
with the same redirect URI.

### 2. Configure

```bash
cp .env.example .env     # fill in Odoo creds, MCP_PUBLIC_URL, MCP_AUTH, OAUTH_*, MCP_ALLOWED_USERS
```

Odoo credentials stay on the server as env vars; Claude never sees them.
Consider `ODOO_READONLY=1` for a first deployment — `unlink` and `execute_kw`
can delete or mutate anything.

### 3. Deploy

Any host that gives you an HTTPS domain works. With Docker:

```bash
docker build -t odoo-mcp .
docker run -d -p 8000:8000 --env-file .env odoo-mcp
```

Or without Docker: `pip install .` then `odoo-mcp` with the same env vars.
Put it behind TLS (Caddy, nginx, Cloudflare Tunnel, or your PaaS's built-in
HTTPS on Fly.io / Railway / Render). `MCP_PUBLIC_URL` must equal the public
origin, since OAuth redirects back to it.

Sanity check from outside:
```bash
curl https://odoo-mcp.example.com/.well-known/oauth-protected-resource/mcp
```
should return JSON naming your server as the resource.

### 4. Add it in claude.ai

Settings → Connectors → **Add custom connector** → URL
`https://odoo-mcp.example.com/mcp`. Leave client ID/secret blank: the server
supports Dynamic Client Registration, so claude.ai registers itself. You'll be
sent through your provider's login, then the 13 tools appear.

### Local test without OAuth

```bash
MCP_TRANSPORT=http MCP_AUTH=token MCP_BEARER_TOKENS=dev-secret \
ODOO_URL=... ODOO_DB=... ODOO_USER=... ODOO_PASSWORD=... python server.py
```
```bash
claude mcp add odoo-remote --transport http http://localhost:8000/mcp \
  --header "Authorization: Bearer dev-secret"
```

All HTTP settings are documented at the top of [`server.py`](./server.py) and in
[`.env.example`](./.env.example).

---

## Key concepts (how to drive it)

- **Domains** use Odoo **prefix (Polish) notation** — never the words `and`/`or`:
  ```
  ["&", ["state","=","posted"], ["move_type","=","out_invoice"]]
  ["|", ["a","=",1], ["b","=",2]]
  ```
- **Every read returns a pagination envelope**:
  ```json
  {"records": [...], "total": 128, "limit": 100, "offset": 0,
   "has_more": true, "next_offset": 100, "format": "json"}
  ```
  If `has_more` is true, call again with `offset = next_offset`.
- **Output `format`**: `json` (process), `compact` (light array-of-arrays),
  `table` (Markdown for users), `html` (paste into Odoo), `csv` (spreadsheet).
- **IDs for methods** always go in a list: `args=[[490749]]`.
- **Relational command tuples**: `[[0,0,{...}]]` (new o2m line),
  `[[6,0,[id1,id2]]]` (set m2m).
- **execute_kw is the swiss-army knife** — any Odoo button = a method behind it:
  `account.move / action_post / [[490749]]` validates an invoice.

## Safety notes
- Always run `list_available_profiles` first to confirm the environment.
- Read-only introspection via `execute_kw` (search/read/fields_get/read_group…)
  is allowed on `readonly` profiles; mutating methods are blocked.
- Errors come back as `{"success": false, "error": "..."}` with the cleaned
  Odoo message (e.g. `Invalid leaf`, `Expected singleton`).

---

*Wraps the Odoo external API. Concept & tool set documented in
`Odoo_MCP_Tools_Guide.md`.*
