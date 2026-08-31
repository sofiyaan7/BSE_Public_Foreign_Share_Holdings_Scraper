# BSE Shareholding Pattern Scraper

Streamlit app that pulls, for a quarter you choose and any list of BSE-listed
companies, the two statements from the bottom of BSE's *Shareholding Pattern*
page:

1. **Statement showing shareholding pattern of the Public shareholder** — every
   category and sub-category, the number of shares, voting rights, locked-in
   shares, dematerialised holdings, plus the named shareholders holding >1%.
2. **Statement showing foreign ownership limits** — board-approved limit and
   limit utilised, for the shareholding date and the previous four quarters.

Results stream into the page as each company is fetched, and the whole run
exports to a multi-sheet Excel workbook.

## Install & run

```bash
pip install -r requirements.txt
streamlit run app.py
```

## Using it

1. **Pick the quarter** in the sidebar (newest first).
2. **Choose what to scrape:**
   - **🌐 All BSE listed companies** — pulls BSE's scrip master (~5,100 active
     equity listings) and scrapes the lot. Optional filters: group (A / B / X /
     …), minimum market cap, ordering, a "stop after N" cap for trial runs, and
     an ETF/mutual-fund-scheme exclusion that is on by default.
   - **✍️ Pick companies manually** — one name or scrip code per line, or a
     CSV/TXT upload. Names go through BSE's own search, so `Reliance Industries`,
     `RELIANCE INDUSTRIES LTD` and `500325` all resolve.
3. Hit **🚀 Start scraping**. The KPI strip, both data tabs, the *Last Company*
   tab and the run log all refresh as results land, with a live rate and ETA.
4. **Export.** One click on **⬇️ Download full Excel workbook** gets the entire
   run — all five sheets (About, Summary, Public Shareholding, Foreign Ownership
   Limits, Run Log) in a single `.xlsx`, with no row cap. The workbook is built
   once when the run finishes, so the button is already live; later clicks and
   reruns reuse it. Individual tables are also there as plain CSVs, plus a
   `.zip` of all of them.

### Local storage

Rows are appended to disk as each company completes — nothing waits in memory
for the run to finish. Two data files carry the quarter in their name:

```
FreeFloatWShares_June2026.csv          public shareholding pattern (the free float)
ForeignOwnershipLimits_June2026.csv    board-approved limit and limit utilised
Summary_June2026.csv                   one row per company
RunLog_June2026.csv                    what resolved, what failed and why
```

Set the destination in the sidebar under **💾 Local storage**; the resolved
absolute path is shown so you always know where files went. `~` and `$HOME` are
expanded, and relative paths are made absolute. A live panel shows each file's
row count and size growing during the run.

If you see *"Can't write to X — saving to /tmp/bse_shp_runs instead"*, the app
could not create files in the folder you asked for and used a temp directory.
Scraping still works and data is still saved. It is expected on a hosted
deployment, where the app directory is read-only — but a temp folder can be
cleared when the app restarts, and you cannot browse it from the browser, so use
the download buttons to keep a copy. Running locally, it usually means a typo or
a folder you don't have permission to write to.

Files are flushed after every company, so an interrupted run loses nothing.
Verified by hard-killing a run mid-flight: the CSV was valid and complete up to
the last finished company, and a full Excel workbook could still be rebuilt from
it. Existing files are never overwritten — a second run on the same quarter
writes `FreeFloatWShares_June2026_2.csv`.

### Scale

Roughly measured against the live API:

| Workers | Rate | Full universe (~4,900 companies) |
|---|---|---|
| 4 | 1.3 co/s | ~64 min |
| 8 (default) | 2.5 co/s | ~34 min |
| 10–12 | 3.1–3.4 co/s | ~25 min |

A full run produces about **165,000 public-shareholding rows**. Rows are
streamed to CSV on disk as they arrive (under `runs/<timestamp>/`) rather than
piling up in the browser session, so memory stays flat and a partial run is
still fully exportable. Above 250k rows the app steers you to the CSV bundle,
since openpyxl gets slow and memory-hungry at that size.

## How it works

