"""
app_product_set.py — Meta Product Set Creator
Upload CSV → auto-runs all rows in batches of 10 product sets.
FSN lookup uses 100-FSN batches per API call.

CSV columns (required):
  set_name   — product set name/nomenclature
  fsns       — pipe-separated FSN list  e.g. "FSN001 | FSN002 | FSN003"
"""

import time
import pandas as pd
import streamlit as st

from meta_catalog_api import create_product_set, find_product_set_by_name, get_product_sets, lookup_fsns_batch

st.set_page_config(page_title="Meta Product Set Creator", page_icon="🗂️", layout="wide")

DEFAULT_CATALOG_ID = "1703640393200941"
SETS_PER_BATCH     = 10

# ── Sidebar ───────────────────────────────────────────────────────────────────
with st.sidebar:
    st.title("⚙️ Config")
    meta_token = st.text_area("Access Token", height=80, placeholder="Paste Meta access token…").strip()
    catalog_id = st.text_input("Catalog ID", value=DEFAULT_CATALOG_ID)
    st.divider()
    st.markdown("**CSV format**")
    st.code("set_name,fsns\nMoto Phones,FSN001|FSN002\nSamsung TVs,FSN003|FSN004", language="csv")

st.title("🗂️ Meta Product Set Creator")

if not meta_token:
    st.info("🔑 Paste your Meta access token in the sidebar.")
    st.stop()

tab_existing, tab_create = st.tabs(["📋 Existing Product Sets", "➕ Create Product Sets"])

# ══════════════════════════════════════════════════════════════════════════════
# TAB 1 — View existing product sets
# ══════════════════════════════════════════════════════════════════════════════
with tab_existing:
    st.subheader("Existing Product Sets")
    st.caption(f"Catalog: `{catalog_id}`")

    if st.button("🔄 Fetch Product Sets", type="primary"):
        import json as _json

        with st.spinner("Fetching…"):
            sets, err = get_product_sets(meta_token, catalog_id)
        if err:
            st.error(f"Error: {err}")
        elif not sets:
            st.warning("No product sets found.")
        else:
            df_sets = pd.DataFrame(sets)[["id", "name", "product_count", "filter"]]
            df_sets.columns = ["Product Set ID", "Name", "Product Count", "Filter"]
            st.success(f"Found **{len(sets)}** product set(s).")
            st.dataframe(df_sets, use_container_width=True, hide_index=True)
            st.download_button(
                "⬇️ Download as CSV",
                df_sets.to_csv(index=False).encode(),
                file_name="existing_product_sets.csv", mime="text/csv",
            )

            # ── Duplicate FSN group detection ─────────────────────────────
            st.divider()
            st.subheader("🔁 Product Sets Sharing the Same FSN Group")

            fsn_group_map: dict[str, list[str]] = {}  # fsn_key → [set_name, ...]
            for s in sets:
                raw_filter = s.get("filter") or ""
                try:
                    f = _json.loads(raw_filter) if isinstance(raw_filter, str) else raw_filter
                    fsns = f.get("retailer_id", {}).get("is_any", [])
                    key  = "|".join(sorted(fsns))   # canonical key
                except Exception:
                    key = raw_filter  # fallback: use raw string

                if key:
                    fsn_group_map.setdefault(key, []).append(s.get("name", "—"))

            dupes = {k: v for k, v in fsn_group_map.items() if len(v) > 1}

            if not dupes:
                st.info("No product sets share the same FSN group.")
            else:
                rows = []
                for fsn_key, names in dupes.items():
                    sample_fsns = fsn_key[:120] + "…" if len(fsn_key) > 120 else fsn_key
                    rows.append({
                        "FSN Group (sample)":      sample_fsns,
                        "# Sets with same FSNs":   len(names),
                        "Product Set Names":        " | ".join(names),
                    })
                df_dupes = pd.DataFrame(rows)
                st.warning(f"⚠️ **{len(dupes)}** FSN group(s) used by more than one product set.")
                st.dataframe(df_dupes, use_container_width=True, hide_index=True)
                st.download_button(
                    "⬇️ Download Duplicates CSV",
                    df_dupes.to_csv(index=False).encode(),
                    file_name="duplicate_fsn_groups.csv", mime="text/csv",
                )

