#!/usr/bin/env python3
"""
Odoo MCP Multi — an MCP server that turns Claude into an operator of the Odoo ORM.

This is a faithful, from-scratch reimplementation of the server described in
`Odoo_MCP_Tools_Guide.md`. It exposes 12 tools that wrap Odoo's external API
(XML-RPC) with ergonomic conveniences: multi-profile connections, a pagination
envelope on every read, five output formats, Odoo "command tuple" support and
verbose, actionable error handling.

Transport: stdio (the standard way an MCP client such as Claude Desktop /
Claude Code launches a local server).

Run:      python server.py
Config:   profiles.json  (copy from profiles.example.json) or env vars
          ODOO_URL / ODOO_DB / ODOO_USER / ODOO_PASSWORD for a single "default"
          profile.
"""

from __future__ import annotations

import ast
import csv
import io
import json
import os
import sys
import xmlrpc.client
from functools import lru_cache, wraps
from html import escape
from typing import Any

from fastmcp import FastMCP

mcp = FastMCP("Odoo MCP Multi")

# --------------------------------------------------------------------------- #
# 1. Profile configuration (defined on the HOST, never inside Claude)
# --------------------------------------------------------------------------- #

def _load_profiles() -> tuple[dict[str, dict], str]:
    """Load profiles from profiles.json, or fall back to env vars.

    Returns (profiles_dict, default_profile_name).
    """
    here = os.path.dirname(os.path.abspath(__file__))
    cfg_path = os.environ.get("ODOO_PROFILES_FILE", os.path.join(here, "profiles.json"))

    if os.path.exists(cfg_path):
        with open(cfg_path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        profiles = data.get("profiles", {})
        default = data.get("default") or (next(iter(profiles), None))
        if not profiles:
            raise RuntimeError(f"No profiles defined in {cfg_path}")
        return profiles, default

    # Fallback: a single profile from environment variables.
    url = os.environ.get("ODOO_URL")
    if not url:
        raise RuntimeError(
            "No profiles.json found and ODOO_URL is not set. "
            "Copy profiles.example.json -> profiles.json and fill it in, "
            "or export ODOO_URL / ODOO_DB / ODOO_USER / ODOO_PASSWORD."
        )
    profiles = {
        "default": {
            "url": url,
            "database": os.environ.get("ODOO_DB", ""),
            "username": os.environ.get("ODOO_USER", ""),
            "password": os.environ.get("ODOO_PASSWORD", ""),
            "readonly": os.environ.get("ODOO_READONLY", "").lower() in ("1", "true", "yes"),
        }
    }
    return profiles, "default"


PROFILES, DEFAULT_PROFILE = _load_profiles()


# --------------------------------------------------------------------------- #
# 2. Odoo connection (XML-RPC external API)
# --------------------------------------------------------------------------- #

class OdooError(Exception):
    """Raised for anything that should be returned as {success: false, error}."""


@lru_cache(maxsize=None)
def _authenticate(profile_name: str) -> tuple[Any, str, int, str]:
    """Authenticate against Odoo and cache (models_proxy, db, uid, password).

    Cached per-profile so we don't re-authenticate on every tool call.
    """
    p = PROFILES.get(profile_name)
    if p is None:
        raise OdooError(
            f"Unknown profile '{profile_name}'. "
            f"Available: {', '.join(PROFILES) or '(none)'}."
        )

    url = p["url"].rstrip("/")
    db = p["database"]
    user = p["username"]
    key = p["password"]

    common = xmlrpc.client.ServerProxy(f"{url}/xmlrpc/2/common", allow_none=True)
    try:
        uid = common.authenticate(db, user, key, {})
    except Exception as exc:  # network / xmlrpc faults
        raise OdooError(f"Could not reach Odoo at {url}: {exc}") from exc

    if not uid:
        raise OdooError(
            f"Authentication failed for user '{user}' on database '{db}'. "
            "Check username / password (or API key) / database name."
        )

    models = xmlrpc.client.ServerProxy(f"{url}/xmlrpc/2/object", allow_none=True)
    return models, db, uid, key


def _client(profile: str | None) -> tuple[Any, str, int, str]:
    return _authenticate(profile or DEFAULT_PROFILE)


def _is_readonly(profile: str | None) -> bool:
    return bool(PROFILES.get(profile or DEFAULT_PROFILE, {}).get("readonly"))


def _kw(profile: str | None, model: str, method: str,
        args: list | None = None, kwargs: dict | None = None) -> Any:
    """The single choke point: everything routes through execute_kw."""
    models, db, uid, key = _client(profile)
    try:
        return models.execute_kw(db, uid, key, model, method, args or [], kwargs or {})
    except xmlrpc.client.Fault as fault:
        # Odoo packs the useful message into faultString.
        raise OdooError(_clean_fault(fault.faultString)) from fault
    except Exception as exc:
        raise OdooError(str(exc)) from exc


def _clean_fault(msg: str) -> str:
    """Trim Odoo's giant traceback down to the last, most relevant line."""
    lines = [ln for ln in msg.strip().splitlines() if ln.strip()]
    return lines[-1] if lines else msg


# --------------------------------------------------------------------------- #
# 3. Helpers: domain parsing, envelope, formatting
# --------------------------------------------------------------------------- #

def _parse_domain(domain: Any) -> list:
    """Accept a JSON/Python-literal string or an already-parsed list.

    Domains use Odoo prefix (Polish) notation, e.g.
        ["&", ["state", "=", "posted"], ["move_type", "=", "out_invoice"]]
    """
    if domain is None or domain == "":
        return []
    if isinstance(domain, list):
        return domain
    if isinstance(domain, str):
        s = domain.strip()
        try:
            return json.loads(s)
        except json.JSONDecodeError:
            try:
                return ast.literal_eval(s)
            except (ValueError, SyntaxError) as exc:
                raise OdooError(
                    f"Could not parse domain: {domain!r}. "
                    'Use prefix notation, e.g. ["&", ["a","=",1], ["b","=",2]]. '
                    "Do NOT use the words 'and'/'or' — use '&' / '|'."
                ) from exc
    raise OdooError(f"Unsupported domain type: {type(domain).__name__}")


def _parse_ids(ids: Any) -> list[int]:
    """Accept [1,2,3], '1,2,3', or a single int."""
    if isinstance(ids, int):
        return [ids]
    if isinstance(ids, list):
        return [int(i) for i in ids]
    if isinstance(ids, str):
        s = ids.strip()
        if s.startswith("["):
            return [int(i) for i in json.loads(s)]
        return [int(part) for part in s.split(",") if part.strip()]
    raise OdooError(f"Could not parse ids: {ids!r} — use [1,2,3] or '1,2,3'.")


def _parse_values(values: Any) -> dict:
    if isinstance(values, dict):
        return values
    if isinstance(values, str):
        try:
            return json.loads(values)
        except json.JSONDecodeError as exc:
            raise OdooError(f"`values` must be a JSON object: {exc}") from exc
    raise OdooError("`values` must be a JSON object.")


def _split_fields(fields: Any) -> list[str]:
    if not fields:
        return []
    if isinstance(fields, list):
        return [str(f).strip() for f in fields if str(f).strip()]
    return [f.strip() for f in str(fields).split(",") if f.strip()]


def _envelope(records: list, total: int, limit: int, offset: int, fmt: str) -> dict:
    """The pagination 'envelope' wrapped around every read."""
    has_more = (offset + len(records)) < total
    return {
        "records": records,
        "total": total,
        "limit": limit,
        "offset": offset,
        "has_more": has_more,
        "next_offset": offset + limit if has_more else None,
        "format": fmt,
    }


# ---- output formatting ---------------------------------------------------- #

def _stringify(value: Any, truncate: int | None = None) -> str:
    if value is None or value is False:
        return ""
    if isinstance(value, (list, tuple)):
        # Relational many2one arrives as [id, "Name"] -> show the name.
        if len(value) == 2 and isinstance(value[0], int):
            s = str(value[1])
        else:
            s = json.dumps(value, ensure_ascii=False)
    elif isinstance(value, dict):
        s = json.dumps(value, ensure_ascii=False)
    else:
        s = str(value)
    if truncate and len(s) > truncate:
        s = s[: truncate - 1] + "…"
    return s


def _columns(records: list[dict], fields: list[str]) -> list[str]:
    if fields:
        return fields
    cols: list[str] = []
    for r in records:
        for k in r:
            if k not in cols:
                cols.append(k)
    return cols


def _format_records(records: list[dict], fields: list[str], fmt: str) -> Any:
    """Serialize records to one of: json | compact | table | html | csv."""
    fmt = (fmt or "json").lower()
    cols = _columns(records, fields)

    if fmt == "json":
        return records

    if fmt == "compact":
        # Array-of-arrays: a header row + one row per record (~60% lighter).
        return {"columns": cols, "rows": [[r.get(c) for c in cols] for r in records]}

    if fmt == "table":
        if not records:
            return "_(no records)_"
        header = "| " + " | ".join(cols) + " |"
        sep = "| " + " | ".join(["---"] * len(cols)) + " |"
        body = [
            "| " + " | ".join(_stringify(r.get(c), truncate=50) for c in cols) + " |"
            for r in records
        ]
        return "\n".join([header, sep, *body])

    if fmt == "html":
        head = "".join(f"<th>{escape(str(c))}</th>" for c in cols)
        rows = ""
        for r in records:
            cells = "".join(f"<td>{escape(_stringify(r.get(c)))}</td>" for c in cols)
            rows += f"<tr>{cells}</tr>"
        return f"<table border='1'><thead><tr>{head}</tr></thead><tbody>{rows}</tbody></table>"

    if fmt == "csv":
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(cols)
        for r in records:
            writer.writerow([_stringify(r.get(c)) for c in cols])
        return buf.getvalue()

    raise OdooError(f"Unknown format '{fmt}'. Use json|compact|table|html|csv.")


def _ok(data: dict) -> dict:
    out = {"success": True}
    out.update(data)
    return out


def _guard_write(profile: str | None) -> None:
    if _is_readonly(profile):
        raise OdooError(
            f"Profile '{profile or DEFAULT_PROFILE}' is marked read-only. "
            "Refusing to mutate data. Remove `\"readonly\": true` from the "
            "profile to allow writes."
        )


# A small decorator so every tool returns {success:false, error:...} uniformly.
# @wraps copies __wrapped__ so FastMCP's inspect.signature sees the real params.
def _tool(fn):
    @wraps(fn)
    def wrapper(*a, **kw):
        try:
            return fn(*a, **kw)
        except OdooError as exc:
            return {"success": False, "error": str(exc)}
        except Exception as exc:  # last-resort safety net
            return {"success": False, "error": f"{type(exc).__name__}: {exc}"}
    return wrapper


# =========================================================================== #
#  GROUP A — Discovery / introspection
# =========================================================================== #

@mcp.tool()
@_tool
def list_available_profiles() -> dict:
    """List the Odoo instances configured on the host machine.

    Always call this FIRST so you know which environment you are pointing at.
    Returns an array of {name, url, database, is_default, readonly}.
    """
    rows = [
        {
            "name": name,
            "url": p.get("url"),
            "database": p.get("database"),
            "is_default": name == DEFAULT_PROFILE,
            "readonly": bool(p.get("readonly")),
        }
        for name, p in PROFILES.items()
    ]
    return _ok({"profiles": rows, "default": DEFAULT_PROFILE})


@mcp.tool()
@_tool
def get_version(profile: str | None = None) -> dict:
    """Return the Odoo server version (e.g. '16.0+e') for a profile.

    Useful to decide method/field compatibility per version.
    """
    models, db, uid, key = _client(profile)
    url = PROFILES[profile or DEFAULT_PROFILE]["url"].rstrip("/")
    common = xmlrpc.client.ServerProxy(f"{url}/xmlrpc/2/common", allow_none=True)
    info = common.version()
    return _ok({"profile": profile or DEFAULT_PROFILE, "version": info})


@mcp.tool()
@_tool
def list_models(search: str = "", format: str = "json", profile: str | None = None) -> dict:
    """List the models (tables) available in the instance.

    `search` filters by model technical name or label (e.g. 'advance', 'account').
    """
    domain = []
    if search:
        domain = ["|", ["model", "ilike", search], ["name", "ilike", search]]
    records = _kw(profile, "ir.model", "search_read", [domain],
                  {"fields": ["name", "model", "info"], "order": "model"})
    formatted = _format_records(records, ["name", "model", "info"], format)
    return _ok({"models": formatted, "count": len(records), "format": format})


@mcp.tool()
@_tool
def list_fields(model: str, attributes: str = "string,type,help,relation,required",
                format: str = "json", profile: str | None = None) -> dict:
    """List the fields (schema) of a model.

    `attributes` is a comma-separated list of field metadata to return, e.g.
    'string,type,help,relation,required,readonly,selection'.
    """
    attrs = _split_fields(attributes)
    fields_def = _kw(profile, model, "fields_get", [], {"attributes": attrs})
    # fields_get returns {field_name: {attr: value}} — flatten to rows.
    rows = []
    for fname, meta in fields_def.items():
        row = {"field": fname}
        row.update({a: meta.get(a) for a in attrs})
        rows.append(row)
    rows.sort(key=lambda r: r["field"])
    cols = ["field"] + attrs
    formatted = _format_records(rows, cols, format)
    return _ok({"model": model, "fields": formatted, "count": len(rows), "format": format})


# =========================================================================== #
#  GROUP B — Reading
# =========================================================================== #

@mcp.tool()
@_tool
def search_read(model: str, domain: Any = "[]", fields: str = "",
                limit: int = 100, offset: int = 0, order: str = "",
                format: str = "json", profile: str | None = None) -> dict:
    """⭐ Search and read records. The most-used tool.

    - `domain`: prefix-notation filter, e.g. ["&", ["state","=","posted"],
      ["move_type","=","out_invoice"]]. Default [] = all. Never use 'and'/'or'.
    - `fields`: comma-separated, e.g. "name,amount_total,partner_id".
    - `limit` (default 100), `offset` (default 0), `order` (e.g. "id desc").
    - `format`: json | compact | table | html | csv.

    Returns a pagination envelope: {records, total, limit, offset, has_more,
    next_offset, format}.  If has_more is true, repeat with offset = next_offset.
    """
    dom = _parse_domain(domain)
    flds = _split_fields(fields)
    kwargs: dict = {"limit": limit, "offset": offset}
    if flds:
        kwargs["fields"] = flds
    if order:
        kwargs["order"] = order

    records = _kw(profile, model, "search_read", [dom], kwargs)
    total = _kw(profile, model, "search_count", [dom])
    formatted = _format_records(records, flds, format)
    env = _envelope(formatted, total, limit, offset, format)
    return _ok(env)


@mcp.tool()
@_tool
def read_group(model: str, domain: Any = "[]", fields: str = "", groupby: str = "",
               order: str = "", limit: int = 0, offset: int = 0, lazy: bool = True,
               format: str = "json", profile: str | None = None) -> dict:
    """Aggregate records SQL-style (GROUP BY) without pulling every row.

    The single most useful tool for reporting: sums / counts / averages grouped
    by one or more fields, computed by Odoo's database — cheap and fast.

    - `domain`: prefix-notation filter (same rules as search_read).
    - `fields`: measures to aggregate. Use "field:agg" to pick the function,
      e.g. "amount_total:sum,id:count". Bare "amount_total" defaults to sum.
      A group's row always includes "<groupby>_count" (rows in that group).
    - `groupby`: comma-separated dimensions, e.g. "state,move_type". For date
      fields you may granularize: "invoice_date:month".
    - `order`, `limit`, `offset`, `lazy` map to Odoo's read_group kwargs.
      lazy=False fully expands multi-level groupings in one call.

    Example — total invoiced per state:
      model=account.move  domain=[["move_type","=","out_invoice"]]
      fields="amount_total:sum"  groupby="state"
    """
    dom = _parse_domain(domain)
    measures = _split_fields(fields)
    groups = _split_fields(groupby)
    if not groups:
        raise OdooError('read_group needs `groupby`, e.g. groupby="state".')

    kwargs: dict = {"lazy": lazy}
    if order:
        kwargs["orderby"] = order
    if limit:
        kwargs["limit"] = limit
    if offset:
        kwargs["offset"] = offset

    rows = _kw(profile, model, "read_group", [dom, measures, groups], kwargs)
    # Columns: the grouped dimensions, their *_count, and the measures.
    cols: list[str] = []
    for g in groups:
        base = g.split(":", 1)[0]
        if base not in cols:
            cols.append(base)
    for r in rows:
        for k in r:
            if k != "__domain" and k not in cols:
                cols.append(k)
    # Drop Odoo's internal __domain / __context noise from the output rows.
    clean = [{k: v for k, v in r.items() if not k.startswith("__")} for r in rows]
    formatted = _format_records(clean, cols, format)
    return _ok({"model": model, "groups": formatted, "count": len(clean),
                "format": format})


@mcp.tool()
@_tool
def get_financial_report(report_id_or_name: str, date_from: str = "", date_to: str = "",
                         date_filter: str = "", company_ids: Any = None,
                         format: str = "json", profile: str | None = None) -> dict:
    """Compute a native accounting report (Balance Sheet, P&L, …).

    ⚠️ Designed for Odoo 17/18/19+. On Odoo < 17 this returns a validation
    error (the report engine differs). `date_filter` accepts today / this_month
    / this_year / last_month, etc.
    """
    ver = _kw(profile, "ir.module.module", "search_read",
              [[["name", "=", "base"], ["state", "=", "installed"]]],
              {"fields": ["latest_version"], "limit": 1})
    series = (ver[0]["latest_version"] if ver else "0").split(".")[0]
    try:
        major = int(series)
    except ValueError:
        major = 0
    if major and major < 17:
        raise OdooError(
            f"get_financial_report requires Odoo 17+ (this instance looks like "
            f"major version {major}). Use search_read on account.move.line / "
            "account.account with a read_group aggregation instead."
        )

    # Resolve the report (accepts numeric id, XML id, or exact display name).
    report_id = _resolve_report(profile, report_id_or_name)
    options: dict = {}
    if date_from:
        options["date"] = {"date_from": date_from, "date_to": date_to,
                           "filter": date_filter or "custom"}
    elif date_filter:
        options["date"] = {"filter": date_filter}
    if company_ids:
        options["companies"] = _parse_ids(company_ids)

    # Odoo 17+ account.report engine.
    lines = _kw(profile, "account.report", "get_report_information",
                [report_id, options])
    return _ok({"report_id": report_id, "options": options, "report": lines,
                "format": format})


def _resolve_report(profile: str | None, ref: str) -> int:
    if isinstance(ref, int) or (isinstance(ref, str) and ref.isdigit()):
        return int(ref)
    if isinstance(ref, str) and "." in ref and " " not in ref:
        # Looks like an XML id (module.name).
        rec = _kw(profile, "ir.model.data", "search_read",
                  [[["model", "=", "account.report"],
                    ["module", "=", ref.split(".", 1)[0]],
                    ["name", "=", ref.split(".", 1)[1]]]],
                  {"fields": ["res_id"], "limit": 1})
        if rec:
            return rec[0]["res_id"]
    found = _kw(profile, "account.report", "search_read",
                [[["name", "=", ref]]], {"fields": ["id"], "limit": 1})
    if found:
        return found[0]["id"]
    raise OdooError(f"Could not resolve report '{ref}' (tried id / XML id / name).")


# =========================================================================== #
#  GROUP C — Writing (mutations)
# =========================================================================== #

@mcp.tool()
@_tool
def create(model: str, values: Any, profile: str | None = None) -> dict:
    """Create a record. Returns {success, id}.

    Relational fields use Odoo command tuples:
      - one2many / new line:  [[0, 0, {"field": value}]]
      - many2many / set ids:  [[6, 0, [id1, id2]]]

    Example (invoice with one line):
      {"move_type":"out_invoice","partner_id":1126,"journal_id":1,
       "invoice_line_ids":[[0,0,{"product_id":20579,"quantity":1,
                                 "price_unit":1000,"tax_ids":[[6,0,[2]]]}]]}
    """
    _guard_write(profile)
    vals = _parse_values(values)
    new_id = _kw(profile, model, "create", [vals])
    return _ok({"id": new_id})


@mcp.tool()
@_tool
def write(model: str, ids: Any, values: Any, profile: str | None = None) -> dict:
    """Update existing records. Returns {success, written_ids}.

    Example: model=product.product ids=[20579]
             values={"property_account_income_id": 32}
    """
    _guard_write(profile)
    id_list = _parse_ids(ids)
    vals = _parse_values(values)
    _kw(profile, model, "write", [id_list, vals])
    return _ok({"written_ids": id_list})


@mcp.tool()
@_tool
def unlink(model: str, ids: Any, profile: str | None = None) -> dict:
    """Delete records. Returns {success, deleted_ids}.

    ⚠️ Many models block deletion (posted journal entries, records with
    dependencies). Typically you must first button_draft / action_cancel via
    execute_kw, then unlink.
    """
    _guard_write(profile)
    id_list = _parse_ids(ids)
    _kw(profile, model, "unlink", [id_list])
    return _ok({"deleted_ids": id_list})


@mcp.tool()
@_tool
def execute_kw(model: str, method: str, args: Any = None, kwargs: Any = None,
               profile: str | None = None) -> dict:
    """⭐ The swiss-army knife: execute ANY method of a model.

    - `args`: positional array, e.g. [[490749]] (a list of ids).
    - `kwargs`: object, e.g. {"context": {"active_ids": [490752]}}.

    Anything a user can do with a button in Odoo is a method call here:
      account.move / action_post / [[490749]]        -> validate invoice
      account.move / button_draft / [[490754]]       -> reset to draft
      account.payment.register / action_create_payments / [[3830]]
      account.move / action_process_edi_web_services / [[490749]]  -> stamp CFDI
    """
    # Heuristic: read-only introspection methods are always allowed; obvious
    # mutating verbs are blocked on read-only profiles.
    readonly_methods = {"search", "search_read", "read", "search_count",
                        "fields_get", "name_get", "default_get", "read_group",
                        "name_search", "check_access_rights"}
    if method not in readonly_methods:
        _guard_write(profile)

    parsed_args = args if isinstance(args, list) else (
        json.loads(args) if isinstance(args, str) and args.strip() else [])
    parsed_kwargs = kwargs if isinstance(kwargs, dict) else (
        json.loads(kwargs) if isinstance(kwargs, str) and kwargs.strip() else {})

    result = _kw(profile, model, method, parsed_args, parsed_kwargs)
    return _ok({"result": result})


# =========================================================================== #
#  GROUP D — Bulk operations (ETL)
# =========================================================================== #

@mcp.tool()
@_tool
def export_records(model: str, domain: Any = "[]", fields: str = "",
                   limit: int = 500, offset: int = 0,
                   format: str = "json", profile: str | None = None) -> dict:
    """Export records with Odoo's native export_data (ideal for backup/migration).

    Trick: request the `id` field to get the stable External ID (XML ID),
    re-importable via import_records. For relations use export syntax like
    "country_id/id".
    """
    dom = _parse_domain(domain)
    flds = _split_fields(fields)
    if not flds:
        raise OdooError("export_records needs `fields`, e.g. \"id,name,country_id/id\".")

    ids = _kw(profile, model, "search", [dom], {"limit": limit, "offset": offset})
    total = _kw(profile, model, "search_count", [dom])
    if not ids:
        return _ok(_envelope([], total, limit, offset, format))

    export = _kw(profile, model, "export_data", [ids, flds])
    data_rows = export.get("datas", [])
    # Turn parallel arrays into list-of-dicts keyed by requested field.
    records = [dict(zip(flds, row)) for row in data_rows]
    formatted = _format_records(records, flds, format)
    return _ok(_envelope(formatted, total, limit, offset, format))


@mcp.tool()
@_tool
def import_records(model: str, fields: str, rows: Any, profile: str | None = None) -> dict:
    """Bulk import/update with Odoo's native `load` (upsert).

    Behaviour: if `id` (External ID) exists -> UPDATE; otherwise -> CREATE.
    Typical flow: export_records -> edit CSV -> import_records.

    `rows` may be a list of objects (same shape export_records returns) or a
    list of arrays already aligned to `fields`.
    """
    _guard_write(profile)
    flds = _split_fields(fields)
    if not flds:
        raise OdooError("import_records needs `fields` aligned to the row columns.")

    parsed_rows = rows
    if isinstance(rows, str):
        parsed_rows = json.loads(rows)
    if not isinstance(parsed_rows, list):
        raise OdooError("`rows` must be a list.")

    matrix: list[list] = []
    for r in parsed_rows:
        if isinstance(r, dict):
            matrix.append([_cell(r.get(f)) for f in flds])
        elif isinstance(r, (list, tuple)):
            matrix.append([_cell(v) for v in r])
        else:
            raise OdooError(f"Each row must be an object or array, got {type(r).__name__}.")

    result = _kw(profile, model, "load", [flds, matrix])
    # Odoo's load returns {"ids": [...], "messages": [...]}.
    messages = result.get("messages", [])
    if messages:
        return {"success": False, "error": "Import reported errors.",
                "messages": messages, "ids": result.get("ids")}
    return _ok({"ids": result.get("ids", []), "count": len(matrix)})


def _cell(value: Any) -> str:
    """Odoo's `load` expects strings; normalize Nones/bools/relations."""
    if value is None or value is False:
        return ""
    if isinstance(value, (list, tuple)) and len(value) == 2 and isinstance(value[0], int):
        return str(value[1])
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


# --------------------------------------------------------------------------- #
# Entrypoint
# --------------------------------------------------------------------------- #

def main() -> None:
    """Console-script entry point (see pyproject.toml [project.scripts]).

    Lets the server be launched as `odoo-mcp` after install, including via
    `uvx --from git+https://github.com/AleemHaider/odoo-mcp odoo-mcp`.
    """
    # stdio transport — how Claude Desktop / Claude Code launch a local server.
    print(
        f"[Odoo MCP Multi] profiles: {', '.join(PROFILES)} "
        f"| default: {DEFAULT_PROFILE}",
        file=sys.stderr,
    )
    mcp.run()


if __name__ == "__main__":
    main()
