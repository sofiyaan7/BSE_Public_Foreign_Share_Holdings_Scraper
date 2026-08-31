"""BSE shareholding-pattern scraper.

Pulls, for a chosen quarter and a list of companies, the two statements that sit at
the bottom of BSE's "Shareholding Pattern" page:

  * Statement showing shareholding pattern of the Public shareholder
  * Statement showing foreign ownership limits

The endpoints below were read out of BSE's own front-end bundle, so they are the
same calls the website itself makes:

  Corp_shpSec_SHPPubShold_ng   public shareholder statement
  Corp_shpforeignownership_ng  foreign ownership limits
  Corp_shpSec_shpqtrinfo_ng    quarter metadata (name / start / end date)
  PeerSmartSearch              company name -> scrip code
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from datetime import date

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

API = "https://api.bseindia.com/BseIndiaAPI/api"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.bseindia.com/",
    "Origin": "https://www.bseindia.com",
}

# ---------------------------------------------------------------- quarters ----
# Verified against the API: June 2023 is qtrid 118.00 and the id advances by
# exactly one per calendar quarter (March 2026 = 129.00, June 2026 = 130.00).
_BASE_YEAR = 2023
_MONTH_OFFSET = {"March": 117, "June": 118, "September": 119, "December": 120}
_MONTH_END = {"March": 3, "June": 6, "September": 9, "December": 12}


def quarter_id(month: str, year: int) -> str:
    """qtrid string BSE expects, e.g. ("June", 2026) -> '130.00'."""
    return f"{_MONTH_OFFSET[month] + 4 * (year - _BASE_YEAR)}.00"


def latest_completed_quarter(today: date | None = None) -> tuple[str, int]:
    """Most recent quarter whose end date has passed."""
    today = today or date.today()
    ended = [
        (m, y)
        for y in (today.year - 1, today.year)
        for m in ("March", "June", "September", "December")
        if date(y, _MONTH_END[m], 28) < today
    ]
    return ended[-1]


def quarter_choices(from_year: int = 2016, today: date | None = None) -> list[dict]:
    """Selectable quarters, newest first."""
    last_month, last_year = latest_completed_quarter(today)
    out = []
    for y in range(from_year, last_year + 1):
        for m in ("March", "June", "September", "December"):
            if y == last_year and _MONTH_END[m] > _MONTH_END[last_month]:
                continue
            out.append({"label": f"{m} {y}", "month": m, "year": y,
                        "qtrid": quarter_id(m, y)})
    out.reverse()
    return out


# ------------------------------------------------------------------ session ---
def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update(_HEADERS)
    retry = Retry(
        total=3, connect=3, read=3, backoff_factor=0.8,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET"}),
    )
    s.mount("https://", HTTPAdapter(max_retries=retry, pool_maxsize=10))
    try:  # warm-up so BSE hands us its cookies
        s.get("https://www.bseindia.com/", timeout=20)
    except requests.RequestException:
        pass
    return s


def _get_json(session, path, params, timeout=30):
    r = session.get(f"{API}/{path}", params=params, timeout=timeout)
    r.raise_for_status()
    if not r.text.strip():
        return {}
    return r.json()


# ------------------------------------------------------------------- lookup ---
_LICLICK = re.compile(r"liclick\('(\d+)','([^']*)'\)")


def search_company(session: requests.Session, text: str) -> list[dict]:
    """Resolve a company name (or scrip code) to candidate scrips."""
    text = (text or "").strip()
    if not text:
        return []
    r = session.get(f"{API}/PeerSmartSearch/w",
                    params={"Type": "SS", "text": text}, timeout=25)
    r.raise_for_status()
    seen, out = set(), []
    for code, name in _LICLICK.findall(r.text):
        if code not in seen:
            seen.add(code)
            out.append({"scripcode": code, "name": name.strip()})
    return out


def resolve_company(session: requests.Session, token: str) -> dict:
    """One input line -> {scripcode, name, matched_on, alternatives}."""
    token = (token or "").strip()
    if not token:
        raise ValueError("empty company")
    hits = search_company(session, token)
    if not hits:
        if token.isdigit():
            return {"scripcode": token, "name": f"Scrip {token}",
                    "input": token, "exact": False, "alternatives": []}
        raise LookupError(f"no BSE match for {token!r}")
    exact = [h for h in hits if h["name"].upper() == token.upper()
             or h["scripcode"] == token]
    best = exact[0] if exact else hits[0]
    return {
        "scripcode": best["scripcode"],
        "name": best["name"],
        "input": token,
        "exact": bool(exact) or token.isdigit(),
        "alternatives": [h["name"] for h in hits[1:6]],
    }


def quarter_info(session: requests.Session, scripcode, qtrid) -> dict:
    d = _get_json(session, "Corp_shpSec_shpqtrinfo_ng/w",
                  {"scripcode": scripcode, "qtrcode": qtrid})
    rows = d.get("table1") or d.get("Table1") or []
    return rows[0] if rows else {}


# ------------------------------------------------------------ row shaping ----
PUBLIC_COLUMNS = {
    "Fld_Code": "Code",
    "Fld_ShortCatg": "Category",
    "Fld_SubCategory": "Sub Category",
    "Fld_Level": "Particulars",
    "Fld_ShareHolderName": "Shareholder Name",
    "Fld_NoOfShareHolders": "No. of Shareholders",
    "Fld_NoOfFullyPaidShares": "No. of Fully Paid Up Shares",
    "Fld_NoOfPartlyPaidShares": "No. of Partly Paid Up Shares",
    "Fld_NoOfDRShares": "No. of Shares Underlying DRs",
    "Fld_TotalNoOfShares": "Total No. of Shares Held",
    "Fld_TotalPercentageOf_A_B_C2": "% of Total Shares (A+B+C2)",
    "Fld_NoOfVotingRightsClassX": "Voting Rights - Class X",
    "Fld_NoOfVotingRightsClassY": "Voting Rights - Class Y",
    "Fld_TotalNoOfVotingRights": "Voting Rights - Total",
    "Fld_TotalVotingRightsPercent": "Voting Rights - % of Total",
    "Fld_NoOfConvertibleShares": "No. of Convertible Securities",
    "Fld_NoOfWarrants": "No. of Warrants",
    "Fld_NoOfESOP": "No. of ESOPs",
    "Fld_TotalNoOfCS_Warrants": "Total Convertible Securities + Warrants",
    "Fld_TotaldilutedShares": "Total Diluted Shares",
    "Fld_PercentAfterFullConversion": "% Assuming Full Conversion",
    "Fld_NoOfLockedInShares": "No. of Locked-in Shares",
    "Fld_LockedInSharesPercent": "Locked-in Shares - % of Total",
    "Fld_DematerializedForm": "No. of Shares in Dematerialized Form",
}

FOREIGN_COLUMNS = {
    "Fld_AnnBLabelName": "Particulars",
    "Fld_Boardapprovedlimits": "Board Approved Limit (%)",
    "Fld_Limitsutilized": "Limits Utilized (%)",
}

_ID_COLS = ["Scrip Code", "Company", "Quarter", "Quarter ID"]


def _row_kind(raw: dict) -> str:
    level = (raw.get("Fld_Level") or "").strip()
    code = (raw.get("Fld_Code") or "").strip()
    if raw.get("Fld_ShareHolderName"):
        return "Shareholder detail"
    if level.startswith("Sub Total") or level.startswith("B=") or code.startswith("STB"):
        return "Sub-total"
    if not level:
        return "Category header"
    return "Line item"


def _stamp(scripcode, name, quarter, qtrid) -> dict:
    return {"Scrip Code": int(scripcode) if str(scripcode).isdigit() else scripcode,
            "Company": name, "Quarter": quarter, "Quarter ID": qtrid}


def fetch_public_shareholding(session, scripcode, name, quarter, qtrid,
                              keep_zero_rows=True) -> list[dict]:
    """Statement showing shareholding pattern of the Public shareholder."""
    d = _get_json(session, "Corp_shpSec_SHPPubShold_ng/w",
                  {"SCRIPCODE": scripcode, "QtrCode": qtrid})
    rows = d.get("Table1") or []
    long_name = ((d.get("Table2") or [{}])[0] or {}).get("sLongName") or name
    out = []
    for raw in rows:
        kind = _row_kind(raw)
        if not keep_zero_rows and kind == "Category header" \
                and not raw.get("Fld_TotalNoOfShares"):
            continue
        row = _stamp(scripcode, long_name, quarter, qtrid)
        row["Row Type"] = kind
        for src, dst in PUBLIC_COLUMNS.items():
            row[dst] = raw.get(src)
        out.append(row)
    return out


def fetch_foreign_ownership(session, scripcode, name, quarter, qtrid) -> list[dict]:
    """Statement showing foreign ownership limits."""
    d = _get_json(session, "Corp_shpforeignownership_ng/w",
                  {"scripcode": scripcode, "qtrid": qtrid})
    long_name = ((d.get("Table") or [{}])[0] or {}).get("sLongName") or name
    out = []
    for raw in d.get("Table1") or []:
        row = _stamp(scripcode, long_name, quarter, qtrid)
        for src, dst in FOREIGN_COLUMNS.items():
            row[dst] = raw.get(src)
        out.append(row)
    return out


# ------------------------------------------------------------------ scraping --
@dataclass
class CompanyResult:
    scripcode: str
    name: str
    quarter: str
    qtrid: str
    public_rows: list = field(default_factory=list)
    foreign_rows: list = field(default_factory=list)
    status: str = "OK"
    message: str = ""

    @property
    def total_public_shares(self):
        for r in self.public_rows:
            if r.get("Row Type") == "Sub-total" and \
                    str(r.get("Particulars") or "").startswith("B="):
                return r.get("Total No. of Shares Held")
        return None

    @property
    def public_percent(self):
        for r in self.public_rows:
            if r.get("Row Type") == "Sub-total" and \
                    str(r.get("Particulars") or "").startswith("B="):
                return r.get("% of Total Shares (A+B+C2)")
        return None


def scrape_company(session, token_or_resolved, quarter_label, qtrid,
                   keep_zero_rows=True, pause=0.0) -> CompanyResult:
    """Resolve (if needed) and pull both statements for one company."""
    if isinstance(token_or_resolved, dict):
        info = token_or_resolved
    else:
        try:
            info = resolve_company(session, token_or_resolved)
        except (LookupError, ValueError) as exc:
            return CompanyResult(scripcode="", name=str(token_or_resolved),
                                 quarter=quarter_label, qtrid=qtrid,
                                 status="NOT FOUND", message=str(exc))
        except requests.RequestException as exc:
            return CompanyResult(scripcode="", name=str(token_or_resolved),
                                 quarter=quarter_label, qtrid=qtrid,
                                 status="ERROR", message=f"lookup failed: {exc}")

    res = CompanyResult(scripcode=str(info["scripcode"]), name=info["name"],
                        quarter=quarter_label, qtrid=qtrid)
    try:
        res.public_rows = fetch_public_shareholding(
            session, res.scripcode, res.name, quarter_label, qtrid, keep_zero_rows)
        if pause:
            time.sleep(pause)
        res.foreign_rows = fetch_foreign_ownership(
            session, res.scripcode, res.name, quarter_label, qtrid)
        if res.public_rows:
            res.name = res.public_rows[0]["Company"]
        elif res.foreign_rows:
            res.name = res.foreign_rows[0]["Company"]

        if not res.public_rows and not res.foreign_rows:
            res.status = "NO DATA"
            res.message = f"BSE has no filing for {quarter_label}"
        elif not res.public_rows:
            res.status = "PARTIAL"
            res.message = "public shareholder statement empty"
        elif not res.foreign_rows:
            res.status = "PARTIAL"
            res.message = "foreign ownership limits not disclosed"
    except requests.RequestException as exc:
        res.status = "ERROR"
        res.message = f"network: {exc}"
    except ValueError as exc:
        res.status = "ERROR"
        res.message = f"bad response: {exc}"
    return res


def parse_company_input(text: str) -> list[str]:
    """Split a textarea / pasted list into company tokens."""
    tokens, seen = [], set()
    for line in re.split(r"[\n\r]+", text or ""):
        for part in ([line] if "," not in line or line.strip().isdigit()
                     else line.split(",")):
            t = part.strip().strip('"').strip()
            if t and t.lower() not in seen:
                seen.add(t.lower())
                tokens.append(t)
    return tokens


# -------------------------------------------------------------------- export --
def summary_rows(results: list[CompanyResult]) -> list[dict]:
    return [{
        "Scrip Code": r.scripcode, "Company": r.name, "Quarter": r.quarter,
        "Total Public Shares Held": r.total_public_shares,
        "Public Holding % (A+B+C2)": r.public_percent,
        "Public Rows": len(r.public_rows),
        "Foreign Ownership Rows": len(r.foreign_rows),
        "Status": r.status, "Note": r.message,
    } for r in results]


def build_workbook(public_rows, foreign_rows, log_rows, results,
                   quarter, qtrid) -> bytes:
    """Render the scraped data into a multi-sheet .xlsx and return its bytes."""
    import io
    from datetime import datetime

    import pandas as pd

    def frame(rows):
        return pd.DataFrame(rows) if rows else pd.DataFrame({"Note": ["no data"]})

    pub, fo = frame(public_rows), frame(foreign_rows)
    meta = pd.DataFrame({
        "Field": ["Source", "Quarter", "BSE quarter id", "Generated",
                  "Companies requested", "Public rows", "Foreign rows"],
        "Value": ["BSE India (bseindia.com)", quarter, qtrid,
                  datetime.now().strftime("%d %b %Y %H:%M:%S"),
                  len(results), len(public_rows), len(foreign_rows)],
    })

    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as xl:
        meta.to_excel(xl, sheet_name="About", index=False)
        frame(summary_rows(results)).to_excel(xl, sheet_name="Summary", index=False)
        pub.to_excel(xl, sheet_name="Public Shareholding", index=False)
        fo.to_excel(xl, sheet_name="Foreign Ownership Limits", index=False)
        frame(log_rows).to_excel(xl, sheet_name="Run Log", index=False)

        for ws in xl.book.worksheets:
            ws.freeze_panes = "A2"
            for cells in ws.columns:
                width = max((len(str(c.value)) for c in cells
                             if c.value is not None), default=8)
                ws.column_dimensions[cells[0].column_letter].width = min(
                    max(width + 2, 10), 55)
    return buf.getvalue()
