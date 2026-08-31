"""Streamlit front-end for the BSE shareholding-pattern scraper."""

from __future__ import annotations

import time
from datetime import datetime

import pandas as pd
import streamlit as st

import bse_shp as bse

st.set_page_config(page_title="BSE Shareholding Pattern Scraper",
                   page_icon="📊", layout="wide")

SAMPLE = "Reliance Industries\nTata Consultancy Services\nHDFC Bank\nInfosys"

# ------------------------------------------------------------------- state ----
def _init():
    d = {"results": [], "pub": [], "fo": [], "log": [],
         "running": False, "done": False, "quarter": "", "qtrid": "",
         "started": None, "elapsed": 0.0}
    for k, v in d.items():
        st.session_state.setdefault(k, v)


_init()


def reset_run():
    st.session_state.update(results=[], pub=[], fo=[], log=[],
                            done=False, elapsed=0.0)


# ------------------------------------------------------------------ sidebar ---
st.sidebar.title("📊 BSE SHP Scraper")
st.sidebar.caption("Public shareholding pattern + foreign ownership limits, "
                   "straight from BSE's own APIs.")

quarters = bse.quarter_choices(from_year=2016)
labels = [q["label"] for q in quarters]
sel_label = st.sidebar.selectbox(
    "Quarter", labels, index=0,
    help="Quarters are listed newest first. The latest one is the most recent "
         "quarter companies have filed for.")
sel = next(q for q in quarters if q["label"] == sel_label)
st.sidebar.caption(f"BSE quarter id `{sel['qtrid']}`")

st.sidebar.markdown("### Companies")
src = st.sidebar.radio("Input", ["Type / paste", "Upload CSV"],
                       horizontal=True, label_visibility="collapsed")

tokens: list[str] = []
if src == "Type / paste":
    raw = st.sidebar.text_area(
        "One company name or scrip code per line", value=SAMPLE, height=170,
        help="Names are resolved through BSE's search, so 'Reliance Industries' "
             "or '500325' both work.")
    tokens = bse.parse_company_input(raw)
else:
    up = st.sidebar.file_uploader("CSV / TXT", type=["csv", "txt"])
    col_hint = st.sidebar.text_input("Column to read (blank = first column)", "")
    if up is not None:
        try:
            if up.name.lower().endswith(".txt"):
                tokens = bse.parse_company_input(up.getvalue().decode("utf-8", "ignore"))
            else:
                df_in = pd.read_csv(up, dtype=str).fillna("")
                col = col_hint.strip() or df_in.columns[0]
                if col not in df_in.columns:
                    st.sidebar.error(f"No column {col!r}. Found: {list(df_in.columns)}")
                else:
                    tokens = bse.parse_company_input("\n".join(df_in[col].tolist()))
        except Exception as exc:  # noqa: BLE001 - surfaced to the user
            st.sidebar.error(f"Could not read file: {exc}")

with st.sidebar.expander("⚙️ Options"):
    pause = st.slider("Pause between requests (sec)", 0.0, 2.0, 0.35, 0.05,
                      help="Be gentle with BSE. 0.35s is a good default.")
    keep_zero = st.checkbox("Keep category header rows", value=True,
                            help="Header rows such as 'Institutions' carry no "
                                 "numbers. Untick for a tighter sheet.")
    preview_rows = st.number_input("Preview rows to show", 10, 2000, 250, 10)

st.sidebar.write(f"**{len(tokens)}** companies queued")
start = st.sidebar.button("🚀 Start scraping", type="primary",
                          use_container_width=True,
                          disabled=not tokens or st.session_state.running)
if st.sidebar.button("🧹 Clear results", use_container_width=True):
    reset_run()
    st.rerun()

# -------------------------------------------------------------------- header --
st.title("BSE Shareholding Pattern Scraper")
st.markdown(
    "Pulls the **Statement showing shareholding pattern of the Public "
    "shareholder** and the **Statement showing foreign ownership limits** "
    "for every company you list, for the quarter you pick — then exports the lot "
    "to Excel.")

kpi_slot = st.empty()
prog_slot = st.empty()
now_slot = st.empty()

tab_pub, tab_fo, tab_last, tab_log = st.tabs(
    ["🧾 Public Shareholding", "🌍 Foreign Ownership Limits",
     "🔎 Last Company", "📋 Run Log"])
with tab_pub:
    pub_slot = st.empty()
with tab_fo:
    fo_slot = st.empty()
with tab_last:
    last_slot = st.empty()
with tab_log:
    log_slot = st.empty()

dl_slot = st.empty()


# ------------------------------------------------------------------ painting --
def paint_kpis(done_n, total_n):
    ok = sum(1 for r in st.session_state.log if r["Status"] == "OK")
    bad = sum(1 for r in st.session_state.log
              if r["Status"] in ("ERROR", "NOT FOUND"))
    part = sum(1 for r in st.session_state.log
               if r["Status"] in ("PARTIAL", "NO DATA"))
    with kpi_slot.container():
        c = st.columns(6)
        c[0].metric("Companies", f"{done_n}/{total_n}")
        c[1].metric("Complete", ok)
        c[2].metric("Partial / none", part)
        c[3].metric("Failed", bad)
        c[4].metric("Public rows", f"{len(st.session_state.pub):,}")
        c[5].metric("Foreign rows", f"{len(st.session_state.fo):,}")


