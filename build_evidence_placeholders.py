#!/usr/bin/env python3.11
"""
build_evidence_placeholders.py

Creates Drata evidence library placeholders from the Baker Tilly artifact list CSV.

Usage:
    python3.11 build_evidence_placeholders.py --input <csv_path>
    python3.11 build_evidence_placeholders.py --input <csv_path> --live
    python3.11 build_evidence_placeholders.py --input <csv_path> --live --map-suffix MAI=mindbody
"""

import argparse
import csv
import getpass
import os
import re
import sys
from datetime import datetime

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ─── Config ───────────────────────────────────────────────────────────────────

API_BASE_URL = "https://public-api.drata.com/public/v2"
FRAMEWORK_TAG = "PCI4"
API_KEY = ""  # override via DRATA_API_KEY env var or interactive prompt

GLOBAL_CATEGORIES = {"General", "Service Providers", "Multi-tenant Service Provider"}
SUFFIX_TO_WORKSPACE_KEY = {"B": "booker", "C": "classpass", "M": "mindbody"}
UNKNOWN_SUFFIXES = {"A", "FMX", "MAI"}
BU_PREFIX_LABEL = {
    "main": "GLOBAL",
    "mindbody": "MINDBODY",
    "classpass": "CLASSPASS",
    "booker": "BOOKER",
}
NAME_MAX = 191

# ─── Session Setup ────────────────────────────────────────────────────────────

