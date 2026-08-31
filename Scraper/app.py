"""Streamlit front-end for the BSE shareholding-pattern scraper.

Two ways to choose what to scrape:
  * the entire BSE listed universe for a quarter, optionally filtered, or
  * a hand-picked list of names / scrip codes.
"""

from __future__ import annotations

import os
import time
from datetime import datetime

import pandas as pd
import streamlit as st

import bse_shp as bse

st.set_page_config(page_title="BSE Shareholding Pattern Scraper",
                   page_icon="📊", layout="wide")

SAMPLE = "Reliance Industries\nTata Consultancy Services\nHDFC Bank\nInfosys"
# Streamlit Cloud mounts the repo read-only, so fall back to a temp dir.
RUNS_DIR = bse.pick_run_dir(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "runs"))
ALL_MODE = "🌐 All BSE listed companies"
MANUAL_MODE = "✍️ Pick companies manually"


# ------------------------------------------------------------------- state ----
for key, val in {"writer": None, "running": False, "done": False,
                 "quarter": "", "elapsed": 0.0, "last": None,
                 "interrupted": False, "xlsx": None, "zipb": None,
                 "conn": None, "xlsx_error": None}.items():
    st.session_state.setdefault(key, val)


def reset_run():
    w = st.session_state.writer
    if w is not None:
        w.close()
    st.session_state.update(writer=None, done=False, elapsed=0.0, last=None,
                            interrupted=False, xlsx=None, zipb=None,
                            xlsx_error=None)


@st.cache_data(ttl=3600, show_spinner=False)
def load_universe(segment: str, status: str) -> list[dict]:
    """The BSE scrip master. Cached for an hour — it barely moves."""
    return bse.list_all_companies(bse.make_session(), segment=segment,
                                  status=status)


# ------------------------------------------------------------------ sidebar ---
st.sidebar.title("📊 BSE SHP Scraper")
st.sidebar.caption("Public shareholding pattern + foreign ownership limits, "
                   "straight from BSE's own APIs.")

quarters = bse.quarter_choices(from_year=2016)
sel_label = st.sidebar.selectbox(
    "Quarter", [q["label"] for q in quarters], index=0,
    help="Newest first. The top entry is the most recent quarter companies "
         "have filed for.")
sel = next(q for q in quarters if q["label"] == sel_label)
st.sidebar.caption(f"BSE quarter id `{sel['qtrid']}`")

st.sidebar.markdown("### What to scrape")
mode = st.sidebar.radio("Universe", [ALL_MODE, MANUAL_MODE],
                        label_visibility="collapsed")

targets: list = []
group_of: dict[str, str] = {}
universe: list[dict] = []

if mode == ALL_MODE:
    try:
        universe = load_universe("Equity", "Active")
    except Exception as exc:  # noqa: BLE001 - shown to the user
        st.sidebar.error(f"Could not load the BSE scrip master: {exc}")
        universe = []

    if universe:
        st.sidebar.success(f"{len(universe):,} active equity companies listed")
        all_groups = sorted({(c.get("group") or "?") for c in universe})
        with st.sidebar.expander("🔍 Narrow it down (optional)", expanded=True):
            groups = st.multiselect(
                "Groups", all_groups, default=[],
                help="Blank = every group. 'A' is the large, liquid end "
                     "(~700 companies); B and X are the long tail.")
            min_mcap = st.number_input(
                "Minimum market cap (₹ crore)", min_value=0.0, value=0.0,
                step=100.0,
                help="0 = no market-cap filter. Companies with no reported "
                     "market cap are dropped when this is above 0.")
            sort_by = st.selectbox("Order", ["mktcap", "name", "scripcode"],
                                   format_func={"mktcap": "Largest first",
                                                "name": "Company name",
                                                "scripcode": "Scrip code"}.get)
            limit = st.number_input(
                "Stop after N companies (0 = all)", min_value=0,
                max_value=len(universe), value=0, step=50,
                help="Handy for a trial run — pair it with 'Largest first'.")
            no_funds = st.checkbox(
                "Exclude ETFs / mutual-fund schemes", value=True,
                help="BSE's equity segment also lists ETFs and fund schemes, "
                     "which file no meaningful shareholding pattern.")

        targets = bse.filter_universe(
            universe, groups=groups or None,
            min_mktcap_cr=min_mcap if min_mcap > 0 else None,
            sort_by=sort_by, limit=int(limit) or None,
            exclude_funds=no_funds)
        if no_funds:
            dropped = sum(1 for c in universe if bse.looks_like_fund(c["name"]))
            if dropped:
                st.sidebar.caption(f"Skipping {dropped:,} ETF / fund listings")
        group_of = {c["scripcode"]: (c.get("group") or "") for c in targets}
