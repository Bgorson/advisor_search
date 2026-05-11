"""Streamlit app: search SEC-registered investment advisers (firms and individuals).

Prototype scope:
- Firm layer: ~16,800 SEC-registered RIAs, sourced from SEC Form ADV Part 1 monthly bulk.
- Advisor layer: ~26,600 IARs at IL-headquartered firms, sourced from IAPD individual search.

Most filter-able fields about an advisor are inherited from the advisor's firm
affiliation (services, fee structure, AUM mix, custody). The UI surfaces these as
advisor-level fields with explicit "from firm" attribution. Individual-only signals
(broker dual-registration, regulatory disclosures, industry tenure) are surfaced
prominently per the editorial stance: paradoxes worth knowing about get called out,
they don't get smoothed over.
"""

from __future__ import annotations

import csv
import io
import sqlite3
import zipfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import httpx
import streamlit as st
from rapidfuzz import process

SEC_URL = (
    "https://www.sec.gov/files/investment/data/other/"
    "information-about-registered-investment-advisers-exempt-reporting-advisers/"
    "ia050126.zip"
)
SOURCE_LABEL = "SEC Form ADV Part 1 — May 2026 monthly snapshot"
USER_AGENT = "advisor-search/0.1 bgorson32@gmail.com"

DB_PATH = Path("data/individuals.db")

COMPENSATION = {
    "5E(1)": "Percentage of assets under management",
    "5E(2)": "Hourly charges",
    "5E(3)": "Subscription fees",
    "5E(4)": "Fixed fees (other than subscription)",
    "5E(5)": "Commissions",
    "5E(6)": "Performance-based fees",
    "5E(7)": "Other",
}

SERVICES = {
    "5G(1)": "Financial planning",
    "5G(2)": "Portfolio management for individuals/small businesses",
    "5G(3)": "Portfolio management for investment companies",
    "5G(4)": "Portfolio management for pooled investment vehicles",
    "5G(5)": "Portfolio management for businesses/institutional clients",
    "5G(6)": "Pension consulting",
    "5G(7)": "Selection of other advisers",
    "5G(8)": "Publication of periodicals/newsletters",
    "5G(9)": "Security ratings or pricing",
    "5G(10)": "Market timing services",
    "5G(11)": "Educational seminars/workshops",
    "5G(12)": "Other",
}


@dataclass
class Firm:
    crd: str
    name: str
    legal_name: str
    sec_number: str
    state: str
    city: str
    website: str
    total_websites: str
    latest_filing: str
    aum_discretionary: float
    aum_non_discretionary: float
    accounts_discretionary: str
    accounts_non_discretionary: str
    custody_aum: float
    services: list[str] = field(default_factory=list)
    services_other: str = ""
    compensation: list[str] = field(default_factory=list)
    compensation_other: str = ""


@dataclass
class Advisor:
    crd: str
    first_name: str
    middle_name: str
    last_name: str
    ia_scope: str
    bc_scope: str
    disclosure_fl: str
    finra_registration_count: int
    industry_start_date: str
    employments: list[dict] = field(default_factory=list)

    @property
    def full_name(self) -> str:
        parts = [self.first_name, self.middle_name, self.last_name]
        return " ".join(p for p in parts if p).strip()

    @property
    def display_name(self) -> str:
        return f"{self.first_name} {self.last_name}".strip()

    @property
    def has_disclosure(self) -> bool:
        return self.disclosure_fl == "Y"

    @property
    def is_broker_registered(self) -> bool:
        return self.bc_scope == "Active"


