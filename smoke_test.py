#!/usr/bin/env python3
"""
Smoke test for the Odoo MCP server — verifies it works end-to-end against a
REAL Odoo instance without needing an MCP client wired up.

Usage:
    1. cp profiles.example.json profiles.json  (fill in real credentials)
    2. python smoke_test.py            # tests the default profile
       python smoke_test.py MyProfile  # tests a named profile

It only READS (never writes), so it is safe to run against any instance.
"""

import importlib.util
import os
import sys


def load_server():
    here = os.path.dirname(os.path.abspath(__file__))
    spec = importlib.util.spec_from_file_location("srv", os.path.join(here, "server.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def check(label, result):
    ok = isinstance(result, dict) and result.get("success")
    mark = "PASS" if ok else "FAIL"
    print(f"[{mark}] {label}")
    if not ok:
        print(f"       -> {result.get('error') if isinstance(result, dict) else result}")
    return ok


def main():
    profile = sys.argv[1] if len(sys.argv) > 1 else None
    m = load_server()

    print("=" * 60)
    print("Odoo MCP smoke test (read-only)")
    print("=" * 60)

    passed = True

    # 1. Config
    passed &= check("list_available_profiles", m.list_available_profiles())

    # 2. Connectivity + auth
    passed &= check("get_version", m.get_version(profile=profile))

    # 3. Read a couple of partners (proves search_read + envelope)
    r = m.search_read("res.partner", domain="[]", fields="id,name",
                      limit=3, profile=profile)
    passed &= check("search_read res.partner (limit 3)", r)
    if isinstance(r, dict) and r.get("success"):
        print(f"       total={r.get('total')} has_more={r.get('has_more')} "
              f"records={len(r.get('records', []))}")

    # 4. Introspection through execute_kw (read-only method)
    passed &= check(
        "execute_kw res.partner.fields_get",
        m.execute_kw("res.partner", "fields_get", args=[["name"]],
                     kwargs={"attributes": ["string", "type"]}, profile=profile),
    )

    print("=" * 60)
    print("RESULT:", "ALL PASSED — server is live-verified ✅" if passed
          else "SOME CHECKS FAILED — see errors above ❌")
    print("=" * 60)
    sys.exit(0 if passed else 1)


if __name__ == "__main__":
    main()
