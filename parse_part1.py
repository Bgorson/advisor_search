"""Round 5: deterministic Part 1 extraction from SEC bulk Form ADV CSV.

Maps Item 5E/5G/5F/5D checkboxes + dollar fields to plain-English answers
for the 5 stakeholder questions. No LLM, no PDF, no per-firm API call.
"""

from __future__ import annotations

import csv
import io
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

ZIP_PATH = Path(__file__).parent / "data" / "ia050126.zip"

TARGETS = [
    "RITHOLTZ WEALTH MANAGEMENT",
    "CREATIVE PLANNING",
    "FISHER INVESTMENTS",
    "EDELMAN FINANCIAL ENGINES",
    "MERCER GLOBAL ADVISORS",
]

# Form ADV Item 5E — Compensation Arrangements
COMPENSATION = {
    "5E(1)": "Percentage of assets under management",
    "5E(2)": "Hourly charges",
    "5E(3)": "Subscription fees",
    "5E(4)": "Fixed fees (other than subscription)",
    "5E(5)": "Commissions",
    "5E(6)": "Performance-based fees",
    "5E(7)": "Other",
}

# Form ADV Item 5G — Advisory Services Provided
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

# Form ADV Item 5D — Types of Clients (5D(1)(x) = count, 5D(2)(x) = AUM)
CLIENT_TYPES = {
    "a": "Individuals (non-HNW)",
    "b": "High net worth individuals",
    "c": "Banking/thrift institutions",
    "d": "Investment companies",
    "e": "Business development companies",
    "f": "Pooled investment vehicles",
    "g": "Pension/profit sharing plans",
    "h": "Charitable organizations",
    "i": "Corporations/other businesses",
    "j": "State/municipal entities",
    "k": "Other investment advisers",
    "l": "Insurance companies",
    "m": "Other",
}


@dataclass
class FirmFacts:
    crd: str
    name: str
    legal_name: str
    sec_number: str
    state: str
    website: str
    total_websites: str
    latest_filing: str
    aum_discretionary: str
    aum_non_discretionary: str
    accounts_discretionary: str
    accounts_non_discretionary: str
    custody_aum: str
    services: list[str] = field(default_factory=list)
    services_other: str = ""
    compensation: list[str] = field(default_factory=list)
    compensation_other: str = ""
    client_types: list[tuple[str, str, str]] = field(default_factory=list)


def parse_csv() -> tuple[list[str], dict[str, list[str]]]:
    """Returns (header, {firm_name_upper: row}) for our targets."""
    with zipfile.ZipFile(ZIP_PATH) as z:
        with z.open(z.namelist()[0]) as f:
            text = f.read().decode("latin-1")
    reader = csv.reader(io.StringIO(text))
    header = next(reader)

    found: dict[str, list[str]] = {}
    for row in reader:
        name = row[10].upper()
        for target in TARGETS:
            if target in name and target not in found:
                found[target] = row
                break
        if len(found) == len(TARGETS):
            break
    return header, found


def get(row: list[str], header: list[str], col: str) -> str:
    try:
        idx = header.index(col)
    except ValueError:
        return ""
    return row[idx] if idx < len(row) else ""


def extract_facts(header: list[str], row: list[str]) -> FirmFacts:
    services = [
        label
        for code, label in SERVICES.items()
        if get(row, header, code) == "Y"
    ]
    compensation = [
        label
        for code, label in COMPENSATION.items()
        if get(row, header, code) == "Y"
    ]

    client_types = []
    for letter, label in CLIENT_TYPES.items():
        count = get(row, header, f"5D(1)({letter})").strip()
        aum = get(row, header, f"5D(2)({letter})").strip()
        if count and count not in ("0", ""):
            client_types.append((label, count, aum))

    return FirmFacts(
        crd=get(row, header, "Organization CRD#"),
        name=get(row, header, "Primary Business Name"),
        legal_name=get(row, header, "Legal Name"),
        sec_number=get(row, header, "SEC#"),
        state=get(row, header, "Main Office State"),
        website=get(row, header, "Website Address"),
        total_websites=get(row, header, "Total Number of Website Addresses"),
        latest_filing=get(row, header, "Latest ADV Filing Date"),
        aum_discretionary=get(row, header, "5F(2)(a)").strip(),
        aum_non_discretionary=get(row, header, "5F(2)(b)").strip(),
        accounts_discretionary=get(row, header, "5F(2)(d)").strip(),
        accounts_non_discretionary=get(row, header, "5F(2)(e)").strip(),
        custody_aum=get(row, header, "9A(2)(a)").strip(),
        services=services,
        services_other=get(row, header, "5G(12)-Other").strip(),
        compensation=compensation,
        compensation_other=get(row, header, "5E(7)-Other").strip(),
        client_types=client_types,
    )


