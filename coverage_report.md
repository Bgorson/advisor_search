# Round 6 — Per-Firm Coverage Report (no LLM yet)

**Sources used:**
- SEC bulk Form ADV Part 1 CSV — May 2026 (`ia050126.zip`, 5MB)
- All-time Schedule D archive — through Dec 2024 (`adv-filing-data-...part2.zip`, 409MB) — *not yet parsed*
- December 2024 brochure bulk (`adv-brochures-2024-december.zip`, 405MB) — only 1 of 5 firms had a filing this month
- IAPD direct brochure URL pattern — works for known BRCHR_VRSN_ID but ID lookup is gated

**Cumulative LLM cost so far: $0** (everything from milestones 1-4 was deterministic).

## Coverage matrix

| Field | Ritholtz | Creative Planning | Fisher | Edelman | Mercer |
|---|---|---|---|---|---|
| #1 Services | ✅ Part 1 | ✅ Part 1 | ✅ Part 1 | ✅ Part 1 | ✅ Part 1 |
| #2 Engagement (fee-only/discretionary) | ⚠️ Part 1 partial | ⚠️ Part 1 partial | ⚠️ Part 1 partial | ⚠️ Part 1 partial | ⚠️ Part 1 + brochure (sources DISAGREE) |
| #3 Fee structure | ✅ Part 1 | ✅ Part 1 | ✅ Part 1 | ✅ Part 1 | ✅ Part 1 |
| #4 Minimums | ❌ no source | ❌ no source | ❌ no source | ❌ no source | ⚠️ regex partial — fee floors found, not asset minimum |
| #5 Custodians | ❌ no brochure | ❌ no brochure | ❌ no brochure | ❌ no brochure | ✅ keyword dict — Schwab primary, Fidelity/Raymond James secondary |

## What we have for sure (free path)

For **all 16,779 SEC-registered RIAs** in the bulk Part 1 CSV, with $0 in API spend:
- Field #1: full categorical service breakdown + free-text "Other"
- Field #3: full fee structure breakdown + free-text "Other"
- Partial signals on #2: fee-only/has-commissions flag, discretionary/non-discretionary AUM mix, client mix
- Partial signal on #5: custody $ amount (yes/no relationship to a custodian, but no name)
- Firm metadata: AUM, account counts, employee count, latest filing date, primary website (often unreliable)

## What we don't have

- **Field #4 (minimums)** — entirely brochure/website territory. No structured source.
- **Field #5 (custodian names)** — brochure or website only. Schedule D Item 9A is filed but not in any bulk feed; sec-api.io's Brochure API is enterprise-only and doesn't appear to expose parsed Item 12 anyway.
- **Brochures at scale** — bulk monthly zips work but each is 350-625 MB, and a firm's specific filing month is unknown without per-firm lookup. Direct URL needs `BRCHR_VRSN_ID` which is only available via gated XHR.

## Source disagreement (Mercer)

- Part 1 (May 2026) Item 5E: AUM%, Hourly, Fixed fees, Other — **no commissions box** → reads as fee-only.
- Brochure (Dec 2024): "fee-based" 3×, "commission-based" 2×, "non-discretionary" 5× → reads as fee-based mixed.

This is exactly what the stakeholder's pin (`time series + provenance for every field`) is for. Don't pick a winner — record both with source + date and let consumers reconcile.

## What to do at decision gate

Three options for the next round:

**(A) Accept current free coverage, ship a v0 API.**
For 16,779 firms: fields #1 and #3 fully answered, #2 and #5 partial, #4 not answered. Useful as-is, totally free, no LLM ever.

**(B) Add brochure-text pass for the 50-firm prototype.**
Either download more monthly bulks (~$0 but ~5-10 GB bandwidth) or solve the BRCHR_VRSN_ID lookup (probably needs scraping IAPD with a headless browser since the data only loads via JS). Once obtained, run the regex/keyword pass we just validated. Estimated coverage lift: ~50% on #4, ~95% on #5, real signal on #2.

**(C) Pull the LLM trigger only for the holdouts.**
After (B), any firm where regex/keyword left a field empty gets one LLM call per missing field. With Sonnet 4.6 and the brochure already extracted, that's ~$0.05 per holdout-field, scoped to specific firms.
