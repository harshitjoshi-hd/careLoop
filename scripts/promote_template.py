"""Promote a new system message for one of CareLoop's sphere templates.

    SPHERE_APP_TOKEN=... python scripts/promote_template.py \\
        --use-case funnel-hypothesis-generation \\
        --system-message-file prompts/funnel-hypothesis-generation.system.md [--apply]

Dry-run by default: prints the diff against what is live and exits. With
--apply it PATCHes (which creates an unpromoted snapshot) and then promotes the
newest version. Idempotent: if the file's text is already live, does nothing.
Prompts live in prompts/ so a template change is reviewable in git.
"""
import argparse
import difflib
import json
import os
import sys
import urllib.request
from pathlib import Path

from app.integrations.sphere import _base_url  # noqa: E402  (same host rule as the app)
BASE = f"{_base_url()}/v2/projects/7121/use-cases"
IDS = json.loads(Path("fixtures/pd_checkout/sphere_ids.json").read_text())
TOKEN = os.environ.get("SPHERE_APP_TOKEN", "")
HEADERS = {"X-APP-TOKEN": TOKEN, "Content-Type": "application/json"}


def call(url, method="GET", payload=None):
    req = urllib.request.Request(url, method=method, headers=HEADERS,
                                 data=json.dumps(payload).encode() if payload else None)
    with urllib.request.urlopen(req, timeout=45) as resp:
        return json.loads(resp.read())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--use-case", required=True)
    ap.add_argument("--system-message-file", required=True)
    ap.add_argument("--output-schema-file", help="optional JSON schema to set alongside the system message")
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()
    if not TOKEN:
        print("SPHERE_APP_TOKEN is not set"); return 1

    uc = next(u for u in IDS["use_cases"] if u["name"] == a.use_case)
    base = f"{BASE}/{uc['use_case_id']}/prompt-templates/{uc['template_id']}"
    new = Path(a.system_message_file).read_text().strip()

    cur = call(base)
    live = (cur["system_message"] or "").strip()
    new_schema = json.loads(Path(a.output_schema_file).read_text()) if a.output_schema_file else None
    schema_changed = new_schema is not None and new_schema != cur.get("output_schema")
    print(f"{a.use_case}: template {uc['template_id']} is at v{cur['version']} (active={cur['is_active']})")
    if live == new and not schema_changed:
        print("that text (and schema) is already live — nothing to do."); return 0
    if schema_changed:
        print("output_schema changes too:")
        print("\n".join(difflib.unified_diff(json.dumps(cur.get("output_schema"), indent=1).splitlines(),
                                             json.dumps(new_schema, indent=1).splitlines(),
                                             fromfile="live", tofile=a.output_schema_file, lineterm="")))

    print("\n".join(difflib.unified_diff(live.splitlines(), new.splitlines(),
                                         fromfile=f"live v{cur['version']}", tofile=a.system_message_file,
                                         lineterm="")))
    if not a.apply:
        print("\nDry run. Re-run with --apply to patch and promote."); return 0

    print("\nPATCH (creates a snapshot; does NOT activate it)...")
    patch = {"system_message": new}
    if schema_changed:
        patch["output_schema"] = new_schema
    call(base, "PATCH", patch)
    versions = call(f"{base}/versions")
    rows = versions["result"] if isinstance(versions, dict) else versions   # {"result": [...]}
    latest = max(int(v["version"]) for v in rows)
    out = call(f"{base}/promote", "PATCH", {"version_id": latest})
    print(f"promoted: v{out.get('version')} active={out.get('is_active')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