@st.cache_resource(show_spinner="Loading SEC Form ADV firm data (one-time, ~10s)…")
def load_data() -> tuple[list[str], list[list[str]], dict[str, int], dict[str, int]]:
    """Download (cached) and parse the SEC bulk CSV.
    Returns (header, rows, name_to_idx, crd_to_idx)."""
    cache_path = Path("data/ia050126.zip")
    if not cache_path.exists():
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        with httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=120) as c:
            r = c.get(SEC_URL, follow_redirects=True)
            r.raise_for_status()
            cache_path.write_bytes(r.content)
    with zipfile.ZipFile(cache_path) as z:
        with z.open(z.namelist()[0]) as f:
            text = f.read().decode("latin-1")
    reader = csv.reader(io.StringIO(text))
    header = next(reader)
    rows = list(reader)
    name_to_idx = {row[10].upper(): i for i, row in enumerate(rows) if row[10]}
    crd_to_idx = {row[1]: i for i, row in enumerate(rows) if row[1]}
    return header, rows, name_to_idx, crd_to_idx


@st.cache_resource(show_spinner="Loading advisor index (IL prototype, ~26k IARs)…")
def load_advisors() -> tuple[list[Advisor], list[str]]:
    """Load all individuals from SQLite. Returns (advisors, search_names_upper)."""
    if not DB_PATH.exists():
        return [], []
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row

    employments: dict[str, list[dict]] = {}
    for row in conn.execute(
        "SELECT ind_crd, firm_crd, firm_name, branch_city, branch_state, "
        "branch_zip, ia_only FROM current_employments"
    ):
        employments.setdefault(row["ind_crd"], []).append(dict(row))

    advisors: list[Advisor] = []
    for row in conn.execute(
        "SELECT ind_crd, first_name, middle_name, last_name, ia_scope, bc_scope, "
        "disclosure_fl, finra_registration_count, industry_cal_date_iapd "
        "FROM individuals"
    ):
        advisors.append(
            Advisor(
                crd=row["ind_crd"],
                first_name=row["first_name"] or "",
                middle_name=row["middle_name"] or "",
                last_name=row["last_name"] or "",
                ia_scope=row["ia_scope"] or "",
                bc_scope=row["bc_scope"] or "",
                disclosure_fl=row["disclosure_fl"] or "",
                finra_registration_count=row["finra_registration_count"] or 0,
                industry_start_date=row["industry_cal_date_iapd"] or "",
                employments=employments.get(row["ind_crd"], []),
            )
        )
    conn.close()

    names = [a.display_name.upper() for a in advisors]
    return advisors, names


def _parse_money(s: str) -> float:
    s = s.strip().replace(",", "")
    if not s or s in (".", ".00"):
        return 0.0
    try:
        return float(s)
    except ValueError:
        return 0.0


def _get(row: list[str], header: list[str], col: str) -> str:
    try:
        idx = header.index(col)
    except ValueError:
        return ""
    return row[idx] if idx < len(row) else ""


def to_firm(header: list[str], row: list[str]) -> Firm:
    services = [label for code, label in SERVICES.items() if _get(row, header, code) == "Y"]
    compensation = [
        label for code, label in COMPENSATION.items() if _get(row, header, code) == "Y"
    ]
    return Firm(
        crd=_get(row, header, "Organization CRD#"),
        name=_get(row, header, "Primary Business Name"),
        legal_name=_get(row, header, "Legal Name"),
        sec_number=_get(row, header, "SEC#"),
        state=_get(row, header, "Main Office State"),
        city=_get(row, header, "Main Office City"),
        website=_get(row, header, "Website Address"),
        total_websites=_get(row, header, "Total Number of Website Addresses"),
        latest_filing=_get(row, header, "Latest ADV Filing Date"),
        aum_discretionary=_parse_money(_get(row, header, "5F(2)(a)")),
        aum_non_discretionary=_parse_money(_get(row, header, "5F(2)(b)")),
        accounts_discretionary=_get(row, header, "5F(2)(d)"),
        accounts_non_discretionary=_get(row, header, "5F(2)(e)"),
        custody_aum=_parse_money(_get(row, header, "9A(2)(a)")),
        services=services,
        services_other=_get(row, header, "5G(12)-Other"),
        compensation=compensation,
        compensation_other=_get(row, header, "5E(7)-Other"),
    )


