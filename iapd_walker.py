"""Walk IAPD's individual-search API to enumerate every IAR per SEC-registered firm.

Discovery: the IAPD frontend at api.adviserinfo.sec.gov accepts a firm-CRD filter
on its individual search endpoint. Per-record payload includes name, CRD, current
and previous employments, industry start date, BrokerCheck cross-registration, and
the regulatory-disclosure flag — all from one paginated call.

Constraints (verified May 2026):
- Page size cap: nrows <= 100
- Deep pagination cap: start <= 10,000 (ES max_result_window)
- For firms over 10k IARs, shard by `state=XX`
- Cloudflare WAF blocks non-browser User-Agents — we spoof Chrome
- ~1 req/sec is the safe rate; bursts trip intermittent 403s

Output: data/individuals.db (SQLite). Idempotent + resumable via walk_progress table.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import sqlite3
import sys
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import httpx

PROJECT_ROOT = Path(__file__).parent
ZIP_PATH = PROJECT_ROOT / "data" / "ia050126.zip"
DB_PATH = PROJECT_ROOT / "data" / "individuals.db"

ENDPOINT = "https://api.adviserinfo.sec.gov/search/individual"

# Cloudflare on api.adviserinfo.sec.gov rejects identifiable non-browser User-Agents.
BROWSER_HEADERS = {
    "accept": "application/json, text/plain, */*",
    "accept-language": "en-US,en;q=0.9",
    "origin": "https://adviserinfo.sec.gov",
    "referer": "https://adviserinfo.sec.gov/",
    "sec-ch-ua": '"Chromium";v="148", "Google Chrome";v="148", "Not/A)Brand";v="99"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"macOS"',
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "same-site",
    "user-agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"
    ),
}

PAGE_SIZE = 100
DEEP_PAGINATION_CAP = 10_000
DEFAULT_REQ_DELAY = 1.0

US_STATES = [
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA",
    "HI", "ID", "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD",
    "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ",
    "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI", "SC",
    "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY",
    "DC", "PR", "VI", "GU", "AS", "MP",
]


SCHEMA = """
CREATE TABLE IF NOT EXISTS individuals (
    ind_crd TEXT PRIMARY KEY,
    first_name TEXT,
    middle_name TEXT,
    last_name TEXT,
    other_names_json TEXT,
    ia_scope TEXT,
    bc_scope TEXT,
    disclosure_fl TEXT,
    finra_registration_count INTEGER,
    industry_cal_date_iapd TEXT,
    raw_source_json TEXT,
    observed_at TEXT,
    source TEXT
);

CREATE TABLE IF NOT EXISTS current_employments (
    ind_crd TEXT,
    firm_crd TEXT,
    firm_name TEXT,
    branch_city TEXT,
    branch_state TEXT,
    branch_zip TEXT,
    ia_only TEXT,
    observed_at TEXT,
    PRIMARY KEY (ind_crd, firm_crd, branch_city, branch_state)
);

CREATE INDEX IF NOT EXISTS idx_current_emp_firm ON current_employments(firm_crd);
CREATE INDEX IF NOT EXISTS idx_current_emp_state ON current_employments(branch_state);