def fmt_money(s: str) -> str:
    s = s.strip()
    if not s or s in (".", ".00", "0", "0.00"):
        return "$0"
    try:
        n = float(s.replace(",", ""))
    except ValueError:
        return s
    if n >= 1_000_000_000:
        return f"${n/1_000_000_000:.2f}B"
    if n >= 1_000_000:
        return f"${n/1_000_000:.1f}M"
    if n >= 1_000:
        return f"${n/1_000:.0f}K"
    return f"${n:,.0f}"


def report(f: FirmFacts) -> None:
    print(f"\n{'=' * 72}")
    print(f"{f.name}  (CRD {f.crd}, {f.sec_number})")
    print(f"{f.legal_name} · {f.state} · last ADV filing {f.latest_filing}")
    print("=" * 72)

    total_disc = fmt_money(f.aum_discretionary)
    total_nondisc = fmt_money(f.aum_non_discretionary)
    print(f"\nAUM: {total_disc} discretionary · {total_nondisc} non-discretionary")
    print(
        f"Accounts: {f.accounts_discretionary} discretionary · "
        f"{f.accounts_non_discretionary} non-discretionary"
    )
    if f.custody_aum and f.custody_aum != ".00":
        print(f"Assets in custody: {fmt_money(f.custody_aum)}")

    print(f"\nWebsite: {f.website or '(none)'}")
    if f.total_websites and f.total_websites not in ("0", "1"):
        print(f"  ({f.total_websites} websites total registered)")

    print(f"\n[#1 Services offered] — {len(f.services)} categories")
    for s in f.services:
        print(f"  • {s}")
    if f.services_other:
        print(f"  • Other: {f.services_other}")

    print(f"\n[#3 How clients pay] — {len(f.compensation)} fee types")
    for c in f.compensation:
        print(f"  • {c}")
    if f.compensation_other:
        print(f"  • Other: {f.compensation_other}")

    print("\n[#2 Engagement model — partial signal]")
    fee_only = "Commissions" not in f.compensation
    print(f"  fee-only: {'Yes' if fee_only else 'No (has commissions)'}")
    has_disc = f.aum_discretionary and float(f.aum_discretionary.replace(",", "") or 0) > 0
    has_nondisc = f.aum_non_discretionary and float(f.aum_non_discretionary.replace(",", "") or 0) > 0
    if has_disc and has_nondisc:
        print("  authority: both discretionary and non-discretionary")
    elif has_disc:
        print("  authority: discretionary only")
    elif has_nondisc:
        print("  authority: non-discretionary only")

    print("\n[#4 Minimum client assets] — NOT IN PART 1 (need brochure or website)")

    print("\n[#5 Platform/custodian] — partial signal")
    if f.custody_aum and f.custody_aum != ".00":
        print(f"  has custody of ${fmt_money(f.custody_aum)} (custodian names in Schedule D)")
    else:
        print("  no custody reported (uses 3rd-party custodian)")
    print("  full custodian list: NOT IN PART 1 main CSV (need Schedule D detail or website)")

    if f.client_types:
        print("\nClient mix (context for engagement model):")
        for label, count, aum in f.client_types[:5]:
            print(f"  {label:45s}  count={count:>8s}  AUM={fmt_money(aum)}")


def main() -> None:
    header, found = parse_csv()
    print(f"loaded {len(found)} firms from CSV")
    for target in TARGETS:
        row = found.get(target)
        if row is None:
            print(f"\n!! {target} not found in CSV")
            continue
        report(extract_facts(header, row))


if __name__ == "__main__":
    main()