def fmt_money(n: float) -> str:
    if n == 0:
        return "$0"
    if n >= 1_000_000_000:
        return f"${n/1_000_000_000:.2f}B"
    if n >= 1_000_000:
        return f"${n/1_000_000:.1f}M"
    if n >= 1_000:
        return f"${n/1_000:.0f}K"
    return f"${n:,.0f}"


def years_in_industry(start_date: str) -> int | None:
    if not start_date or len(start_date) < 4:
        return None
    try:
        start_year = int(start_date[:4])
    except ValueError:
        return None
    return max(0, datetime.now().year - start_year)


def find_firm_candidates(
    query: str, name_to_idx: dict[str, int], limit: int = 10
) -> list[tuple[str, int, int]]:
    q = query.strip().upper()
    if not q or q.isdigit():
        return []
    matches = process.extract(q, name_to_idx.keys(), limit=limit, score_cutoff=60)
    return [(name, name_to_idx[name], score) for name, score, _ in matches]


def find_advisor_candidates(
    query: str, advisors: list[Advisor], names: list[str], limit: int = 15
) -> list[tuple[int, int]]:
    """Returns [(advisor_idx, score)] for fuzzy match. CRD lookups are exact."""
    q = query.strip().upper()
    if not q:
        return []
    if q.isdigit():
        return [(i, 100) for i, a in enumerate(advisors) if a.crd == q]
    matches = process.extract(q, names, limit=limit, score_cutoff=70)
    return [(idx, score) for _, score, idx in matches]


def render_firm(firm: Firm) -> None:
    st.subheader(firm.name)
    cols = st.columns(4)
    cols[0].metric("CRD", firm.crd)
    cols[1].metric("SEC #", firm.sec_number)
    cols[2].metric("Location", f"{firm.city}, {firm.state}" if firm.city else firm.state)
    cols[3].metric("Last ADV filing", firm.latest_filing)

    cols = st.columns(3)
    cols[0].metric("AUM (discretionary)", fmt_money(firm.aum_discretionary))
    cols[1].metric("AUM (non-discretionary)", fmt_money(firm.aum_non_discretionary))
    cols[2].metric("Custody $", fmt_money(firm.custody_aum) if firm.custody_aum else "(none)")

    if firm.legal_name and firm.legal_name != firm.name:
        st.caption(f"Legal name: {firm.legal_name}")

    st.divider()

    st.markdown("### #1 Services offered")
    st.caption(f"Source: {SOURCE_LABEL} · Item 5G checkboxes")
    for s in firm.services:
        st.markdown(f"- {s}")
    if firm.services_other:
        st.markdown(f"- **Other:** {firm.services_other}")

    st.markdown("### #3 How clients pay")
    st.caption(f"Source: {SOURCE_LABEL} · Item 5E checkboxes")
    for c in firm.compensation:
        st.markdown(f"- {c}")
    if firm.compensation_other:
        st.markdown(f"- **Other:** {firm.compensation_other}")

    st.markdown("### #2 Engagement model")
    st.caption("⚠️ Partial signal — full answer requires brochure/website crawl (not yet wired up)")
    fee_only = "Commissions" not in firm.compensation
    has_disc = firm.aum_discretionary > 0
    has_nondisc = firm.aum_non_discretionary > 0
    if fee_only:
        st.markdown("- Fee-only (no commissions box checked in Item 5E)")
    else:
        st.markdown("- ⚠️ Has commission compensation per Item 5E")
    if has_disc and has_nondisc:
        st.markdown("- Both discretionary and non-discretionary management")
    elif has_disc:
        st.markdown("- Discretionary management only")
    elif has_nondisc:
        st.markdown("- Non-discretionary management only")

    if fee_only and has_disc and has_nondisc:
        disc_share = firm.aum_discretionary / (firm.aum_discretionary + firm.aum_non_discretionary)
        if disc_share > 0.8:
            st.warning(
                f"**Worth a closer look:** firm presents as fee-only, but "
                f"{disc_share:.0%} of AUM is discretionary — meaning advisors can trade "
                f"without client permission on most accounts. Compare against the firm's "
                f"public positioning."
            )

    st.markdown("### #4 Minimum client assets")
    st.warning(
        "Not covered yet. Form ADV Part 1 (the structured filing) doesn't disclose this. "
        "Future round will pull this from each firm's Part 2A brochure or website."
    )

    st.markdown("### #5 Platform / custodian affiliation")
    st.caption(f"Source: {SOURCE_LABEL} · Item 9A custody disclosure")
    if firm.custody_aum:
        st.markdown(
            f"- Reports {fmt_money(firm.custody_aum)} in custody — has at least one qualified custodian relationship"
        )
        st.caption(
            "⚠️ Custodian *names* (Schedule D Section 9.A) aren't in any SEC bulk feed. "
            "Future round will extract them from each firm's Part 2A brochure."
        )
    else:
        st.markdown("- No custody reported (uses third-party custodian)")

    st.divider()
    st.markdown("### Additional context")
    if firm.website:
        st.markdown(
            f"- Primary website on file: {firm.website}"
            + (
                f" *(plus {int(firm.total_websites) - 1} other registered URLs)*"
                if firm.total_websites and firm.total_websites != "1"
                else ""
            )
        )
        if firm.total_websites and int(firm.total_websites) > 5:
            st.caption(
                "⚠️ This firm registered many websites (often from acquired sub-RIAs). "
                "The single 'Website Address' field in the bulk CSV is not always the primary marketing site."
            )
    if firm.accounts_discretionary or firm.accounts_non_discretionary:
        st.markdown(
            f"- Account count: {firm.accounts_discretionary or '0'} discretionary · "
            f"{firm.accounts_non_discretionary or '0'} non-discretionary"
        )


