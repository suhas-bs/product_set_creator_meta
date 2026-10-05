"""
app_product_set.py — Meta Product Set Creator
CSV in → batch FSN lookup (100/call) → product set creation → results out.
Also lists all existing product sets from the catalog.

CSV columns (required):
  set_name   — name/nomenclature for the product set
  fsns       — pipe-separated FSN list  e.g. "FSN001|FSN002|FSN003"
"""

import io
import time

import pandas as pd
import streamlit as st

from meta_catalog_api import create_product_set, get_product_sets, lookup_fsns_batch

st.set_page_config(page_title="Meta Product Set Creator", page_icon="🗂️", layout="wide")

DEFAULT_CATALOG_ID = "1703640393200941"

# ── Sidebar ───────────────────────────────────────────────────────────────────
with st.sidebar:
    st.title("⚙️ Config")
    meta_token = st.text_area("Access Token", height=80, placeholder="Paste Meta access token…").strip()
    catalog_id = st.text_input("Catalog ID", value=DEFAULT_CATALOG_ID)
    st.divider()
    st.markdown("**CSV format**")
    st.code("set_name,fsns\nMoto Phones,FSN001|FSN002|FSN003\nSamsung TVs,FSN004|FSN005", language="csv")

# ── Main ──────────────────────────────────────────────────────────────────────
st.title("🗂️ Meta Product Set Creator")

if not meta_token:
    st.info("🔑 Paste your Meta access token in the sidebar.")
    st.stop()

tab_existing, tab_create = st.tabs(["📋 Existing Product Sets", "➕ Create Product Sets"])

# ══════════════════════════════════════════════════════════════════════════════
# TAB 1 — Existing product sets
# ══════════════════════════════════════════════════════════════════════════════
with tab_existing:
    st.subheader("Existing Product Sets")
    st.caption(f"Catalog: `{catalog_id}`")

    if st.button("🔄 Fetch Product Sets", type="primary"):
        with st.spinner("Fetching product sets…"):
            sets, err = get_product_sets(meta_token, catalog_id)

        if err:
            st.error(f"Error: {err}")
        elif not sets:
            st.warning("No product sets found in this catalog.")
        else:
            df_sets = pd.DataFrame(sets)[["id", "name", "product_count", "filter"]]
            df_sets.columns = ["Product Set ID", "Name", "Product Count", "Filter"]
            st.success(f"Found **{len(sets)}** product set(s).")
            st.dataframe(df_sets, use_container_width=True, hide_index=True)

            csv = df_sets.to_csv(index=False).encode()
            st.download_button(
                "⬇️ Download as CSV", csv,
                file_name="existing_product_sets.csv", mime="text/csv",
            )