CREATE TABLE IF NOT EXISTS previous_employments (
    ind_crd TEXT,
    firm_crd TEXT,
    firm_name TEXT,
    branch_city TEXT,
    branch_state TEXT,
    branch_zip TEXT,
    observed_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_prev_emp_ind ON previous_employments(ind_crd);

CREATE TABLE IF NOT EXISTS walk_progress (
    firm_crd TEXT,
    shard TEXT,
    total_records INTEGER,
    completed_at TEXT,
    PRIMARY KEY (firm_crd, shard)
);
"""


def db_connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA)
    return conn


def load_firm_crds(main_office_state: str | None = None) -> list[tuple[str, str]]:
    """Returns [(firm_crd, firm_name), ...] from the existing SEC bulk CSV.

    If main_office_state is given (2-letter code), only firms whose main office
    is in that state are returned.
    """
    if not ZIP_PATH.exists():
        sys.exit(f"missing {ZIP_PATH} — run the Streamlit app once to download the SEC bulk CSV")
    with zipfile.ZipFile(ZIP_PATH) as z:
        with z.open(z.namelist()[0]) as f:
            text = f.read().decode("latin-1")
    reader = csv.reader(io.StringIO(text))
    header = next(reader)
    crd_idx = header.index("Organization CRD#")
    name_idx = header.index("Primary Business Name")
    state_idx = header.index("Main Office State")
    out: list[tuple[str, str]] = []
    for row in reader:
        if crd_idx >= len(row) or not row[crd_idx]:
            continue
        if main_office_state and (state_idx >= len(row) or row[state_idx] != main_office_state):
            continue
        out.append((row[crd_idx], row[name_idx] if name_idx < len(row) else ""))
    return out


def fetch_page(
    client: httpx.Client,
    firm_crd: str,
    start: int,
    state: str | None,
) -> dict:
    # NOTE: do NOT pass includePrevious=true. It silently breaks the `state` filter
    # (the API returns the full firm roster regardless of state). We don't need previous
    # employments for the universe enumeration; fetch them per-individual later if needed.
    params: dict[str, str | int] = {
        "firm": firm_crd,
        "nrows": PAGE_SIZE,
        "start": start,
        "wt": "json",
    }
    if state:
        params["state"] = state
    for attempt in range(4):
        try:
            r = client.get(ENDPOINT, params=params, timeout=30)
            if r.status_code == 200 and r.content:
                return r.json()
            wait = 2 ** attempt
            print(
                f"   HTTP {r.status_code} ({len(r.content)}b) firm={firm_crd} "
                f"state={state} start={start} — backoff {wait}s",
                file=sys.stderr,
            )
            time.sleep(wait)
        except httpx.HTTPError as e:
            if attempt == 3:
                raise
            time.sleep(2 ** attempt)
            print(f"   {type(e).__name__}: {e} — retrying", file=sys.stderr)
    raise RuntimeError(f"exhausted retries firm={firm_crd} state={state} start={start}")


def upsert_individual(conn: sqlite3.Connection, src: dict, observed_at: str) -> None:
    ind_crd = src["ind_source_id"]
    conn.execute(
        """
        INSERT INTO individuals (
            ind_crd, first_name, middle_name, last_name, other_names_json,
            ia_scope, bc_scope, disclosure_fl, finra_registration_count,
            industry_cal_date_iapd, raw_source_json, observed_at, source
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(ind_crd) DO UPDATE SET
            first_name=excluded.first_name,
            middle_name=excluded.middle_name,
            last_name=excluded.last_name,
            other_names_json=excluded.other_names_json,
            ia_scope=excluded.ia_scope,
            bc_scope=excluded.bc_scope,
            disclosure_fl=excluded.disclosure_fl,
            finra_registration_count=excluded.finra_registration_count,
            industry_cal_date_iapd=excluded.industry_cal_date_iapd,
            raw_source_json=excluded.raw_source_json,
            observed_at=excluded.observed_at
        """,
        (
            ind_crd,
            src.get("ind_firstname", ""),
            src.get("ind_middlename", ""),
            src.get("ind_lastname", ""),
            json.dumps(src.get("ind_other_names") or []),
            src.get("ind_ia_scope", ""),
            src.get("ind_bc_scope", ""),
            src.get("ind_ia_disclosure_fl", ""),
            int(src.get("ind_approved_finra_registration_count") or 0),
            src.get("ind_industry_cal_date_iapd", ""),
            json.dumps(src),
            observed_at,
            "IAPD",
        ),
    )

    for emp in src.get("ind_ia_current_employments") or []:
        conn.execute(
            """
            INSERT OR REPLACE INTO current_employments (
                ind_crd, firm_crd, firm_name, branch_city, branch_state,
                branch_zip, ia_only, observed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                ind_crd,
                emp.get("firm_id", ""),
                emp.get("firm_name", ""),
                emp.get("branch_city", ""),
                emp.get("branch_state", ""),
                emp.get("branch_zip", ""),
                emp.get("ia_only", ""),
                observed_at,
            ),
        )

    conn.execute("DELETE FROM previous_employments WHERE ind_crd = ?", (ind_crd,))
    for emp in src.get("ind_previous_employments") or []:
        conn.execute(
            """
            INSERT INTO previous_employments (
                ind_crd, firm_crd, firm_name, branch_city, branch_state,
                branch_zip, observed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                ind_crd,
                emp.get("firm_id", ""),
                emp.get("firm_name", ""),
                emp.get("branch_city", ""),
                emp.get("branch_state", ""),
                emp.get("branch_zip", ""),
                observed_at,
            ),
        )


def walk_shard(
    client: httpx.Client,
    conn: sqlite3.Connection,
    firm_crd: str,
    state: str | None,
    observed_at: str,
    delay: float,
) -> tuple[int, bool]:
    """Walk one (firm) or (firm, state) slice. Returns (records_seen, hit_cap)."""
    start = 0
    total: int | None = None
    seen = 0
    while True:
        page = fetch_page(client, firm_crd, start, state)
        if page.get("errorMessage"):
            if "Exceeded limit" in page["errorMessage"]:
                return seen, True
            raise RuntimeError(f"API error: {page['errorMessage']}")
        hits = page.get("hits") or {}
        if total is None:
            total = hits.get("total", 0)
        rows = hits.get("hits") or []
        if not rows:
            break
        for hit in rows:
            upsert_individual(conn, hit["_source"], observed_at)
            seen += 1
        conn.commit()
        start += PAGE_SIZE
        if start >= total:
            return seen, False
        if start >= DEEP_PAGINATION_CAP:
            return seen, total > DEEP_PAGINATION_CAP
        time.sleep(delay)
    return seen, False


def walk_firm(
    client: httpx.Client,
    conn: sqlite3.Connection,
    firm_crd: str,
    firm_name: str,
    delay: float,
) -> tuple[int, bool]:
    """Walk one firm, sharding by state if it overflows the 10k cap.
    Returns (records_seen, was_sharded)."""
    observed_at = datetime.now(timezone.utc).isoformat()

    existing = conn.execute(
        "SELECT total_records FROM walk_progress WHERE firm_crd = ? AND shard = ''",
        (firm_crd,),
    ).fetchone()
    if existing is not None:
        return existing[0], False

    seen, capped = walk_shard(client, conn, firm_crd, None, observed_at, delay)
    if not capped:
        conn.execute(
            "INSERT OR REPLACE INTO walk_progress (firm_crd, shard, total_records, completed_at) "
            "VALUES (?, '', ?, ?)",
            (firm_crd, seen, observed_at),
        )
        conn.commit()
        return seen, False

    print(f"   {firm_name} ({firm_crd}) over 10k cap — sharding by state", file=sys.stderr)
    total = 0
    for state in US_STATES:
        already = conn.execute(
            "SELECT total_records FROM walk_progress WHERE firm_crd = ? AND shard = ?",
            (firm_crd, state),
        ).fetchone()
        if already is not None:
            total += already[0]
            continue
        try:
            s_seen, s_capped = walk_shard(client, conn, firm_crd, state, observed_at, delay)
        except Exception as e:
            print(f"   {firm_name}/{state} failed: {type(e).__name__}: {e}", file=sys.stderr)
            continue
        conn.execute(
            "INSERT OR REPLACE INTO walk_progress (firm_crd, shard, total_records, completed_at) "
            "VALUES (?, ?, ?, ?)",
            (firm_crd, state, s_seen, observed_at),
        )
        conn.commit()
        total += s_seen
        if s_capped:
            print(
                f"   WARN: {firm_name}/{state} still over 10k cap — partial coverage",
                file=sys.stderr,
            )
        time.sleep(delay)

    conn.execute(
        "INSERT OR REPLACE INTO walk_progress (firm_crd, shard, total_records, completed_at) "
        "VALUES (?, '', ?, ?)",
        (firm_crd, total, observed_at),
    )
    conn.commit()
    return total, True


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None, help="stop after N firms (testing)")
    ap.add_argument("--start-firm", type=str, default=None, help="resume from this firm CRD")
    ap.add_argument(
        "--delay", type=float, default=DEFAULT_REQ_DELAY,
        help=f"seconds between requests (default {DEFAULT_REQ_DELAY})",
    )
    ap.add_argument(
        "--smoke", action="store_true",
        help="quick sanity run against Ritholtz/Creative Planning/Fisher/Edelman/Mercer only",
    )
    ap.add_argument(
        "--state", type=str, default=None,
        help="restrict to firms whose main office is in this 2-letter state (e.g. IL)",
    )
    args = ap.parse_args()

    main_office_state = args.state.upper() if args.state else None
    firms = load_firm_crds(main_office_state=main_office_state)
    if main_office_state:
        print(
            f"loaded {len(firms):,} firms with main office in {main_office_state}",
            file=sys.stderr,
        )
    else:
        print(f"loaded {len(firms):,} firms from SEC bulk CSV", file=sys.stderr)

    if args.smoke:
        smoke_crds = {"168652", "128170", "109544", "287770", "117344"}
        firms = [f for f in firms if f[0] in smoke_crds]
        print(f"smoke mode: walking {len(firms)} known firms", file=sys.stderr)
    else:
        if args.start_firm:
            idx = next(
                (i for i, (crd, _) in enumerate(firms) if crd == args.start_firm), None,
            )
            if idx is None:
                sys.exit(f"firm CRD {args.start_firm} not found in CSV")
            firms = firms[idx:]
            print(f"resuming from index {idx} ({args.start_firm})", file=sys.stderr)
        if args.limit:
            firms = firms[: args.limit]
            print(f"limited to {len(firms)} firms", file=sys.stderr)

    conn = db_connect()
    started = time.time()
    total_iars = 0
    sharded_firms = 0

    with httpx.Client(headers=BROWSER_HEADERS) as client:
        for i, (crd, name) in enumerate(firms, 1):
            try:
                seen, was_sharded = walk_firm(client, conn, crd, name, args.delay)
            except Exception as e:
                print(
                    f"[{i}/{len(firms)}] FAIL {name} ({crd}): {type(e).__name__}: {e}",
                    file=sys.stderr,
                )
                continue
            total_iars += seen
            if was_sharded:
                sharded_firms += 1
            elapsed = time.time() - started
            rate = i / elapsed if elapsed else 0
            print(
                f"[{i}/{len(firms)}] {name} ({crd}): {seen} IARs | "
                f"total={total_iars:,} | {rate:.2f} firm/s",
            )

    elapsed = time.time() - started
    print(
        f"\ndone. {total_iars:,} individual records across {len(firms):,} firms "
        f"({sharded_firms} state-sharded) in {elapsed/60:.1f} min",
    )


if __name__ == "__main__":
    main()