def render_advisor(
    advisor: Advisor,
    header: list[str],
    rows: list[list[str]],
    crd_to_idx: dict[str, int],
) -> None:
    st.subheader(advisor.display_name)
    if advisor.middle_name:
        st.caption(f"Full name: {advisor.full_name}")

    cols = st.columns(4)
    cols[0].metric("CRD", advisor.crd)
    yrs = years_in_industry(advisor.industry_start_date)
    cols[1].metric("In industry", f"{yrs} yrs" if yrs is not None else "—")
    cols[2].metric("IA status", advisor.ia_scope or "—")
    cols[3].metric(
        "Also broker?",
        "Yes" if advisor.is_broker_registered else ("Was" if advisor.bc_scope == "InActive" else "No"),
    )

    flags: list[tuple[str, str]] = []
    if advisor.has_disclosure:
        flags.append(
            (
                "warning",
                "**Has a regulatory disclosure on file.** Complaints, judgments, settlements, "
                "or termination events are noted in this advisor's record. Detail page on "
                "[IAPD](https://adviserinfo.sec.gov/individual/summary/" + advisor.crd + ").",
            )
        )
    if advisor.is_broker_registered:
        flags.append(
            (
                "info",
                "**Also registered as a broker** (FINRA BrokerCheck active). May sell "
                "commissioned products in addition to advisory work.",
            )
        )

    if flags:
        st.markdown("### Worth knowing")
        for level, msg in flags:
            if level == "warning":
                st.warning(msg)
            else:
                st.info(msg)

    st.divider()

    st.markdown("### Current employment")
    if not advisor.employments:
        st.warning("No current employment recorded.")
        return

    # Dedupe by (firm_crd, branch_city, branch_state) for display
    seen = set()
    for emp in advisor.employments:
        key = (emp["firm_crd"], emp.get("branch_city", ""), emp.get("branch_state", ""))
        if key in seen:
            continue
        seen.add(key)
        loc = ", ".join(
            filter(None, [emp.get("branch_city", ""), emp.get("branch_state", "")])
        )
        zip_str = f" {emp['branch_zip']}" if emp.get("branch_zip") else ""
        st.markdown(
            f"- **{emp['firm_name']}** (CRD {emp['firm_crd']}) — {loc or 'no branch location'}{zip_str}"
        )

    st.divider()

    # Firm-inherited fields. If advisor is at multiple firms, show first firm's
    # data with attribution, and note the other firms below.
    primary_firm_crd = advisor.employments[0]["firm_crd"]
    primary_firm_name = advisor.employments[0]["firm_name"]
    firm_idx = crd_to_idx.get(primary_firm_crd)
    if firm_idx is None:
        st.info(
            f"Firm-inherited fields not available — {primary_firm_name} (CRD {primary_firm_crd}) "
            f"is not in the SEC Form ADV Part 1 snapshot. Could be state-only registered, "
            f"exempt reporting, or recently de-registered."
        )
        return

    firm = to_firm(header, rows[firm_idx])

    st.markdown(f"### Firm-inherited attributes — from {firm.name}")
    st.caption(
        f"Source: {SOURCE_LABEL}. These are firm-wide attributes. The individual advisor "
        f"may operate under a narrower or broader scope than the firm-wide report."
    )

    cols = st.columns(3)
    cols[0].metric("Firm AUM (disc.)", fmt_money(firm.aum_discretionary))
    cols[1].metric("Firm AUM (non-disc.)", fmt_money(firm.aum_non_discretionary))
    cols[2].metric("Firm custody $", fmt_money(firm.custody_aum) if firm.custody_aum else "(none)")

    st.markdown("**Services offered** (Form ADV Item 5G)")
    for s in firm.services:
        st.markdown(f"- {s}")
    if firm.services_other:
        st.markdown(f"- Other: {firm.services_other}")

    st.markdown("**Fee structure** (Form ADV Item 5E)")
    for c in firm.compensation:
        st.markdown(f"- {c}")
    if firm.compensation_other:
        st.markdown(f"- Other: {firm.compensation_other}")

    # Paradox detection — surface tensions per editorial stance.
    paradoxes: list[str] = []
    is_fee_only_firm = "Commissions" not in firm.compensation
    if advisor.is_broker_registered and is_fee_only_firm:
        paradoxes.append(
            f"**Advisor is registered as a broker, but {firm.name} reports no commissions** "
            f"in its Item 5E fee structure. Worth understanding how this advisor's broker "
            f"activity is structured relative to the firm's fee-only positioning."
        )

    has_disc = firm.aum_discretionary > 0
    has_nondisc = firm.aum_non_discretionary > 0
    if has_disc and has_nondisc:
        disc_share = firm.aum_discretionary / (firm.aum_discretionary + firm.aum_non_discretionary)
        if is_fee_only_firm and disc_share > 0.8:
            paradoxes.append(
                f"**Firm presents as fee-only, but {disc_share:.0%} of firm AUM is "
                f"discretionary** — meaning advisors can trade without client permission "
                f"on most accounts. Compare against the firm's public positioning."
            )

    if paradoxes:
        st.divider()
        st.markdown("### Worth a closer look")
        for p in paradoxes:
            st.warning(p)

    # Other firms (if multi-affiliated)
    other_firms = [
        e for e in advisor.employments if e["firm_crd"] != primary_firm_crd
    ]
    if other_firms:
        st.divider()
        st.markdown("### Other current affiliations")
        st.caption(
            "This advisor is registered at multiple firms. Firm-inherited attributes "
            "above reflect the first listed firm only."
        )
        seen_other = set()
        for emp in other_firms:
            if emp["firm_crd"] in seen_other:
                continue
            seen_other.add(emp["firm_crd"])
            st.markdown(f"- {emp['firm_name']} (CRD {emp['firm_crd']})")


