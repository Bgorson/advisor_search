---
title: Advisor Lookup
emoji: 📄
colorFrom: blue
colorTo: indigo
sdk: streamlit
sdk_version: 1.41.0
app_file: app.py
pinned: false
---

# Advisor Search

Prototype web app that looks up SEC-registered investment advisers and returns structured Form ADV Part 1 data. **Currently $0/mo to operate** (no LLM calls in the deployed path).

## What this app answers today

For any of ~16,800 SEC-registered RIAs, given a firm name or CRD:

| # | Stakeholder field | Coverage today | Source |
|---|---|---|---|
| 1 | **Services offered** | ✅ Complete | Form ADV Part 1, Item 5G checkboxes + "Other" free text |
| 2 | **Engagement model** | ⚠️ Partial — fee-only flag + discretionary/non-discretionary mix | Items 5E + 5F |
| 3 | **How clients pay** | ✅ Complete | Item 5E fee-structure checkboxes + "Other" free text |
| 4 | **Minimum client assets** | ❌ Not yet covered | (needs brochure / website crawl) |
| 5 | **Platform / custodian** | ⚠️ Partial — custody $ amount, no names | Item 9A |

Plus AUM, account counts, location, latest filing date, and other context as side effects.

## Architecture

```
┌──────────────────────────────────────────────┐
│  Streamlit web app (app.py)                  │
│   ├─ Search by name (fuzzy) or CRD           │
│   └─ Render structured fields w/ provenance  │
└──────────────────┬───────────────────────────┘
                   │ in-memory load on first request
                   ▼
┌──────────────────────────────────────────────┐
│  SEC Form ADV Part 1 monthly snapshot        │
│   • 16,779 firms × 448 columns               │
│   • 5 MB zip → 41 MB CSV                     │
│   • Currently pinned to May 2026             │
│   • Downloaded directly from sec.gov         │
└──────────────────────────────────────────────┘
```

No LLM in the deployed path. Streamlit caches the parsed CSV in memory; lookups are O(n) over 16K rows (~50ms).

## Design decision worth flagging

When sources disagree about a firm's attributes (e.g. Part 1 says "fee-only", brochure says "fee-based"), **we surface the disagreement instead of reconciling it**. The disagreement itself is signal — it usually means structural change, regulatory positioning, or affiliated-entity complexity. The schema (`source, observed_at, confidence` per fact) is built for this. See `coverage_report.md` for the worked example with Mercer Global Advisors.

This is consistent with the original brief from the stakeholder: "track sources of all data points, time series for all fields, date-stamp everything."

## Roadmap — what would unlock the missing fields

The free deterministic path has been pushed about as far as it goes for SEC bulk data. Closing the gaps requires per-firm data sources (brochures or websites). Cost estimates are for the 16,779-firm universe.

| Step | What it adds | Cost (universe) | Effort |
|---|---|---|---|
| **Now (deployed)** | Fields #1, #3 + partials on #2, #5 | $0 | Done |
| Schedule D parsing (custody-related items 9C, 7A affiliations, 5K trading) | Sharpens #2, #5 partials | $0 | 1-2 days — the data is in the same SEC bulk archive |
| Brochure text mining (regex + custodian dictionary) | ~95% of #5 names, ~50% of #4 minimums for "simple" firms | $0 LLM, ~10 GB bandwidth (monthly bulk zips) or solve `BRCHR_VRSN_ID` lookup | 2-3 days |
| Website crawl + LLM fallback (only on regex misses) | Closes remaining #4 minimums and #2 prose, full #5 names | ~$50-150 one-time, ~$50/mo refresh | 3-5 days |
| Allow advisors to edit their own data | New "self-reported" source type alongside SEC + brochure | TBD | Future |

## Known data-quality caveats

- **The "Website Address" field is unreliable.** Many firms register dozens of URLs (often from acquired sub-RIAs); the bulk CSV exposes only one, sometimes a LinkedIn or careers page. Will be addressed in the website-crawl step.
- **Brochure data is filed annually around March-April; many firms' brochures lag Part 1 by 6-18 months.** `observed_at` per fact will make this visible.
- **State-registered advisers are not in this dataset** — only SEC-registered. The bulk feed scope is ~16,800 firms, vs. ~30,000+ if state-registered are included.

## Run locally

```bash
uv sync
uv run streamlit run app.py
```

Open http://localhost:8501.

## Deployment

This repo is set up for [Streamlit Community Cloud](https://share.streamlit.io):
1. Sign in with GitHub
2. Create new app → pick this repo
3. Main file path: `app.py`
4. Deploy

The CSV is downloaded at first request (cached in memory). No secrets needed for the current deployed path.

## Files

| File | Purpose |
|---|---|
| `app.py` | The deployed Streamlit app |
| `parse_part1.py` | CLI version of the same Part 1 parser (used during prototyping) |
| `coverage_report.md` | Detailed coverage analysis from the prototype rounds |
| `requirements.txt` | Streamlit Cloud deploy deps |
| `pyproject.toml` / `uv.lock` | Full local dev environment (incl. spike scripts) |
| `spike.py` | Earlier-round PDF extraction experiments (not used by app) |