else:
    src = st.sidebar.radio("Input", ["Type / paste", "Upload CSV"],
                           horizontal=True, label_visibility="collapsed")
    if src == "Type / paste":
        raw = st.sidebar.text_area(
            "One company name or scrip code per line", value=SAMPLE, height=170,
            help="Names go through BSE's search, so 'Reliance Industries' or "
                 "'500325' both work.")
        targets = bse.parse_company_input(raw)
    else:
        up = st.sidebar.file_uploader("CSV / TXT", type=["csv", "txt"])
        col_hint = st.sidebar.text_input("Column to read (blank = first)", "")
        if up is not None:
            try:
                if up.name.lower().endswith(".txt"):
                    targets = bse.parse_company_input(
                        up.getvalue().decode("utf-8", "ignore"))
                else:
                    df_in = pd.read_csv(up, dtype=str).fillna("")
                    col = col_hint.strip() or df_in.columns[0]
                    if col not in df_in.columns:
                        st.sidebar.error(
                            f"No column {col!r}. Found: {list(df_in.columns)}")
                    else:
                        targets = bse.parse_company_input(
                            "\n".join(df_in[col].tolist()))
            except Exception as exc:  # noqa: BLE001 - shown to the user
                st.sidebar.error(f"Could not read file: {exc}")

st.sidebar.markdown("### 💾 Local storage")
save_dir_in = st.sidebar.text_input(
    "Folder for the CSV files", value=RUNS_DIR,
    help="Rows are appended here as they are scraped, so nothing is held in "
         "memory waiting for the run to finish.")
_asked = bse.normalise_path(save_dir_in or RUNS_DIR)
save_dir = bse.pick_run_dir(save_dir_in or RUNS_DIR)
if save_dir != _asked:
    st.sidebar.warning(
        f"Can't write to `{_asked}` — saving to `{save_dir}` instead. "
        f"Files are still being saved; use the download buttons to keep a copy, "
        f"since a temp folder can be cleared when the app restarts.")
elif save_dir != (save_dir_in or "").strip():
    st.sidebar.caption(f"Saving to `{save_dir}`")
_tag = sel["label"].replace(" ", "")
st.sidebar.caption(
    "Two data files are written continuously:  \n"
    f"`FreeFloatWShares_{_tag}.csv`  \n"
    f"`ForeignOwnershipLimits_{_tag}.csv`")

with st.sidebar.expander("⚙️ Options"):
    workers = st.slider("Parallel workers", 1, 16, 8,
                        help="8 gets through the full BSE list in roughly "
                             "35 minutes. Higher is faster but harder on BSE.")
    pause = st.slider("Pause per request (sec)", 0.0, 2.0, 0.0, 0.05,
                      help="Extra politeness delay inside each worker. Leave "
                           "at 0 when running in parallel.")
    keep_zero = st.checkbox("Keep category header rows", value=True,
                            help="Header rows such as 'Institutions' carry no "
                                 "numbers. Untick for a tighter sheet.")
    preview_rows = st.number_input("Preview rows to show", 10, 2000, 200, 10)
    repaint_every = st.number_input(
        "Refresh tables every N companies", 1, 200, 5, 1,
        help="Small numbers feel livelier; larger numbers keep long runs fast.")

n_targets = len(targets)
if n_targets:
    eta = n_targets / max(workers * 0.3, 0.3) / 60
    st.sidebar.info(f"**{n_targets:,}** companies queued · "
                    f"~{eta:.0f} min at {workers} workers")
else:
    st.sidebar.warning("Nothing queued yet.")

start = st.sidebar.button("🚀 Start scraping", type="primary",
                          use_container_width=True, disabled=not n_targets)