def render_advisor_search(
    header: list[str],
    rows: list[list[str]],
    crd_to_idx: dict[str, int],
    advisors: list[Advisor],
    advisor_names: list[str],
) -> None:
    if not advisors:
        st.warning(
            "Advisor data not loaded. Run `python3 iapd_walker.py --state IL` to populate "
            "`data/individuals.db`, then refresh."
        )
        return

    st.caption(
        f"Prototype scope · {len(advisors):,} IARs indexed from IL-headquartered firms. "
        f"Advisors at firms HQ'd outside Illinois are not yet available in this build."
    )

    query = st.text_input(
        "Search by advisor name or individual CRD",
        placeholder="e.g. Tatum Schuler, or 1056600",
    )
    if not query:
        st.info("Enter an advisor name or CRD to begin.")
        with st.expander("Example queries"):
            st.markdown(
                "- `Ronald Selik` (has a regulatory disclosure)\n"
                "- `Tatum Schuler`\n"
                "- `Mark Baker` (registered at multiple First Trust entities)\n"
                "- `1056600` (Judith Slawsky, 43+ years in industry)"
            )
        return

    candidates = find_advisor_candidates(query, advisors, advisor_names, limit=15)
    if not candidates:
        st.error(f"No advisor matches for {query!r}.")
        st.caption(
            "Reminder: this prototype only indexes advisors at IL-headquartered firms. "
            "If the advisor's firm is HQ'd elsewhere, they won't appear here yet."
        )
        return

    if len(candidates) == 1 or candidates[0][1] >= 95:
        render_advisor(advisors[candidates[0][0]], header, rows, crd_to_idx)
        return

    def label(c: tuple[int, int]) -> str:
        idx, score = c
        a = advisors[idx]
        primary = a.employments[0]["firm_name"] if a.employments else "no firm"
        return f"{a.display_name}  —  {primary}  (CRD {a.crd}, score {score})"

    chosen = st.selectbox(
        "Multiple matches — pick one:",
        options=candidates,
        format_func=label,
    )
    if chosen is not None:
        render_advisor(advisors[chosen[0]], header, rows, crd_to_idx)


