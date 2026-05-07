"""Streamlit app: search SEC-registered investment advisers and view structured Form ADV Part 1 facts.

Stakeholder-shareable v0. Honest about coverage:
- Fields #1 (services) and #3 (fees) come from Item 5G and 5E checkboxes
- Field #2 (engagement model) is partial — fee-only flag + discretionary mix
- Field #5 (custodian) is partial — custody $ amount, no names yet
- Field #4 (minimums) is not covered yet — brochure/website crawl needed
"""

from __future__ import annotations

import csv
import io
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import streamlit as st
from rapidfuzz import process

# May 2026 monthly snapshot (5MB)
SEC_URL = (
    "https://www.sec.gov/files/investment/data/other/"
    "information-about-registered-investment-advisers-exempt-reporting-advisers/"
    "ia050126.zip"
)
SOURCE_LABEL = "SEC Form ADV Part 1 — May 2026 monthly snapshot"
USER_AGENT = "syl-proto/0.1 (brandon.gorson@gmail.com)"

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


@st.cache_resource(show_spinner="Loading SEC Form ADV data (one-time, ~10s)…")
def load_data() -> tuple[list[str], list[list[str]], dict[str, int]]:
    """Download (cached) and parse the SEC bulk CSV. Returns (header, rows, name_to_idx)."""
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
    return header, rows, name_to_idx


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


def find_firm_candidates(query: str, name_to_idx: dict[str, int], limit: int = 10) -> list[tuple[str, int, int]]:
    """Returns [(name, csv_idx, score)] sorted by relevance."""
    q = query.strip().upper()
    if not q:
        return []
    if q.isdigit():
        return []  # CRD path handled separately
    matches = process.extract(q, name_to_idx.keys(), limit=limit, score_cutoff=60)
    return [(name, name_to_idx[name], score) for name, score, _ in matches]


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

    # Field #1
    st.markdown("### #1 Services offered")
    st.caption(f"Source: {SOURCE_LABEL} · Item 5G checkboxes")
    for s in firm.services:
        st.markdown(f"- {s}")
    if firm.services_other:
        st.markdown(f"- **Other:** {firm.services_other}")

    # Field #3
    st.markdown("### #3 How clients pay")
    st.caption(f"Source: {SOURCE_LABEL} · Item 5E checkboxes")
    for c in firm.compensation:
        st.markdown(f"- {c}")
    if firm.compensation_other:
        st.markdown(f"- **Other:** {firm.compensation_other}")

    # Field #2
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

    # Field #4
    st.markdown("### #4 Minimum client assets")
    st.warning(
        "Not covered yet. Form ADV Part 1 (the structured filing) doesn't disclose this. "
        "Future round will pull this from each firm's Part 2A brochure or website."
    )

    # Field #5
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

    # Misc / context
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


def main() -> None:
    st.set_page_config(
        page_title="RIA Lookup — syl_proto",
        page_icon="📄",
        layout="centered",
    )
    st.title("Investment Adviser Lookup")
    st.caption(
        f"Prototype · Data: {SOURCE_LABEL} · "
        "Covers SEC-registered investment advisers (~16,800 firms)."
    )

    header, rows, name_to_idx = load_data()
    st.caption(f"Loaded {len(rows):,} firms.")

    query = st.text_input("Search by firm name or CRD number", placeholder="e.g. Ritholtz, or 168652")
    if not query:
        st.info("Enter a firm name or CRD to begin.")
        with st.expander("Example queries"):
            st.markdown("- `Ritholtz Wealth`\n- `Creative Planning`\n- `168652` (Ritholtz's CRD)")
        return

    # CRD path
    if query.strip().isdigit():
        crd = query.strip()
        match = next(
            (i for i, row in enumerate(rows) if row[1] == crd),
            None,
        )
        if match is None:
            st.error(f"No firm found with CRD {crd}. (Note: only SEC-registered RIAs are in this dataset; state-only registered firms won't appear.)")
            return
        render_firm(to_firm(header, rows[match]))
        return

    # Name path — fuzzy
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


if __name__ == "__main__":
    main()
