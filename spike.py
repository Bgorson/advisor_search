"""Phase 0 spike: validate brochure-first extraction.

Goal: find where this falls apart before building any pipeline.
Prints what worked and what didn't for each firm.
"""

from __future__ import annotations

import os
import sys
import time
from io import BytesIO

import httpx
import pdfplumber
import pypdf
from anthropic import Anthropic
from dotenv import load_dotenv
from pydantic import BaseModel, Field

load_dotenv()
API_KEY = os.environ.get("SYL_ANTHROPIC_API_KEY")
if not API_KEY:
    sys.exit("SYL_ANTHROPIC_API_KEY missing — set it in .env")

client = Anthropic(api_key=API_KEY)

FIRMS = [
    ("Ritholtz Wealth Management", "154051"),
    ("Creative Planning", "128170"),
    ("Fisher Investments", "109544"),
    ("Edelman Financial Engines", "287770"),
    ("Mercer Global Advisors", "117344"),
]

BROCHURE_URL = "https://reports.adviserinfo.sec.gov/reports/ADV/{crd}/PDF/{crd}.pdf"
USER_AGENT = "syl-proto/0.1 research spike (contact: brandon.gorson@gmail.com)"


class FieldAnswer(BaseModel):
    value: str = Field(description="Answer as stated in the brochure, or 'NOT FOUND'.")
    confidence: str = Field(description="high | medium | low")
    quote: str = Field(description="Verbatim quote (≤200 chars), or empty if NOT FOUND.")


class Extraction(BaseModel):
    services_offered: FieldAnswer = Field(
        description="Advisory services the firm provides (wealth management, financial planning, retirement advice, tax planning, etc.)."
    )
    engagement_model: FieldAnswer = Field(
        description="How the firm structures client relationships: fee-only vs commission-based vs fee-based; ongoing wealth management vs project-based vs hourly; discretionary vs non-discretionary."
    )
    how_clients_pay: FieldAnswer = Field(
        description="Compensation method: percentage of AUM, hourly, fixed/flat fees, performance-based, commissions, subscription, or combinations."
    )
    minimum_client_assets: FieldAnswer = Field(
        description="Minimum investable assets required to take on a new client. State dollar amount or 'no minimum' if explicit."
    )
    platform_affiliation: FieldAnswer = Field(
        description="Custodians, broker-dealers, or technology platforms the firm uses or is affiliated with (Schwab, Fidelity, Pershing, LPL, TD Ameritrade, Envestnet, Orion, etc.)."
    )


PROMPT = """You are extracting structured information from a Form ADV filing by a registered investment adviser.

For each field, return:
- value: the answer as stated, or "NOT FOUND" if the brochure doesn't address it.
- confidence: "high" (clearly stated), "medium" (inferred from context), or "low" (ambiguous).
- quote: a verbatim quote (≤200 characters) supporting the answer, or empty string if NOT FOUND.

Be conservative. Do NOT infer beyond what the document says. If the document only contains Form ADV Part 1 (the structured form) without a Part 2A brochure, many of these fields may be NOT FOUND — that's expected and useful information.

DOCUMENT TEXT:
{text}"""


def fetch_brochure(crd: str) -> bytes | None:
    url = BROCHURE_URL.format(crd=crd)
    try:
        r = httpx.get(url, headers={"User-Agent": USER_AGENT}, timeout=30, follow_redirects=True)
    except Exception as e:
        print(f"   fetch error: {type(e).__name__}: {e}")
        return None
    if r.status_code != 200:
        print(f"   HTTP {r.status_code} from {url}")
        return None
    if not r.content.startswith(b"%PDF"):
        print(f"   response is not a PDF (first bytes: {r.content[:20]!r})")
        return None
    return r.content


def extract_text(pdf_bytes: bytes) -> tuple[str, str]:
    """Returns (text, extractor_used)."""
    try:
        reader = pypdf.PdfReader(BytesIO(pdf_bytes))
        text = "\n".join(p.extract_text() or "" for p in reader.pages)
        if len(text) >= 1000:
            return text, "pypdf"
        print(f"   pypdf got only {len(text)} chars — trying pdfplumber")
    except Exception as e:
        print(f"   pypdf failed: {type(e).__name__}: {e} — trying pdfplumber")

    with pdfplumber.open(BytesIO(pdf_bytes)) as pdf:
        text = "\n".join((page.extract_text() or "") for page in pdf.pages)
    return text, "pdfplumber"


PART_2A_MARKERS = (
    "FORM ADV PART 2A",
    "Form ADV Part 2A",
    "Part 2A of Form ADV",
    "PART 2A OF FORM ADV",
    "Item 1 - Cover Page",
    "ITEM 1 - COVER PAGE",
    "Brochure\n",
)


MAX_PROMPT_CHARS = 180_000  # ~45K tokens — under tier-1 Haiku 50K ITPM limit


def slice_to_part_2a(text: str) -> tuple[str, str | None]:
    """Find the LAST occurrence of a Part 2A marker and slice from there.

    Brochures sit at the end of consolidated ADV PDFs; early occurrences of
    "Part 2A" are references in Part 1 / Schedule D headers. After slicing,
    cap to the last MAX_PROMPT_CHARS to stay under rate limits.
    """
    latest_idx = -1
    latest_marker: str | None = None
    for marker in PART_2A_MARKERS:
        idx = text.rfind(marker)
        if idx > latest_idx:
            latest_idx = idx
            latest_marker = marker
    if latest_idx == -1:
        sliced = text
    else:
        sliced = text[latest_idx:]
    if len(sliced) > MAX_PROMPT_CHARS:
        sliced = sliced[-MAX_PROMPT_CHARS:]
    return sliced, latest_marker


def extract_fields(text: str) -> Extraction:
    response = client.messages.parse(
        model="claude-haiku-4-5",
        max_tokens=4000,
        thinking={"type": "disabled"},
        messages=[{"role": "user", "content": PROMPT.format(text=text)}],
        output_format=Extraction,
    )
    return response.parsed_output


def main() -> None:
    for name, crd in FIRMS:
        print(f"\n{'=' * 72}")
        print(f"{name}  (CRD {crd})")
        print("=" * 72)

        print("→ fetching from IAPD")
        pdf = fetch_brochure(crd)
        if pdf is None:
            continue
        print(f"   got {len(pdf):,} bytes")

        print("→ extracting text")
        try:
            text, extractor = extract_text(pdf)
        except Exception as e:
            print(f"   PDF extraction failed: {type(e).__name__}: {e}")
            continue
        print(f"   {extractor}: {len(text):,} chars")

        if len(text) < 1000:
            print("   ⚠️  very little text — likely scanned/image PDF. Skipping LLM.")
            continue

        sliced, marker = slice_to_part_2a(text)
        if marker is None:
            print("   ⚠️  no Part 2A marker found — sending full text (Part 1 only?)")
        else:
            print(f"   sliced at \"{marker.strip()}\": {len(sliced):,} chars (was {len(text):,})")

        print("→ extracting fields with Haiku 4.5")
        try:
            result = extract_fields(sliced)
        except Exception as e:
            print(f"   LLM extraction failed: {type(e).__name__}: {e}")
            continue

        for field_name in (
            "services_offered",
            "engagement_model",
            "how_clients_pay",
            "minimum_client_assets",
            "platform_affiliation",
        ):
            f: FieldAnswer = getattr(result, field_name)
            print(f"\n  [{field_name}]  confidence={f.confidence}")
            print(f"   value: {f.value}")
            if f.quote:
                print(f'   quote: "{f.quote}"')

        time.sleep(1)


if __name__ == "__main__":
    main()