def render_firm_search(
    header: list[str],
    rows: list[list[str]],
    name_to_idx: dict[str, int],
) -> None:
    query = st.text_input("Search by firm name or CRD number", placeholder="e.g. Ritholtz, or 168652")
    if not query:
        st.info("Enter a firm name or CRD to begin.")
        with st.expander("Example queries"):
            st.markdown("- `Ritholtz Wealth`\n- `Creative Planning`\n- `168652` (Ritholtz's CRD)")
        return

    if query.strip().isdigit():
        crd = query.strip()
        match = next((i for i, row in enumerate(rows) if row[1] == crd), None)
        if match is None:
            st.error(
                f"No firm found with CRD {crd}. "
                f"(Note: only SEC-registered RIAs are in this dataset; "
                f"state-only registered firms won't appear.)"
            )
            return
        render_firm(to_firm(header, rows[match]))
        return

    candidates = find_firm_candidates(query, name_to_idx, limit=10)
    if not candidates:
        st.error(f"No matches for {query!r}.")
        return
    if len(candidates) == 1 or candidates[0][2] >= 95:
        render_firm(to_firm(header, rows[candidates[0][1]]))
        return
    chosen = st.selectbox(
        "Multiple matches — pick one:",
        options=candidates,
        format_func=lambda c: f"{c[0]}  (match score {c[2]})",
    )
    if chosen is not None:
        render_firm(to_firm(header, rows[chosen[1]]))


def main() -> None:
    st.set_page_config(
        page_title="RIA Lookup — syl_proto",
        page_icon="📄",
        layout="centered",
    )
    st.title("Investment Adviser Lookup")
    st.caption(
        f"Prototype · {SOURCE_LABEL} · "
        "~16,800 SEC-registered firms · ~26,600 advisors at IL-headquartered firms."
    )

    header, rows, name_to_idx, crd_to_idx = load_data()
    advisors, advisor_names = load_advisors()

    mode = st.radio(
        "What are you searching for?",
        ["Advisor (person)", "Firm"],
        horizontal=True,
        index=0,
    )

    if mode == "Advisor (person)":
        render_advisor_search(header, rows, crd_to_idx, advisors, advisor_names)
    else:
        render_firm_search(header, rows, name_to_idx)


if __name__ == "__main__":
    main()
