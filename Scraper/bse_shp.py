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

import csv
import io
import os
import re
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import date, datetime

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


class BSERefused(RuntimeError):
    """BSE answered, but not with data.

    A refused request is bounced to /error_Bse.html. Chasing that redirect
    loops ~30 times before failing, so redirects are disabled and the bounce is
    reported as what it is. The usual cause on a hosted deployment is BSE
    blocking the server's IP.
    """


def _get_json(session, path, params, timeout=30):
    r = session.get(f"{API}/{path}", params=params, timeout=timeout,
                    allow_redirects=False)
    if r.is_redirect or r.is_permanent_redirect:
        raise BSERefused(
            f"BSE bounced the request to {r.headers.get('location', '?')} "
            f"(HTTP {r.status_code}). On cloud hosting this usually means BSE "
            f"is blocking the server's IP address.")
    if r.status_code in (401, 403, 429):
        raise BSERefused(
            f"BSE returned HTTP {r.status_code} — the request was rejected"
            + (" (rate limited; lower the worker count)"
               if r.status_code == 429 else
               " (IP or headers refused)"))
    r.raise_for_status()
    body = r.text.strip()
    if not body:
        return {}
    ctype = (r.headers.get("content-type") or "").lower()
    if not body.startswith(("[", "{")) or "html" in ctype:
        raise BSERefused(
            f"BSE returned {ctype or 'non-JSON'} instead of data "
            f"({body[:80]!r})")
    return r.json()


def check_connection(session: requests.Session) -> dict:
    """Can we actually reach BSE from here? Used by the app's diagnostics."""
    out = {"website": None, "api": None, "ok": False, "detail": ""}
    try:
        r = session.get("https://www.bseindia.com/", timeout=20,
                        allow_redirects=False)
        out["website"] = r.status_code
    except requests.RequestException as exc:
        out["detail"] = f"cannot reach bseindia.com: {exc}"
        return out
    try:
        d = _get_json(session, "Corp_shpSec_shpqtrinfo_ng/w",
                      {"scripcode": 500325, "qtrcode": quarter_id("June", 2023)})
        out["api"] = 200
        rows = d.get("table1") or d.get("Table1") or []
        out["ok"] = bool(rows)
        out["detail"] = ("BSE is reachable and returning data."
                         if rows else "API answered but sent no rows.")
    except BSERefused as exc:
        out["detail"] = str(exc)
    except requests.RequestException as exc:
        out["detail"] = f"API unreachable: {exc}"
    except ValueError as exc:
        out["detail"] = f"API sent unparseable data: {exc}"
    return out


# ------------------------------------------------------------------- lookup ---
_LICLICK = re.compile(r"liclick\('(\d+)','([^']*)'\)")


def search_company(session: requests.Session, text: str) -> list[dict]:
    """Resolve a company name (or scrip code) to candidate scrips."""
    text = (text or "").strip()
    if not text:
        return []
    r = session.get(f"{API}/PeerSmartSearch/w",
                    params={"Type": "SS", "text": text}, timeout=25,
                    allow_redirects=False)
    if r.is_redirect:
        raise BSERefused("BSE bounced the company search — server IP likely "
                         "blocked.")
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
    input_token: str = ""

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
        except BSERefused as exc:
            return CompanyResult(scripcode="", name=str(token_or_resolved),
                                 quarter=quarter_label, qtrid=qtrid,
                                 status="BLOCKED", message=str(exc),
                                 input_token=str(token_or_resolved))
        except (LookupError, ValueError) as exc:
            return CompanyResult(scripcode="", name=str(token_or_resolved),
                                 quarter=quarter_label, qtrid=qtrid,
                                 status="NOT FOUND", message=str(exc),
                                 input_token=str(token_or_resolved))
        except requests.RequestException as exc:
            return CompanyResult(scripcode="", name=str(token_or_resolved),
                                 quarter=quarter_label, qtrid=qtrid,
                                 status="ERROR", message=f"lookup failed: {exc}",
                                 input_token=str(token_or_resolved))

    token = (token_or_resolved if isinstance(token_or_resolved, str)
             else info.get("name") or str(info.get("scripcode", "")))
    res = CompanyResult(scripcode=str(info["scripcode"]), name=info["name"],
                        quarter=quarter_label, qtrid=qtrid, input_token=token)
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
    except BSERefused as exc:
        res.status = "BLOCKED"
        res.message = str(exc)
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