if st.sidebar.button("🧹 Clear results", use_container_width=True):
    reset_run()
    st.rerun()

with st.sidebar.expander("🩺 Connection check"):
    st.caption("If scraping fails everywhere, start here. Hosted deployments "
               "are often blocked by BSE at the IP level.")
    if st.button("Test BSE connection", use_container_width=True):
        with st.spinner("Talking to BSE…"):
            st.session_state.conn = bse.check_connection(bse.make_session())
    c = st.session_state.conn
    if c:
        st.write(f"bseindia.com: `{c['website']}` · API: `{c['api']}`")
        (st.success if c["ok"] else st.error)(c["detail"])

# -------------------------------------------------------------------- header --
st.title("BSE Shareholding Pattern Scraper")
st.markdown(
    "Pulls the **Statement showing shareholding pattern of the Public "
    "shareholder** and the **Statement showing foreign ownership limits** for "
    "the quarter you pick — for every BSE-listed company, or just the ones you "
    "name — then exports the lot to Excel.")

kpi_slot = st.empty()
prog_slot = st.empty()
now_slot = st.empty()
files_slot = st.empty()

tabs = st.tabs(["🧾 Public Shareholding", "🌍 Foreign Ownership Limits",
                "🔎 Last Company", "📋 Run Log", "🌐 Universe"])
with tabs[0]:
    pub_slot = st.empty()
with tabs[1]:
    fo_slot = st.empty()
with tabs[2]:
    last_slot = st.empty()
with tabs[3]:
    log_slot = st.empty()
with tabs[4]:
    if universe:
        st.caption(f"BSE scrip master — {len(universe):,} active equity "
                   f"companies. {n_targets:,} selected for this run.")
        st.dataframe(pd.DataFrame(targets or universe)[
            ["scripcode", "name", "ticker", "group", "isin", "mktcap_cr"]],
            use_container_width=True, hide_index=True, height=430)
    else:
        st.info("Switch to **🌐 All BSE listed companies** to load the "
                "full scrip master.")

dl_slot = st.empty()


# ------------------------------------------------------------------ painting --
def paint_kpis(done_n, total_n):
    w = st.session_state.writer
    sc = w.status_counts if w else {}
    with kpi_slot.container():
        c = st.columns(6)
        c[0].metric("Companies", f"{done_n:,}/{total_n:,}")
        c[1].metric("Complete", f"{sc.get('OK', 0):,}")
        c[2].metric("Partial / none",
                    f"{sc.get('PARTIAL', 0) + sc.get('NO DATA', 0):,}")
        c[3].metric("Failed",
                    f"{sc.get('ERROR', 0) + sc.get('NOT FOUND', 0) + sc.get('BLOCKED', 0):,}")
        c[4].metric("Public rows", f"{(w.public_rows if w else 0):,}")
        c[5].metric("Foreign rows", f"{(w.foreign_rows if w else 0):,}")


def paint_files():
    """Show the CSVs filling up on disk while the run is going."""
    w = st.session_state.writer
    if not w:
        files_slot.empty()
        return
    rows = w.file_status()
    with files_slot.container():
        st.caption(f"💾 Writing continuously to `{w.dir}`")
        cols = st.columns(len(rows))
        for col, f in zip(cols, rows):
            size = (f"{f['bytes'] / 1_048_576:.1f} MB" if f["bytes"] >= 1_048_576
                    else f"{f['bytes'] / 1024:.0f} KB")
            col.metric(f["file"], f"{f['rows']:,} rows", size,
                       delta_color="off")


def paint_tables(limit):
    w = st.session_state.writer
    limit = int(limit)
    if w and w.preview_public:
        pub_slot.dataframe(pd.DataFrame(w.preview_public).tail(limit),
                           use_container_width=True, hide_index=True, height=430)
    else:
        pub_slot.info("No public-shareholding rows yet.")
    if w and w.preview_foreign:
        fo_slot.dataframe(pd.DataFrame(w.preview_foreign).tail(limit),
                          use_container_width=True, hide_index=True, height=430)
    else:
        fo_slot.info("No foreign-ownership rows yet.")
    if w and w.log:
        log_slot.dataframe(pd.DataFrame(w.log[-limit:]),
                           use_container_width=True, hide_index=True, height=430)
    else:
        log_slot.info("Run log is empty.")