# ══════════════════════════════════════════════════════════════════════════════
# TAB 2 — Create product sets
# ══════════════════════════════════════════════════════════════════════════════
with tab_create:
    st.subheader("Create Product Sets from CSV")
    st.caption("FSNs are looked up in batches of 100 to stay within rate limits.")

    uploaded = st.file_uploader("Upload CSV", type=["csv"])
    if not uploaded:
        st.info("📄 Upload a CSV with `set_name` and `fsns` columns.")
        st.stop()

    df = pd.read_csv(uploaded)

    required = {"set_name", "fsns"}
    if not required.issubset(df.columns):
        st.error(f"CSV must have columns: {required}. Found: {set(df.columns)}")
        st.stop()

    total_rows  = len(df)
    sets_per_batch = 10
    total_batches  = (total_rows + sets_per_batch - 1) // sets_per_batch

    st.success(f"Found **{total_rows}** product set(s) → **{total_batches}** batch(es) of {sets_per_batch}.")

    col_b, col_info = st.columns([1, 3])
    batch_num = col_b.number_input(
        "Batch to run", min_value=1, max_value=total_batches, value=1, step=1
    )
    batch_start = (batch_num - 1) * sets_per_batch
    batch_end   = min(batch_start + sets_per_batch, total_rows)
    col_info.info(f"Batch {batch_num}/{total_batches} → rows {batch_start+1}–{batch_end} of {total_rows}")

    df_batch = df.iloc[batch_start:batch_end].reset_index(drop=True)

    with st.expander(f"Preview — batch {batch_num}", expanded=True):
        st.dataframe(df_batch, use_container_width=True, hide_index=True)

    if not st.button(f"🚀 Run batch {batch_num} ({len(df_batch)} set(s))", type="primary"):
        st.stop()

    df = df_batch   # process only this batch

    # Pre-fetch existing product sets → build name → id map
    with st.spinner("Checking existing product sets…"):
        existing_sets, _ = get_product_sets(meta_token, catalog_id)
    existing_map = {s["name"]: s["id"] for s in existing_sets}
    if existing_map:
        st.info(f"ℹ️ Found {len(existing_map)} existing product set(s) — duplicates will be skipped.")

    results = []

    for row_idx, row in df.iterrows():
        set_name = str(row["set_name"]).strip()
        raw_fsns = str(row["fsns"]).strip()
        cat_id   = (
            str(row.get("catalog_id", catalog_id)).strip()
            if "catalog_id" in df.columns
            else catalog_id
        )

        if not cat_id:
            results.append({
                "set_name": set_name, "product_set_id": None,
                "matched_count": 0, "unmatched_count": 0,
                "matched_fsns": "", "unmatched_fsns": raw_fsns,
                "status": "failed", "error": "No catalog ID",
            })
            continue

        # ── Already exists? Return existing ID and skip ──────────────────
        if set_name in existing_map:
            existing_id = existing_map[set_name]
            results.append({
                "set_name": set_name, "product_set_id": existing_id,
                "matched_count": "-", "unmatched_count": "-",
                "matched_fsns": "", "unmatched_fsns": "",
                "status": "existing", "error": "",
            })
            st.markdown(f"---\n**[{int(row_idx)+1}/{len(df)}] {set_name}** — already exists")
            st.info(f"⏭️ Skipped — existing product set ID: `{existing_id}`")
            continue

        fsns = [f.strip() for f in raw_fsns.split("|") if f.strip()]
        total_fsns = len(fsns)

        st.markdown(f"---\n**[{int(row_idx)+1}/{len(df)}] {set_name}** — {total_fsns} FSN(s)")
        prog_bar   = st.progress(0, text="Looking up FSNs…")
        status_ph  = st.empty()

        # Step 1: Batch FSN lookup
        def _progress(done, total):
            pct  = int(done / total * 100)
            prog_bar.progress(pct / 100, text=f"Looked up {done}/{total} FSNs…")

        matched, unmatched = lookup_fsns_batch(
            meta_token, cat_id, fsns,
            batch_size=100,
            progress_cb=_progress,
        )

        prog_bar.progress(1.0, text="Lookup complete.")

        if not matched:
            results.append({
                "set_name": set_name, "product_set_id": None,
                "matched_count": 0, "unmatched_count": len(unmatched),
                "matched_fsns": "", "unmatched_fsns": "|".join(unmatched),
                "status": "failed", "error": "No FSNs found in catalog",
            })
            status_ph.error(f"❌ No FSNs matched in catalog — skipping.")
            continue

        status_ph.info(f"🛠️ {len(matched)}/{total_fsns} FSNs matched. Creating product set…")

        # Step 2: Create product set
        ps_id, err = create_product_set(meta_token, cat_id, set_name, list(matched.keys()))

        if err:
            results.append({
                "set_name": set_name, "product_set_id": None,
                "matched_count": len(matched), "unmatched_count": len(unmatched),
                "matched_fsns": "|".join(matched.keys()),
                "unmatched_fsns": "|".join(unmatched),
                "status": "failed", "error": err,
            })
            status_ph.error(f"❌ Create failed: {err}")
        else:
            tag = "success" if not unmatched else "partial"
            results.append({
                "set_name": set_name, "product_set_id": ps_id,
                "matched_count": len(matched), "unmatched_count": len(unmatched),
                "matched_fsns": "|".join(matched.keys()),
                "unmatched_fsns": "|".join(unmatched),
                "status": tag, "error": f"{len(unmatched)} FSN(s) not found" if unmatched else "",
            })
            icon = "✅" if not unmatched else "⚠️"
            status_ph.success(
                f"{icon} Product set `{ps_id}` created — "
                f"{len(matched)} matched, {len(unmatched)} unmatched."
            )

    # Summary
    st.divider()
    result_df = pd.DataFrame(results)
    st.dataframe(result_df, use_container_width=True, hide_index=True)

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("✅ Created",  int((result_df["status"] == "success").sum()))
    c2.metric("⚠️ Partial",  int((result_df["status"] == "partial").sum()))
    c3.metric("⏭️ Existing", int((result_df["status"] == "existing").sum()))
    c4.metric("❌ Failed",   int((result_df["status"] == "failed").sum()))

    csv_bytes = result_df.to_csv(index=False).encode()
    st.download_button(
        "⬇️ Download Results CSV", csv_bytes,
        file_name="product_set_results.csv", mime="text/csv", type="primary",
    )