# ------------------------------------------------- the whole BSE universe -----
SCRIP_LIST_COLUMNS = {
    "SCRIP_CD": "scripcode", "Scrip_Name": "name", "scrip_id": "ticker",
    "GROUP": "group", "ISIN_NUMBER": "isin", "FACE_VALUE": "face_value",
    "Mktcap": "mktcap", "Status": "status", "Segment": "segment",
}


def list_all_companies(session: requests.Session, group: str = "",
                       status: str = "Active", segment: str = "Equity",
                       name_like: str = "") -> list[dict]:
    """Every BSE-listed company in a segment — the full scrip master."""
    d = _get_json(session, "ListofScripData_new/w",
                  {"Group": group, "Scripcode": "", "segment": segment,
                   "status": status, "scripName": name_like}, timeout=120)
    rows = d if isinstance(d, list) else (d.get("Table") or [])
    out = []
    for raw in rows:
        c = {dst: raw.get(src) for src, dst in SCRIP_LIST_COLUMNS.items()}
        if not c["scripcode"]:
            continue
        c["scripcode"] = str(c["scripcode"]).strip()
        c["name"] = (c["name"] or "").strip()
        try:
            c["mktcap_cr"] = float(c["mktcap"]) if c["mktcap"] else None
        except (TypeError, ValueError):
            c["mktcap_cr"] = None
        out.append(c)
    return out


# BSE's equity segment also lists ETFs and mutual-fund schemes. They file no
# meaningful shareholding pattern, so a full-universe run can skip them.
_FUND_MARKERS = (
    " ETF", "ETF ", "MUTUAL FUND", "SEGREGATED PORTFOLIO", "DIVIDEND PLAN",
    "GROWTH PLAN", "INDEX FUND", "BHARAT BOND", "IDCW", "REINVESTMENT",
    "-  SEGREGATED", "LIQUID FUND", "GILT FUND", "ARBITRAGE FUND",
    "DIRECT PLAN", "REGULAR PLAN", "PLAN-GROWTH", "PLAN - GROWTH",
    "DIRECT GROWTH", "REGULAR GROWTH", "LONG-SHORT FUND", "LONG- SHORT FUND",
    "EXCHANGE TRADED FUND",
)


def looks_like_fund(name: str) -> bool:
    n = (name or "").upper().strip()
    # "…LogisticsETF" has no separator before the suffix
    return n.endswith("ETF") or any(m in n for m in _FUND_MARKERS)


def filter_universe(companies: list[dict], groups: list[str] | None = None,
                    min_mktcap_cr: float | None = None,
                    sort_by: str = "name", limit: int | None = None,
                    exclude_funds: bool = False) -> list[dict]:
    """Narrow / order the universe before a run."""
    sel = list(companies)
    if exclude_funds:
        sel = [c for c in sel if not looks_like_fund(c.get("name", ""))]
    if groups:
        want = {g.upper() for g in groups}
        sel = [c for c in sel if (c.get("group") or "").upper() in want]
    if min_mktcap_cr is not None:
        sel = [c for c in sel
               if c.get("mktcap_cr") is not None and c["mktcap_cr"] >= min_mktcap_cr]
    if sort_by == "mktcap":
        sel.sort(key=lambda c: (c.get("mktcap_cr") is None,
                                -(c.get("mktcap_cr") or 0)))
    elif sort_by == "scripcode":
        sel.sort(key=lambda c: c["scripcode"])
    else:
        sel.sort(key=lambda c: c["name"].upper())
    return sel[:limit] if limit else sel


# ----------------------------------------------------- concurrent scraping ----
_local = threading.local()


def thread_session() -> requests.Session:
    """One requests.Session per worker thread (Sessions aren't thread-safe)."""
    s = getattr(_local, "session", None)
    if s is None:
        s = _local.session = make_session()
    return s