def build_session(api_key: str) -> requests.Session:
    retry = Retry(
        total=5,
        backoff_factor=2,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"],
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session = requests.Session()
    session.mount("https://", adapter)
    session.headers.update({"Authorization": f"Bearer {api_key}"})
    return session

# ─── API Helpers ──────────────────────────────────────────────────────────────

def get_workspaces(session: requests.Session) -> list[dict]:
    return paginate(session, f"{API_BASE_URL}/workspaces")


def paginate(session: requests.Session, url: str, params: dict | None = None) -> list[dict]:
    """Cursor-based paginator. Returns all items across all pages."""
    params = dict(params or {})
    params.setdefault("size", 50)
    items = []
    while True:
        resp = session.get(url, params=params)
        resp.raise_for_status()
        body = resp.json()
        items.extend(body.get("data", []))
        cursor = body.get("pagination", {}).get("cursor")
        if not cursor:
            break
        params["cursor"] = cursor
    return items


def build_pci_to_control_ids(session: requests.Session, workspace_id: int) -> dict[str, list[int]]:
    """Build PCI req code → list of non-archived DCF control IDs (built at startup)."""
    print("  Building PCI → DCF control ID lookup (~52 API calls)...")
    url = f"{API_BASE_URL}/workspaces/{workspace_id}/framework-requirements"
    params = {
        "frameworkTag": FRAMEWORK_TAG,
        "isInScope": "true",
        "expand[]": "controls",
    }
    reqs = paginate(session, url, params)
    lookup: dict[str, list[int]] = {}
    for req in reqs:
        pci_code = req.get("name", "").strip()
        if not pci_code:
            continue
        control_ids = [
            c["id"]
            for c in req.get("controls", {}).get("data", [])
            if c.get("archivedAt") is None
        ]
        if control_ids:
            existing = lookup.get(pci_code, [])
            lookup[pci_code] = list(dict.fromkeys(existing + control_ids))
    print(f"  Loaded {len(lookup)} PCI codes with linked controls.")
    return lookup


def fetch_existing_evidence(session: requests.Session, workspace_id: int) -> dict[str, dict]:
    """Pre-fetch all existing evidence items. Returns name → {id, control_ids}."""
    url = f"{API_BASE_URL}/workspaces/{workspace_id}/evidence-library"
    items = paginate(session, url, {"expand[]": "controls"})
    result: dict[str, dict] = {}
    for item in items:
        name = item.get("name")
        if not name:
            continue
        ctrl_ids = [c["id"] for c in item.get("controls", []) if c.get("id")]
        result[name] = {"id": item["id"], "control_ids": ctrl_ids}
    return result

# ─── Parsing ──────────────────────────────────────────────────────────────────

_TOKEN_RE = re.compile(r"^([A-Z]+-\d+)([A-Z]*)$")


def parse_request_id(col0: str) -> tuple[str, str, str] | None:
    """
    Returns (base_id, suffix, short_title) or None if malformed.
    col0 format: "{TOKEN} {SHORT_TITLE}"
    """
    col0 = col0.strip()
    if not col0:
        return None
    parts = col0.split(" ", 1)
    token = parts[0]
    short_title = parts[1].strip() if len(parts) > 1 else ""
    m = _TOKEN_RE.match(token)
    if not m:
        return None
    return m.group(1), m.group(2), short_title


def parse_pci_codes(col2: str) -> list[str]:
    return [c.strip() for c in col2.split() if c.strip()]

# ─── Name Construction ────────────────────────────────────────────────────────

def build_name(bu_prefix: str, base_id: str, short_title: str) -> str:
    prefix_part = f"[{bu_prefix}] {base_id} "
    budget = NAME_MAX - len(prefix_part)
    if budget <= 0:
        return prefix_part[:NAME_MAX]
    if len(short_title) > budget:
        take = max(0, budget - 3)
        short_title = short_title[:take] + "..."
    return (prefix_part + short_title).rstrip()

# ─── Routing ──────────────────────────────────────────────────────────────────

def get_routing(
    base_id: str,
    suffix: str,
    category: str,
    suffix_overrides: dict[str, str],
) -> list[tuple[str, str]] | None:
    """
    Returns list of (workspace_key, bu_prefix) pairs, or None to send to unrouted.
    workspace_key: "main" | "mindbody" | "classpass" | "booker"
    """
    # R3 — pre-split: known BU suffix (B/C/M)
    if suffix in SUFFIX_TO_WORKSPACE_KEY:
        wkey = SUFFIX_TO_WORKSPACE_KEY[suffix]
        return [(wkey, BU_PREFIX_LABEL[wkey])]

    # R4 — unknown suffix (A/FMX/MAI)
    if suffix in UNKNOWN_SUFFIXES:
        override = suffix_overrides.get(suffix)
        if override is None:
            return None
        return [(override, BU_PREFIX_LABEL[override])]

    # Any non-empty suffix not handled above is unrouted
    if suffix:
        return None

    # No suffix — R1 or R2
    # R1 — GLOBAL: org-wide categories or PA- prefix items
    if category in GLOBAL_CATEGORIES or base_id.startswith("PA-"):
        return [("main", "GLOBAL")]

    # R2 — per-BU: create 3× in each BU workspace
    return [
        ("mindbody", "MINDBODY"),
        ("classpass", "CLASSPASS"),
        ("booker", "BOOKER"),
    ]

# ─── Evidence Creation ────────────────────────────────────────────────────────

def create_evidence_item(
    session: requests.Session,
    workspace_id: int,
    name: str,
    description: str,
    control_ids: list[int],
    dry_run: bool,
    existing_evidence: dict[str, dict],
) -> tuple[str, int | None, str | None]:
    """
    Returns (action, http_status, error_message).
    action: "CREATE" | "SKIP" | "DRYRUN" | "ERROR"
    """
    if name in existing_evidence:
        return "SKIP", None, None

    if dry_run:
        return "DRYRUN", None, None

    body: dict = {"name": name}
    if description:
        body["description"] = description
    if control_ids:
        body["controlIds"] = control_ids

    try:
        resp = session.post(
            f"{API_BASE_URL}/workspaces/{workspace_id}/evidence-library",
            json=body,
        )
        if resp.status_code in (200, 201):
            data = resp.json()
            existing_evidence[name] = {"id": data.get("id"), "control_ids": control_ids}
            return "CREATE", resp.status_code, None
        try:
            err_msg = resp.json().get("message", resp.text[:200])
        except Exception:
            err_msg = resp.text[:200]
        return "ERROR", resp.status_code, err_msg
    except requests.RequestException as e:
        return "ERROR", None, str(e)

def update_evidence_item(
    session: requests.Session,
    workspace_id: int,
    evidence_id: int,
    name: str,
    control_ids: list[int],
    dry_run: bool,
    current_control_ids: list[int],
) -> tuple[str, int | None, str | None]:
    """
    Updates controlIds on an existing evidence item.
    Returns (action, http_status, error_message).
    action: "UPDATE" | "NOOP" | "DRYRUN" | "ERROR"
    Never clears controlIds — if control_ids is empty, returns NOOP.
    """
    if not control_ids:
        return "NOOP", None, None

    if sorted(current_control_ids) == sorted(control_ids):
        return "NOOP", None, None

    if dry_run:
        return "DRYRUN", None, None

    body: dict = {"name": name, "controlIds": control_ids}
    try:
        resp = session.put(
            f"{API_BASE_URL}/workspaces/{workspace_id}/evidence-library/{evidence_id}",
            json=body,
        )
        if resp.status_code in (200, 201):
            return "UPDATE", resp.status_code, None
        try:
            err_msg = resp.json().get("message", resp.text[:200])
        except Exception:
            err_msg = resp.text[:200]
        return "ERROR", resp.status_code, err_msg
    except requests.RequestException as e:
        return "ERROR", None, str(e)

# ─── Interactive Startup ──────────────────────────────────────────────────────

def print_banner(csv_path: str, live: bool) -> None:
    W = 72
    title = "Evidence Placeholder Builder  ·  Mindbody PCI DSS 4.0.1"
    mode_str = "LIVE — writes will be sent to Drata" if live else "DRY RUN — no writes"
    csv_name = os.path.basename(csv_path)
    if len(csv_name) > W - 12:
        csv_name = csv_name[:W - 15] + "..."

    def row(text: str = "", center: bool = False) -> str:
        inner = text.center(W) if center else f"  {text}".ljust(W)
        return f"║{inner}║"

    print()
    print("╔" + "═" * W + "╗")
    print(row(title, center=True))
    print("╠" + "═" * W + "╣")
    print(row(f"Input   {csv_name}"))
    print(row(f"Mode    {mode_str}"))
    print("╚" + "═" * W + "╝")


def resolve_api_key() -> str:
    key = os.environ.get("DRATA_API_KEY", "").strip()
    if not key:
        key = API_KEY.strip()
    if not key:
        key = getpass.getpass("Drata API key: ").strip()
        try:
            import termios
            termios.tcflush(sys.stdin, termios.TCIFLUSH)
        except Exception:
            pass
    if not key:
        print("ERROR: No API key provided.", file=sys.stderr)
        sys.exit(1)
    return key


def _pick_workspace(workspaces: list[dict], prompt: str) -> int:
    """Prompt user to select a workspace by number. Returns workspace ID."""
    while True:
        raw = input(f"  {prompt} (1-{len(workspaces)}): ").strip()
        try:
            idx = int(raw) - 1
            if 0 <= idx < len(workspaces):
                ws = workspaces[idx]
                print(f"    → {ws['name']}")
                return ws["id"]
            print(f"    Enter a number between 1 and {len(workspaces)}.")
        except ValueError:
            print("    Enter a number.")


def interactive_startup(
    session: requests.Session,
    csv_path: str,
    cli_suffix_map: dict[str, str],
    live: bool,
) -> tuple[dict[str, int], dict[str, str]]:
    """
    Guides the operator through workspace assignment.
    Returns (workspace_ids, suffix_routing).
    workspace_ids: role → workspace integer ID
    suffix_routing: unknown suffix → workspace_key (only entries explicitly set via --map-suffix)
    """
    print_banner(csv_path, live)

    # Step 2: connectivity + workspace list
    print("\nConnecting to Drata API...")
    try:
        workspaces = get_workspaces(session)
    except requests.RequestException as e:
        print(f"ERROR: Cannot connect to Drata API: {e}", file=sys.stderr)
        sys.exit(1)

    if not workspaces:
        print("ERROR: No workspaces returned.", file=sys.stderr)
        sys.exit(1)

    print(f"\nFound {len(workspaces)} workspace(s):")
    for i, ws in enumerate(workspaces, 1):
        primary_tag = "  [PRIMARY]" if ws.get("primary") else ""
        print(f"  [{i}] {ws['name']}{primary_tag}")

    # Step 3: workspace role assignment
    role_labels = {
        "main":      "Main PCI workspace (Baker Tilly audit hub — [GLOBAL] items go here)",
        "mindbody":  "Mindbody BU workspace",
        "classpass": "Classpass BU workspace",
        "booker":    "Booker BU workspace",
    }
    print()
    workspace_ids: dict[str, int] = {}
    for role, label in role_labels.items():
        workspace_ids[role] = _pick_workspace(workspaces, f"Select [{label}]")

    # Step 4: unknown suffix handling
    # Scan CSV to find which unknown suffixes are present
    unknown_found: dict[str, int] = {}
    try:
        with open(csv_path, newline="", encoding="utf-8-sig") as f:
            reader = csv.reader(f)
            next(reader, None)
            for row in reader:
                if not row or not row[0].strip():
                    continue
                parsed = parse_request_id(row[0])
                if parsed and parsed[1] in UNKNOWN_SUFFIXES:
                    s = parsed[1]
                    unknown_found[s] = unknown_found.get(s, 0) + 1
    except OSError as e:
        print(f"ERROR: Cannot read CSV for suffix scan: {e}", file=sys.stderr)
        sys.exit(1)

    suffix_routing: dict[str, str] = dict(cli_suffix_map)

    if unknown_found:
        print(f"\nUnrecognized BU suffixes (ignored by default — use --map-suffix to include):")
        for suffix in sorted(unknown_found):
            count = unknown_found[suffix]
            target = suffix_routing.get(suffix)
            if target:
                print(f"  {suffix} ({count} rows) → routed to {target}")
            else:
                print(f"  {suffix} ({count} rows) → skipped  (--map-suffix {suffix}=<workspace>)")

    # Step 5: confirm + mode
    mode_str = "LIVE (writes WILL be sent to Drata)" if live else "DRY RUN (no writes)"
    print(f"\nMode: {mode_str}")
    answer = input("Proceed? [y/N]: ").strip().lower()
    if answer != "y":
        print("Aborted.")
        sys.exit(0)

    return workspace_ids, suffix_routing

# ─── Main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Create Drata evidence placeholders from Baker Tilly artifact list CSV."
    )
    parser.add_argument("--input", required=True, metavar="CSV_PATH", dest="csv_path", help="Path to Baker Tilly artifact list CSV")
    parser.add_argument("--live", action="store_true", help="Execute writes (default: dry run)")
    parser.add_argument(
        "--update",
        action="store_true",
        help=(
            "Update existing evidence items to match the DCF control links from the CSV. "
            "Without this flag, existing items are skipped. "
            "Has no effect on items that do not yet exist (those are always created). "
            "Never clears controlIds — items with no PCI codes in the CSV are left unchanged."
        ),
    )
    parser.add_argument(
        "--map-suffix",
        action="append",
        default=[],
        metavar="SUFFIX=WORKSPACE",
        help=(
            "Route rows with an unrecognized BU suffix (A, FMX, MAI) to the specified workspace "
            "role (main/mindbody/classpass/booker). Without this flag those rows are skipped. "
            "Can be repeated. Example: --map-suffix MAI=mindbody --map-suffix FMX=booker"
        ),
    )
    args = parser.parse_args()

    # Parse --map-suffix flags
    cli_suffix_map: dict[str, str] = {}
    valid_targets = {"main", "mindbody", "classpass", "booker"}
    for mapping in args.map_suffix:
        if "=" not in mapping:
            print(f"ERROR: --map-suffix requires SUFFIX=workspace format (got: {mapping!r})", file=sys.stderr)
            sys.exit(1)
        suffix, wkey = mapping.split("=", 1)
        suffix = suffix.strip().upper()
        wkey = wkey.strip().lower()
        if not suffix:
            print(f"ERROR: --map-suffix suffix cannot be empty (got: {mapping!r})", file=sys.stderr)
            sys.exit(1)
        if suffix not in UNKNOWN_SUFFIXES:
            print(f"ERROR: --map-suffix suffix must be one of {sorted(UNKNOWN_SUFFIXES)} (got: {suffix!r})", file=sys.stderr)
            sys.exit(1)
        if wkey not in valid_targets:
            print(f"ERROR: workspace target must be one of {valid_targets} (got: {wkey!r})", file=sys.stderr)
            sys.exit(1)
        cli_suffix_map[suffix] = wkey

    # Step 1: API key
    api_key = resolve_api_key()
    session = build_session(api_key)

    # Interactive startup: workspace selection + unknown suffix handling
    workspace_ids, suffix_routing = interactive_startup(
        session, args.csv_path, cli_suffix_map, args.live
    )

    # Build DCF control ID lookup (uses main PCI workspace)
    print()
    try:
        pci_to_control_ids = build_pci_to_control_ids(session, workspace_ids["main"])
    except requests.RequestException as e:
        print(f"ERROR: Failed to build DCF control lookup: {e}", file=sys.stderr)
        sys.exit(1)

    # Pre-fetch existing evidence per workspace (idempotency + update support)
    print("\nPre-fetching existing evidence items...")
    existing_evidence: dict[str, dict[str, dict]] = {}
    for role, ws_id in workspace_ids.items():
        try:
            evidence = fetch_existing_evidence(session, ws_id)
            existing_evidence[role] = evidence
            print(f"  {role}: {len(evidence)} existing item(s)")
        except requests.RequestException as e:
            if args.live:
                print(f"ERROR: Cannot pre-fetch existing evidence for '{role}' (id: {ws_id}) — aborting --live run to prevent duplicates.", file=sys.stderr)
                sys.exit(1)
            print(f"  WARNING: Could not pre-fetch {role}: {e} — dry-run unaffected")
            existing_evidence[role] = {}

    # Validate CSV is readable before creating output files
    try:
        with open(args.csv_path, newline="", encoding="utf-8-sig"):
            pass
    except OSError as e:
        print(f"ERROR: Cannot read input CSV: {e}", file=sys.stderr)
        sys.exit(1)

    # Output files
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_log_path = f"run_log_{ts}.csv"
    unrouted_path = f"unrouted_{ts}.csv"

    run_log_fields = [
        "workspace_id", "bu_prefix", "base_id", "short_title",
        "full_name", "action", "http_status", "error_message",
    ]
    unrouted_fields = ["request_id", "suffix", "category", "description", "reason"]

    counts: dict[str, int] = {"CREATE": 0, "UPDATE": 0, "NOOP": 0, "SKIP": 0, "DRYRUN": 0, "ERROR": 0, "UNROUTED": 0, "MALFORMED": 0}

    print(f"\n{'─' * 70}")
    print(f"  {'ACTION':<10}  {'WORKSPACE':<12}  NAME")
    print(f"{'─' * 70}")

    with (
        open(run_log_path, "w", newline="", encoding="utf-8") as log_f,
        open(unrouted_path, "w", newline="", encoding="utf-8") as unrouted_f,
    ):
        log_writer = csv.DictWriter(log_f, fieldnames=run_log_fields)
        log_writer.writeheader()

        unrouted_writer = csv.DictWriter(unrouted_f, fieldnames=unrouted_fields)
        unrouted_writer.writeheader()

        try:
            csv_file = open(args.csv_path, newline="", encoding="utf-8-sig")
        except OSError as e:
            print(f"ERROR: Cannot open CSV: {e}", file=sys.stderr)
            sys.exit(1)

        with csv_file:
            reader = csv.reader(csv_file)
            next(reader, None)  # skip header

            for row_num, row in enumerate(reader, start=2):
                if not row or not row[0].strip():
                    continue

                # Pad short rows defensively
                while len(row) < 5:
                    row.append("")

                col0        = row[0].strip()
                description = row[1].strip()
                pci_raw     = row[2].strip()
                category    = row[3].strip()

                parsed = parse_request_id(col0)
                if parsed is None:
                    print(f"  {'MALFORMED':<10}  row {row_num}: {col0!r}")
                    counts["MALFORMED"] += 1
                    log_writer.writerow({
                        "workspace_id": "", "bu_prefix": "", "base_id": "",
                        "short_title": col0, "full_name": "", "action": "MALFORMED",
                        "http_status": "", "error_message": "Could not parse Request ID",
                    })
                    continue

                base_id, suffix, short_title = parsed

                # Resolve control IDs from PCI codes in col 2
                pci_codes = parse_pci_codes(pci_raw)
                raw_ids: list[int] = []
                for code in pci_codes:
                    raw_ids.extend(pci_to_control_ids.get(code, []))
                control_ids = list(dict.fromkeys(raw_ids))

                # Determine routing
                routing = get_routing(base_id, suffix, category, suffix_routing)

                if routing is None:
                    reason = f"unknown suffix '{suffix}'"
                    print(f"  {'UNROUTED':<10}  {col0}  (suffix={suffix!r})")
                    counts["UNROUTED"] += 1
                    unrouted_writer.writerow({
                        "request_id": col0,
                        "suffix": suffix,
                        "category": category,
                        "description": description[:500],
                        "reason": reason,
                    })
                    continue

                for workspace_key, bu_prefix in routing:
                    ws_id = workspace_ids[workspace_key]
                    name = build_name(bu_prefix, base_id, short_title)
                    ws_evidence = existing_evidence[workspace_key]
                    ev = ws_evidence.get(name)

                    if ev is not None:
                        if args.update:
                            action, http_status, error_msg = update_evidence_item(
                                session, ws_id, ev["id"], name, control_ids,
                                dry_run=not args.live,
                                current_control_ids=ev["control_ids"],
                            )
                        else:
                            action, http_status, error_msg = "SKIP", None, None
                    else:
                        action, http_status, error_msg = create_evidence_item(
                            session, ws_id, name, description, control_ids,
                            dry_run=not args.live,
                            existing_evidence=ws_evidence,
                        )
                    counts[action] += 1

                    status_str = str(http_status) if http_status else ""
                    print(f"  {action:<10}  [{workspace_key:<10}]  {name}")
                    if error_msg:
                        print(f"  {'':10}  ERROR: {error_msg}")

                    log_writer.writerow({
                        "workspace_id": ws_id,
                        "bu_prefix": bu_prefix,
                        "base_id": base_id,
                        "short_title": short_title,
                        "full_name": name,
                        "action": action,
                        "http_status": status_str,
                        "error_message": error_msg or "",
                    })

    # Summary
    mode_label = "LIVE" if args.live else "DRY RUN"
    print(f"\n{'─' * 70}")
    print(f"  Mode: {mode_label}")
    print(f"  CREATE:    {counts['CREATE']}")
    print(f"  UPDATE:    {counts['UPDATE']}  (existing items — control IDs changed)")
    print(f"  NOOP:      {counts['NOOP']}  (existing items — control IDs already correct or no PCI codes)")
    print(f"  SKIP:      {counts['SKIP']}  (existing items — --update not set)")
    print(f"  DRYRUN:    {counts['DRYRUN']}  (dry run — no writes)")
    print(f"  ERROR:     {counts['ERROR']}")
    print(f"  UNROUTED:  {counts['UNROUTED']}  → {unrouted_path}")
    print(f"  MALFORMED: {counts['MALFORMED']}")
    print(f"{'─' * 70}")
    print(f"  Run log:   {run_log_path}")
    if counts["UNROUTED"] > 0:
        print(f"  Unrouted:  {unrouted_path}")


if __name__ == "__main__":
    main()