def paint_tables(limit):
    if st.session_state.pub:
        df = pd.DataFrame(st.session_state.pub)
        pub_slot.dataframe(df.tail(int(limit)), use_container_width=True,
                           hide_index=True, height=430)
    else:
        pub_slot.info("No public-shareholding rows yet.")
    if st.session_state.fo:
        fo_slot.dataframe(pd.DataFrame(st.session_state.fo).tail(int(limit)),
                          use_container_width=True, hide_index=True, height=430)
    else:
        fo_slot.info("No foreign-ownership rows yet.")
    if st.session_state.log:
        log_slot.dataframe(pd.DataFrame(st.session_state.log),
                           use_container_width=True, hide_index=True, height=430)
    else:
        log_slot.info("Run log is empty.")


def paint_last(res):
    with last_slot.container():
        head = st.columns(4)
        head[0].metric("Company", res.name[:24] or "—")
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
            st.dataframe(pd.DataFrame(res.foreign_rows), use_container_width=True,
                         hide_index=True, height=200)


# -------------------------------------------------------------------- Excel ---
def build_excel() -> bytes:
    return bse.build_workbook(
        st.session_state.pub, st.session_state.fo, st.session_state.log,
        st.session_state.results, st.session_state.quarter,
        st.session_state.qtrid)


def paint_download():
    if not (st.session_state.pub or st.session_state.fo):
        return
    stamp = st.session_state.quarter.replace(" ", "_")
    with dl_slot.container():
        st.divider()
        c1, c2 = st.columns([1, 2])
        c1.download_button(
            "⬇️ Export to Excel", data=build_excel(),
            file_name=f"BSE_Shareholding_{stamp}_"
                      f"{datetime.now():%Y%m%d_%H%M}.xlsx",
            mime="application/vnd.openxmlformats-officedocument."
                 "spreadsheetml.sheet",
            type="primary", use_container_width=True)
        c2.caption("Workbook sheets: **About**, **Summary**, "
                   "**Public Shareholding**, **Foreign Ownership Limits**, "
                   "**Run Log**.")


# ---------------------------------------------------------------- the scrape --
if start:
    reset_run()
    st.session_state.running = True
    st.session_state.quarter = sel["label"]
    st.session_state.qtrid = sel["qtrid"]
    t0 = time.time()
    session = bse.make_session()
    bar = prog_slot.progress(0.0, text="Warming up BSE session…")
    total = len(tokens)

    try:
        for i, tok in enumerate(tokens, start=1):
            bar.progress((i - 1) / total,
                         text=f"[{i}/{total}] Fetching **{tok}** — {sel['label']}")
            res = bse.scrape_company(session, tok, sel["label"], sel["qtrid"],
                                     keep_zero_rows=keep_zero, pause=pause)
            st.session_state.results.append(res)
            st.session_state.pub.extend(res.public_rows)
            st.session_state.fo.extend(res.foreign_rows)
            st.session_state.log.append({
                "#": i, "Input": tok, "Scrip Code": res.scripcode,
                "Company": res.name, "Quarter": res.quarter,
                "Public Rows": len(res.public_rows),
                "Foreign Rows": len(res.foreign_rows),
                "Status": res.status, "Note": res.message,
            })

            icon = {"OK": "✅", "PARTIAL": "⚠️", "NO DATA": "➖",
                    "NOT FOUND": "❓", "ERROR": "❌"}.get(res.status, "•")
            now_slot.markdown(
                f"{icon} **{res.name}** ({res.scripcode or 'n/a'}) — "
                f"{len(res.public_rows)} public rows, "
                f"{len(res.foreign_rows)} foreign-ownership rows"
                + (f" · _{res.message}_" if res.message else ""))
            paint_kpis(i, total)
            paint_tables(preview_rows)
            paint_last(res)
            bar.progress(i / total, text=f"[{i}/{total}] done — {res.name}")
            if pause:
                time.sleep(pause)
    finally:
        st.session_state.running = False
        st.session_state.done = True
        st.session_state.elapsed = time.time() - t0

    bar.progress(1.0, text=f"Finished {total} companies in "
                           f"{st.session_state.elapsed:.1f}s")
    paint_download()

elif st.session_state.done:
    paint_kpis(len(st.session_state.results), len(st.session_state.results))
    n = len(st.session_state.results)
    prog_slot.success(
        f"Last run: **{n} {'company' if n == 1 else 'companies'}**, "
        f"{st.session_state.quarter} — {len(st.session_state.pub):,} public rows, "
        f"{len(st.session_state.fo):,} foreign-ownership rows "
        f"in {st.session_state.elapsed:.1f}s")
    paint_tables(preview_rows)
    if st.session_state.results:
        paint_last(st.session_state.results[-1])
    paint_download()

else:
    paint_kpis(0, len(tokens))
    prog_slot.info("Pick a quarter, list your companies, then hit "
                   "**🚀 Start scraping** in the sidebar.")
    paint_tables(preview_rows)