def scrape_many(companies, quarter_label: str, qtrid: str, workers: int = 8,
                keep_zero_rows: bool = True, pause: float = 0.0,
                should_stop=None):
    """Scrape many companies concurrently, yielding each CompanyResult as it lands.

    `companies` may hold plain tokens (names / codes) or already-resolved dicts
    with `scripcode` + `name`. Results arrive out of order — that is the point.
    """
    def work(item):
        return scrape_company(thread_session(), item, quarter_label, qtrid,
                              keep_zero_rows=keep_zero_rows, pause=pause)

    ex = ThreadPoolExecutor(max_workers=max(1, int(workers)))
    try:
        futures = {ex.submit(work, c): c for c in companies}
        for fut in as_completed(futures):
            yield fut.result()
            if should_stop is not None and should_stop():
                break
    finally:
        # don't block the UI waiting on in-flight requests if we bail out early
        ex.shutdown(wait=False, cancel_futures=True)


# -------------------------------------------------- disk-backed run writer ----
PUBLIC_HEADER = _ID_COLS + ["Row Type"] + list(PUBLIC_COLUMNS.values())
FOREIGN_HEADER = _ID_COLS + list(FOREIGN_COLUMNS.values())
LOG_HEADER = ["#", "Input", "Scrip Code", "Company", "Quarter", "Group",
              "Public Rows", "Foreign Rows", "Status", "Note"]
SUMMARY_HEADER = ["Scrip Code", "Company", "Quarter", "Total Public Shares Held",
                  "Public Holding % (A+B+C2)", "Public Rows",
                  "Foreign Ownership Rows", "Status", "Note"]


def pick_run_dir(preferred: str | None = None) -> str:
    """A directory we can actually write to.

    Streamlit Cloud mounts the repo read-only, so writing `./runs` next to the
    app raises PermissionError and kills the run before it starts. Fall back to
    the system temp directory.
    """
    import tempfile

    candidates = [preferred] if preferred else []
    candidates.append(os.path.join(tempfile.gettempdir(), "bse_shp_runs"))
    for cand in candidates:
        if not cand:
            continue
        try:
            os.makedirs(cand, exist_ok=True)
            probe = os.path.join(cand, ".write_test")
            with open(probe, "w") as fh:
                fh.write("ok")
            os.remove(probe)
            return cand
        except OSError:
            continue
    return tempfile.mkdtemp(prefix="bse_shp_")