def paint_last(res):
    if res is None:
        last_slot.info("Nothing scraped yet.")
        return
    with last_slot.container():
        head = st.columns(4)
        head[0].metric("Company", (res.name or "—")[:24])
        head[1].metric("Scrip", res.scripcode or "—")
        shares = res.total_public_shares
        head[2].metric("Public shares held",
                       f"{int(shares):,}" if shares else "—")
        pct = res.public_percent
        head[3].metric("Public holding %", f"{pct}%" if pct is not None else "—")
        if res.public_rows:
            st.caption("Public shareholder statement")
            st.dataframe(pd.DataFrame(res.public_rows), use_container_width=True,
                         hide_index=True, height=260)
        if res.foreign_rows:
            st.caption("Foreign ownership limits")
            st.dataframe(pd.DataFrame(res.foreign_rows),
                         use_container_width=True, hide_index=True, height=200)
        if res.message:
            st.warning(res.message)


def ensure_workbook():
    """Build the complete workbook once, so downloading is a single click.

    Streamed row by row, so a full-universe run peaks around 90 MB rather than
    the ~1.8 GB the naive pandas-to-openpyxl path needed — which is what used to
    get the app killed on Streamlit Cloud.
    """
    w = st.session_state.writer
    if w is None or st.session_state.xlsx is not None:
        return
    if w.public_rows == 0 and w.foreign_rows == 0:
        return
    total = w.public_rows + w.foreign_rows + w.companies * 2
    try:
        with st.spinner(f"Building the Excel workbook — {total:,} rows across "
                        f"5 sheets. This takes about "
                        f"{max(total / 6500, 1):.0f}s for a full run."):
            st.session_state.xlsx = w.excel_bytes()
            st.session_state.xlsx_error = None
    except (MemoryError, OSError, ValueError) as exc:
        st.session_state.xlsx_error = (
            f"Could not build the workbook ({type(exc).__name__}: {exc}). "
            f"The CSV downloads below hold the same data.")


def paint_download():
    w = st.session_state.writer
    if not w or (w.public_rows == 0 and w.foreign_rows == 0):
        return
    stamp = f"{st.session_state.quarter.replace(' ', '_')}_{datetime.now():%Y%m%d_%H%M}"
    with dl_slot.container():
        st.divider()
        st.markdown("#### ⬇️ Export")

        rows = w.public_rows + w.foreign_rows
        if st.session_state.xlsx is not None:
            c1, c2 = st.columns([1, 2])
            c1.download_button(
                "⬇️  Download full Excel workbook",
                data=st.session_state.xlsx,
                file_name=f"BSE_Shareholding_{stamp}.xlsx",
                mime="application/vnd.openxmlformats-officedocument."
                     "spreadsheetml.sheet",
                type="primary", use_container_width=True, key="dl_xlsx")
            c2.caption(
                f"Everything in one file — **About**, **Summary**, "
                f"**Public Shareholding**, **Foreign Ownership Limits**, "
                f"**Run Log**.  \n"
                f"{w.companies:,} companies · {rows:,} data rows · "
                f"{len(st.session_state.xlsx) / 1_048_576:.1f} MB")
        elif st.session_state.xlsx_error:
            st.error(st.session_state.xlsx_error)
        else:
            st.button("🧮 Build the Excel workbook", use_container_width=False,
                      key="mk_xlsx", on_click=ensure_workbook)

        st.caption("Or take the tables individually as CSV — these stream "
                   "straight off disk, so they work at any size.")
        cols = st.columns(5)
        for col, key in zip(cols, ("public", "foreign", "summary", "log")):
            label = w.CSV_LABELS[key]
            col.download_button(
                f"{label}  \n`{w.row_count(key):,} rows`",
                data=w.csv_bytes(key),
                file_name=f"BSE_{label.replace(' ', '_')}_{stamp}.csv",
                mime="text/csv", use_container_width=True,
                disabled=w.csv_size(key) == 0, key=f"csv_{key}")
        if st.session_state.zipb is None:
            cols[4].button("🗜️ All as .zip", use_container_width=True,
                           key="mk_zip",
                           on_click=lambda: st.session_state.update(
                               zipb=w.zip_bytes()))
        else:
            cols[4].download_button(
                "⬇️ Download .zip", data=st.session_state.zipb,
                file_name=f"BSE_Shareholding_{stamp}.zip",
                mime="application/zip", use_container_width=True, key="dl_zip")

        st.caption(f"These same files are already saved on disk in  `{w.dir}` — "
                   f"the downloads are just copies.")