`bse_shp.py` calls the same JSON endpoints as the BSE website (the names were
read out of BSE's front-end bundle, so no HTML scraping or browser automation):

| Purpose | Endpoint |
|---|---|
| Public shareholder statement | `Corp_shpSec_SHPPubShold_ng/w?SCRIPCODE=&QtrCode=` |
| Foreign ownership limits | `Corp_shpforeignownership_ng/w?scripcode=&qtrid=` |
| Quarter metadata | `Corp_shpSec_shpqtrinfo_ng/w?scripcode=&qtrcode=` |
| Name → scrip code | `PeerSmartSearch/w?Type=SS&text=` |
| Every listed company | `ListofScripData_new/w?segment=Equity&status=Active` |

Quarters are addressed by BSE's `qtrid`. It advances by exactly one per calendar
quarter from a verified anchor (June 2023 = `118.00`), which is how the sidebar
dropdown is built.

## Troubleshooting

**Everything fails with "BSE bounced the request" / 🚫 BLOCKED.** BSE blocks
requests by IP, and hosted platforms (Streamlit Community Cloud included) sit
in datacenter ranges it commonly refuses. The scraper detects this and says so
rather than failing obscurely. Use **🩺 Connection check** in the sidebar to
confirm. There is no code-side fix — run it locally, or deploy somewhere with an
Indian egress IP.

**Nothing gets written / PermissionError on a hosted deployment.** Streamlit
Cloud mounts the repo read-only, so `./runs` cannot be created there. The app
now probes for a writable location and falls back to the system temp directory;
the export panel shows the path in use.

**A run seems to hang after finishing / no full Excel export appears.** Fixed.
Two separate causes. The workbook was regenerated on every rerun (~12 s per
interaction at 50k rows), and the naive pandas-to-openpyxl path needed about
**1.8 GB** of memory for a full-universe run — over Streamlit Cloud's ~1 GB
limit, so the app was killed rather than producing a file. The workbook is now
streamed row by row:

| Approach | Time | Peak memory |
|---|---|---|
| pandas + openpyxl (old) | 42 s | 1,762 MB — OOM on Streamlit Cloud |
| streamed openpyxl (now) | 27 s | **86 MB** |

It is built once when the run ends and cached, so downloading is a single click
and no row cap is needed. Tables larger than Excel's 1,048,576-row sheet limit
are split across numbered sheets automatically.

## Notes

- **Statuses**: `OK` (both statements), `PARTIAL` (one of the two is empty —
  commonly a company that has not disclosed foreign ownership limits),
  `NO DATA` (nothing filed for that quarter), `NOT FOUND` (name did not
  resolve), `ERROR` (network/parse failure). Nothing aborts the run; failures
  land in the run log.
- Foreign ownership limits only exist for recent years. Older quarters return
  the public statement with `PARTIAL` status.
- Requests are throttled (0.35 s default, adjustable) with automatic retries on
  429/5xx. Please keep the pause in place for large lists.
- Redirects are disabled on every API call. A request BSE refuses is bounced to
  `/error_Bse.html`, and following that bounce looped ~30 times per request
  before failing — so a blocked host was ~60x slower to report an error than it
  needed to be.
- Scraping runs across a thread pool, one `requests.Session` per worker.
  Sessions aren't thread-safe, so they're thread-local.
- BSE's equity segment also lists ~260 ETFs and mutual-fund schemes. The
  exclusion checkbox drops them by name; it is deliberately conservative, so
  real companies such as *SBI Funds Management Ltd* are kept.
- Interrupting a run (Streamlit's **Stop**) closes the CSVs cleanly — whatever
  was scraped stays on disk and remains downloadable.
- `bse_shp.py` has no Streamlit dependency, so it can be imported and used for
  batch jobs on its own:

  ```python
  import bse_shp as b
  s = b.make_session()
  q = b.quarter_choices()[0]                     # latest filed quarter
  universe = b.filter_universe(b.list_all_companies(s), exclude_funds=True)
  for res in b.scrape_many(universe, q["label"], q["qtrid"], workers=8):
      ...                                        # CompanyResult per company
  ```