# ══════════════════════════════════════════════════════════════════════════════
# TAB 2 — Create product sets (auto-batched)
# ══════════════════════════════════════════════════════════════════════════════
with tab_create:
    st.subheader("Create Product Sets from CSV")
    st.caption(f"Runs {SETS_PER_BATCH} product sets at a time. FSN lookup: 100 FSNs per API call.")

    uploaded = st.file_uploader("Upload CSV", type=["csv"])
    if not uploaded:
        st.info("📄 Upload a CSV with `set_name` and `fsns` columns.")
        st.stop()

    df_full = pd.read_csv(uploaded)
    required = {"set_name", "fsns"}
    if not required.issubset(df_full.columns):
        st.error(f"CSV must have columns: {required}. Found: {set(df_full.columns)}")
        st.stop()

    total_rows    = len(df_full)
    total_batches = (total_rows + SETS_PER_BATCH - 1) // SETS_PER_BATCH
    st.success(f"**{total_rows}** product sets loaded → **{total_batches}** batches of {SETS_PER_BATCH}")

    with st.expander("Preview CSV", expanded=False):
        st.dataframe(df_full.head(20), use_container_width=True, hide_index=True)

    if not st.button(f"🚀 Run all {total_batches} batches", type="primary"):
        st.stop()

    # ── Pre-fetch existing sets once ─────────────────────────────────────────
    with st.spinner("Loading existing product sets…"):
        existing_sets, _ = get_product_sets(meta_token, catalog_id)
    # Only keep entries where id is present; others will go through create flow
    existing_map = {s["name"]: s["id"] for s in existing_sets if s.get("id")}
    st.info(f"ℹ️ {len(existing_map)} existing product set(s) found — duplicates will be skipped.")

    # ── Overall progress ──────────────────────────────────────────────────────
    overall_bar   = st.progress(0, text="Starting…")
    batch_status  = st.empty()
    results_ph    = st.empty()
    all_results   = []

    for batch_idx in range(total_batches):
        b_start = batch_idx * SETS_PER_BATCH
        b_end   = min(b_start + SETS_PER_BATCH, total_rows)
        df_batch = df_full.iloc[b_start:b_end].reset_index(drop=True)

        overall_bar.progress(
            batch_idx / total_batches,
            text=f"Batch {batch_idx+1}/{total_batches} — sets {b_start+1}–{b_end} of {total_rows}"
        )

        with st.expander(f"Batch {batch_idx+1}/{total_batches}  (rows {b_start+1}–{b_end})", expanded=False):

            for _, row in df_batch.iterrows():
                set_name = str(row["set_name"]).strip()
                raw_fsns = str(row["fsns"]).strip()
                cat_id   = (
                    str(row["catalog_id"]).strip()
                    if "catalog_id" in df_full.columns
                    else catalog_id
                )

                # Already exists → return ID, skip
                if set_name in existing_map:
                    all_results.append({
                        "set_name": set_name,
                        "product_set_id": existing_map[set_name],
                        "matched_count": "-", "unmatched_count": "-",
                        "matched_fsns": "", "unmatched_fsns": "",
                        "status": "existing", "error": "",
                    })
                    st.info(f"⏭️ **{set_name}** — exists → `{existing_map[set_name]}`")
                    continue

                fsns = [f.strip() for f in raw_fsns.split("|") if f.strip()]
                st.write(f"🔍 **{set_name}** — looking up {len(fsns)} FSN(s)…")
                prog = st.progress(0)

                def _cb(done, total, _p=prog):
                    _p.progress(done / total)

                matched, unmatched = lookup_fsns_batch(
                    meta_token, cat_id, fsns, batch_size=100, progress_cb=_cb
                )
                prog.progress(1.0)

                if not matched:
                    all_results.append({
                        "set_name": set_name, "product_set_id": None,
                        "matched_count": 0, "unmatched_count": len(unmatched),
                        "matched_fsns": "", "unmatched_fsns": "|".join(unmatched),
                        "status": "failed", "error": "No FSNs found in catalog",
                    })
                    st.error(f"❌ **{set_name}** — no FSNs matched.")
                    continue

                ps_id, err = create_product_set(meta_token, cat_id, set_name, list(matched.keys()))

                if err:
                    # If it already exists in Meta, recover the existing ID by name
                    recovered_id = find_product_set_by_name(meta_token, cat_id, set_name)
                    if recovered_id:
                        all_results.append({
                            "set_name": set_name, "product_set_id": recovered_id,
                            "matched_count": len(matched), "unmatched_count": len(unmatched),
                            "matched_fsns": "|".join(matched.keys()),
                            "unmatched_fsns": "|".join(unmatched),
                            "status": "existing", "error": "already existed — ID recovered",
                        })
                        existing_map[set_name] = recovered_id
                        st.warning(f"⏭️ **{set_name}** — already existed, recovered ID: `{recovered_id}`")
                    else:
                        all_results.append({
                            "set_name": set_name, "product_set_id": None,
                            "matched_count": len(matched), "unmatched_count": len(unmatched),
                            "matched_fsns": "|".join(matched.keys()),
                            "unmatched_fsns": "|".join(unmatched),
                            "status": "failed", "error": err,
                        })
                        st.error(f"❌ **{set_name}** — {err}")
                else:
                    tag = "success" if not unmatched else "partial"
                    all_results.append({
                        "set_name": set_name, "product_set_id": ps_id,
                        "matched_count": len(matched), "unmatched_count": len(unmatched),
                        "matched_fsns": "|".join(matched.keys()),
                        "unmatched_fsns": "|".join(unmatched),
                        "status": tag,
                        "error": f"{len(unmatched)} FSN(s) not found" if unmatched else "",
                    })
                    icon = "✅" if not unmatched else "⚠️"
                    st.success(f"{icon} **{set_name}** → `{ps_id}` ({len(matched)} matched, {len(unmatched)} unmatched)")
                    # Add new set to existing_map so re-runs within session skip it
                    existing_map[set_name] = ps_id

        # Update live results table after every batch
        result_df = pd.DataFrame(all_results)
        results_ph.dataframe(
            result_df[["set_name", "product_set_id", "matched_count", "unmatched_count", "status", "error"]],
            use_container_width=True, hide_index=True,
        )

    overall_bar.progress(1.0, text="✅ All batches complete!")

    # ── Final summary ─────────────────────────────────────────────────────────
    st.divider()
    result_df = pd.DataFrame(all_results)
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("✅ Created",  int((result_df["status"] == "success").sum()))
    c2.metric("⚠️ Partial",  int((result_df["status"] == "partial").sum()))
    c3.metric("⏭️ Existing", int((result_df["status"] == "existing").sum()))
    c4.metric("❌ Failed",   int((result_df["status"] == "failed").sum()))

    st.download_button(
        "⬇️ Download Full Results CSV",
        result_df.to_csv(index=False).encode(),
        file_name="product_set_results.csv", mime="text/csv", type="primary",
    )
