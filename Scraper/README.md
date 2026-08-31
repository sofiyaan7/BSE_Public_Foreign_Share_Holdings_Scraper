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
2. **List your companies** — one name or scrip code per line, or upload a
   CSV/TXT. Names go through BSE's own search, so `Reliance Industries`,
   `RELIANCE INDUSTRIES LTD` and `500325` all resolve.
3. Hit **🚀 Start scraping**. The KPI strip, the two data tabs, the
   *Last Company* tab and the run log all update after every company, so you
   can watch it work.
4. Hit **⬇️ Export to Excel**.

### Workbook sheets

| Sheet | Contents |
|---|---|
| About | source, quarter, BSE quarter id, generated timestamp, row counts |
| Summary | one row per company: total public shares held, public holding %, status |
| Public Shareholding | the full public shareholder statement, all companies stacked |
| Foreign Ownership Limits | board-approved limit and limit utilised per period |
| Run Log | what was requested, what resolved, and every failure with its reason |

The `Row Type` column in *Public Shareholding* tells you what a row is —
`Category header`, `Line item`, `Shareholder detail` (a named >1% holder) or
`Sub-total` — so you can filter to whichever level you want without
double-counting.

## How it works

`bse_shp.py` calls the same JSON endpoints as the BSE website (the names were
read out of BSE's front-end bundle, so no HTML scraping or browser automation):

| Purpose | Endpoint |
|---|---|
| Public shareholder statement | `Corp_shpSec_SHPPubShold_ng/w?SCRIPCODE=&QtrCode=` |
| Foreign ownership limits | `Corp_shpforeignownership_ng/w?scripcode=&qtrid=` |
| Quarter metadata | `Corp_shpSec_shpqtrinfo_ng/w?scripcode=&qtrcode=` |
| Name → scrip code | `PeerSmartSearch/w?Type=SS&text=` |

Quarters are addressed by BSE's `qtrid`. It advances by exactly one per calendar
quarter from a verified anchor (June 2023 = `118.00`), which is how the sidebar
dropdown is built.

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
- `bse_shp.py` has no Streamlit dependency, so it can be imported and used for
  batch jobs on its own.