class RunWriter:
    """Streams scraped rows straight to CSV.

    A full-universe run is ~140k public rows; holding that in Streamlit's
    session state is asking for trouble, so rows go to disk as they arrive and
    only counters plus a small rolling preview stay in memory.
    """

    def __init__(self, out_dir: str, quarter: str, qtrid: str,
                 preview_cap: int = 600):
        self.dir = out_dir
        os.makedirs(self.dir, exist_ok=True)
        self.quarter, self.qtrid = quarter, qtrid
        self.preview_cap = preview_cap
        self.paths = {n: os.path.join(self.dir, f"{n}.csv")
                      for n in ("public", "foreign", "log", "summary")}

        self._fh, self._w = {}, {}
        for name, header in (("public", PUBLIC_HEADER), ("foreign", FOREIGN_HEADER),
                             ("log", LOG_HEADER), ("summary", SUMMARY_HEADER)):
            fh = open(self.paths[name], "w", newline="", encoding="utf-8-sig")
            w = csv.DictWriter(fh, fieldnames=header, extrasaction="ignore")
            w.writeheader()
            self._fh[name], self._w[name] = fh, w

        self.companies = 0
        self.public_rows = 0
        self.foreign_rows = 0
        self.status_counts: dict[str, int] = {}
        self.preview_public: list[dict] = []
        self.preview_foreign: list[dict] = []
        self.log: list[dict] = []

    def add(self, res: CompanyResult, source_input: str = "",
            group: str = "") -> None:
        self.companies += 1
        for row in res.public_rows:
            self._w["public"].writerow(row)
        for row in res.foreign_rows:
            self._w["foreign"].writerow(row)
        self.public_rows += len(res.public_rows)
        self.foreign_rows += len(res.foreign_rows)
        self.status_counts[res.status] = self.status_counts.get(res.status, 0) + 1

        log_row = {"#": self.companies,
                   "Input": source_input or res.input_token or res.name,
                   "Scrip Code": res.scripcode, "Company": res.name,
                   "Quarter": res.quarter, "Group": group,
                   "Public Rows": len(res.public_rows),
                   "Foreign Rows": len(res.foreign_rows),
                   "Status": res.status, "Note": res.message}
        self._w["log"].writerow(log_row)
        self.log.append(log_row)
        self._w["summary"].writerow(summary_rows([res])[0])

        if res.public_rows:
            self.preview_public = (self.preview_public + res.public_rows)[-self.preview_cap:]
        if res.foreign_rows:
            self.preview_foreign = (self.preview_foreign + res.foreign_rows)[-self.preview_cap:]

        if self.companies % 20 == 0:
            self.flush()

    def flush(self) -> None:
        for fh in self._fh.values():
            try:
                fh.flush()
            except ValueError:
                pass

    def close(self) -> None:
        for fh in self._fh.values():
            try:
                fh.close()
            except ValueError:
                pass

    # ------------------------------------------------------------- exports ----
    def about_rows(self) -> list[dict]:
        return [
            {"Field": "Source", "Value": "BSE India (bseindia.com)"},
            {"Field": "Quarter", "Value": self.quarter},
            {"Field": "BSE quarter id", "Value": self.qtrid},
            {"Field": "Generated",
             "Value": datetime.now().strftime("%d %b %Y %H:%M:%S")},
            {"Field": "Companies scraped", "Value": self.companies},
            {"Field": "Public shareholding rows", "Value": self.public_rows},
            {"Field": "Foreign ownership rows", "Value": self.foreign_rows},
            {"Field": "Status breakdown",
             "Value": ", ".join(f"{k}: {v}" for k, v in
                                sorted(self.status_counts.items())) or "—"},
        ]

    def zip_bytes(self) -> bytes:
        """Every sheet as a CSV inside one zip — safe at any size."""
        self.flush()
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            about = io.StringIO()
            aw = csv.DictWriter(about, fieldnames=["Field", "Value"])
            aw.writeheader()
            aw.writerows(self.about_rows())
            z.writestr("00_about.csv", about.getvalue())
            for label, name in (("01_summary", "summary"),
                                ("02_public_shareholding", "public"),
                                ("03_foreign_ownership_limits", "foreign"),
                                ("04_run_log", "log")):
                if os.path.exists(self.paths[name]):
                    z.write(self.paths[name], f"{label}.csv")
        return buf.getvalue()

    CSV_LABELS = {"public": "Public Shareholding", "foreign": "Foreign Ownership Limits",
                  "summary": "Summary", "log": "Run Log"}

    def csv_bytes(self, name: str) -> bytes:
        """One table, exactly as written, as a plain CSV download."""
        self.flush()
        path = self.paths[name]
        if not os.path.exists(path):
            return b""
        with open(path, "rb") as fh:
            return fh.read()

    def csv_size(self, name: str) -> int:
        path = self.paths.get(name)
        return os.path.getsize(path) if path and os.path.exists(path) else 0

    def row_count(self, name: str) -> int:
        return {"public": self.public_rows, "foreign": self.foreign_rows,
                "summary": self.companies, "log": self.companies}.get(name, 0)

    def excel_bytes(self, max_rows: int = 400_000) -> bytes:
        """Multi-sheet .xlsx read back off the CSVs."""
        import pandas as pd

        self.flush()

        def read(name):
            path = self.paths[name]
            if not os.path.exists(path):
                return pd.DataFrame({"Note": ["no data"]})
            df = pd.read_csv(path, dtype={"Scrip Code": "Int64"},
                             nrows=max_rows, low_memory=False)
            return df if not df.empty else pd.DataFrame({"Note": ["no data"]})

        buf = io.BytesIO()
        with pd.ExcelWriter(buf, engine="openpyxl") as xl:
            pd.DataFrame(self.about_rows()).to_excel(
                xl, sheet_name="About", index=False)
            read("summary").to_excel(xl, sheet_name="Summary", index=False)
            read("public").to_excel(xl, sheet_name="Public Shareholding",
                                    index=False)
            read("foreign").to_excel(xl, sheet_name="Foreign Ownership Limits",
                                     index=False)
            read("log").to_excel(xl, sheet_name="Run Log", index=False)
            for ws in xl.book.worksheets:
                ws.freeze_panes = "A2"
                for cells in ws.iter_cols(min_row=1, max_row=1):
                    col = cells[0]
                    width = max(len(str(col.value or "")) + 2, 12)
                    ws.column_dimensions[col.column_letter].width = min(width, 55)
        return buf.getvalue()