# ---------------------------------------------------------------- the scrape --
if start:
    reset_run()
    st.session_state.running = True
    st.session_state.quarter = sel["label"]
    writer = bse.RunWriter(save_dir, sel["label"], sel["qtrid"])
    st.session_state.writer = writer

    bar = prog_slot.progress(0.0, text="Starting workers…")
    t0 = time.time()
    done = 0
    try:
        for res in bse.scrape_many(targets, sel["label"], sel["qtrid"],
                                   workers=workers, keep_zero_rows=keep_zero,
                                   pause=pause):
            writer.add(res, group=group_of.get(res.scripcode, ""))
            done += 1
            st.session_state.last = res

            icon = {"OK": "✅", "PARTIAL": "⚠️", "NO DATA": "➖",
                    "NOT FOUND": "❓", "ERROR": "❌",
                    "BLOCKED": "🚫"}.get(res.status, "•")
            rate = done / max(time.time() - t0, 0.001)
            left = (n_targets - done) / rate if rate else 0
            bar.progress(done / n_targets,
                         text=f"{done:,}/{n_targets:,} · {rate:.1f} co/s · "
                              f"~{left / 60:.0f} min left")
            now_slot.markdown(
                f"{icon} **{res.name}** ({res.scripcode or 'n/a'}) — "
                f"{len(res.public_rows)} public rows, "
                f"{len(res.foreign_rows)} foreign-ownership rows"
                + (f" · _{res.message}_" if res.message else ""))

            if done % int(repaint_every) == 0 or done == n_targets:
                paint_kpis(done, n_targets)
                paint_files()
                paint_tables(preview_rows)
                paint_last(res)
    except Exception as exc:  # noqa: BLE001 - keep partial data usable
        st.session_state.interrupted = True
        st.error(f"Run stopped early: {exc}")
    finally:
        writer.flush()
        writer.close()
        st.session_state.running = False
        st.session_state.done = True
        st.session_state.elapsed = time.time() - t0

    paint_kpis(done, n_targets)
    paint_files()
    paint_tables(preview_rows)
    paint_last(st.session_state.last)
    bar.progress(1.0, text=f"Finished {done:,} companies in "
                           f"{st.session_state.elapsed / 60:.1f} min")

    blocked = writer.status_counts.get("BLOCKED", 0)
    if blocked and blocked >= max(done * 0.5, 1):
        st.error(
            f"🚫 BSE refused {blocked:,} of {done:,} requests. This host's IP "
            f"is almost certainly blocked — the same code works from a normal "
            f"machine. Run it locally with `streamlit run app.py`, or deploy "
            f"somewhere with an Indian egress IP.")
    elif writer.status_counts.get("ERROR"):
        st.warning(f"{writer.status_counts['ERROR']:,} companies failed on "
                   f"network errors — see the Run Log tab for the reasons.")
    ensure_workbook()
    paint_download()

elif st.session_state.done and st.session_state.writer is not None:
    w = st.session_state.writer
    paint_kpis(w.companies, w.companies)
    noun = "company" if w.companies == 1 else "companies"
    prog_slot.success(
        f"Last run: **{w.companies:,} {noun}**, {st.session_state.quarter} — "
        f"{w.public_rows:,} public rows, {w.foreign_rows:,} foreign-ownership "
        f"rows in {st.session_state.elapsed / 60:.1f} min")
    paint_files()
    paint_tables(preview_rows)
    paint_last(st.session_state.last)
    ensure_workbook()
    paint_download()

else:
    paint_kpis(0, n_targets)
    prog_slot.info("Pick a quarter, choose your universe, then hit "
                   "**🚀 Start scraping** in the sidebar.")
    paint_tables(preview_rows)
    paint_last(None)
