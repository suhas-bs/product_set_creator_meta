"""
app_product_set.py — Meta Product Set Creator
CSV in → FSN lookup → product set creation → results out.

CSV columns (required):
  set_name   — name/nomenclature for the product set
  fsns       — pipe-separated FSN list  e.g. "FSN001|FSN002|FSN003"

Optional CSV column:
  catalog_id — overrides sidebar value per row
"""

import io
import time

import pandas as pd
import streamlit as st

from meta_catalog_api import create_product_set, lookup_fsn

st.set_page_config(page_title="Meta Product Set Creator", page_icon="🗂️", layout="wide")

STATUS_EMOJI = {
    "pending":   "⏳",
    "looking_up": "🔍",
    "creating":  "🛠️",
    "success":   "✅",
    "failed":    "❌",
    "partial":   "⚠️",
}

# ── Sidebar ───────────────────────────────────────────────────────────────────
with st.sidebar:
    st.title("⚙️ Config")
    meta_token  = st.text_area("Access Token", height=80, placeholder="Paste Meta access token…").strip()
    catalog_id  = st.text_input("Default Catalog ID", placeholder="e.g. 1234567890123")
    st.divider()
    st.markdown("**CSV format**")
    st.code("set_name,fsns\nMoto Phones,FSN001|FSN002|FSN003\nSamsung TVs,FSN004|FSN005", language="csv")
    st.caption("Add a `catalog_id` column to override per row.")

# ── Main ──────────────────────────────────────────────────────────────────────
st.title("🗂️ Meta Product Set Creator")
st.caption("Looks up each FSN in the catalog, then creates a product set for each row.")

if not meta_token:
    st.info("🔑 Paste your Meta access token in the sidebar.")
    st.stop()

uploaded = st.file_uploader("Upload CSV", type=["csv"])
if not uploaded:
    st.info("📄 Upload a CSV with `set_name` and `fsns` columns.")
    st.stop()

df = pd.read_csv(uploaded)

required = {"set_name", "fsns"}
if not required.issubset(df.columns):
    st.error(f"CSV must have columns: {required}. Found: {set(df.columns)}")
    st.stop()

st.success(f"Found **{len(df)}** product set(s) to create.")
with st.expander("Preview", expanded=True):
    st.dataframe(df, use_container_width=True, hide_index=True)

if not st.button(f"🚀 Create {len(df)} product set(s)", type="primary"):
    st.stop()

# ── Processing ────────────────────────────────────────────────────────────────
results = []
for _, row in df.iterrows():
    set_name   = str(row["set_name"]).strip()
    raw_fsns   = str(row["fsns"]).strip()
    cat_id     = str(row.get("catalog_id", catalog_id)).strip() if "catalog_id" in df.columns else catalog_id

    if not cat_id:
        results.append({
            "set_name":       set_name,
            "product_set_id": None,
            "matched_fsns":   "",
            "unmatched_fsns": raw_fsns,
            "status":         "failed",
            "error":          "No catalog ID provided",
        })
        continue

    fsns = [f.strip() for f in raw_fsns.split("|") if f.strip()]

    status_ph = st.empty()
    status_ph.info(f"🔍 **{set_name}** — looking up {len(fsns)} FSN(s)…")

    # Step 1: Lookup each FSN
    matched   = []
    unmatched = []
    for fsn in fsns:
        pid, pname, err = lookup_fsn(meta_token, cat_id, fsn)
        if pid:
            matched.append(fsn)
        else:
            unmatched.append(fsn)
        time.sleep(0.2)   # light rate-limit buffer

    if not matched:
        results.append({
            "set_name":       set_name,
            "product_set_id": None,
            "matched_fsns":   "",
            "unmatched_fsns": "|".join(unmatched),
            "status":         "failed",
            "error":          "No FSNs found in catalog",
        })
        status_ph.error(f"❌ **{set_name}** — no FSNs matched in catalog.")
        continue

    status_ph.info(f"🛠️ **{set_name}** — {len(matched)}/{len(fsns)} FSNs matched. Creating product set…")

    # Step 2: Create product set
    ps_id, err = create_product_set(meta_token, cat_id, set_name, matched)

    if err:
        results.append({
            "set_name":       set_name,
            "product_set_id": None,
            "matched_fsns":   "|".join(matched),
            "unmatched_fsns": "|".join(unmatched),
            "status":         "failed",
            "error":          err,
        })
        status_ph.error(f"❌ **{set_name}** — create failed: {err}")
    else:
        status_tag = "success" if not unmatched else "partial"
        results.append({
            "set_name":       set_name,
            "product_set_id": ps_id,
            "matched_fsns":   "|".join(matched),
            "unmatched_fsns": "|".join(unmatched),
            "status":         status_tag,
            "error":          f"{len(unmatched)} FSN(s) not found" if unmatched else "",
        })
        icon = "✅" if not unmatched else "⚠️"
        status_ph.success(
            f"{icon} **{set_name}** — product set `{ps_id}` created "
            f"({len(matched)} matched, {len(unmatched)} unmatched)."
        )

# ── Summary ───────────────────────────────────────────────────────────────────
st.divider()
result_df = pd.DataFrame(results)
st.dataframe(result_df, use_container_width=True, hide_index=True)

c1, c2, c3 = st.columns(3)
c1.metric("✅ Success",  int((result_df["status"] == "success").sum()))
c2.metric("⚠️ Partial",  int((result_df["status"] == "partial").sum()))
c3.metric("❌ Failed",   int((result_df["status"] == "failed").sum()))

csv_bytes = result_df.to_csv(index=False).encode()
st.download_button(
    "⬇️ Download Results CSV", csv_bytes,
    file_name="product_set_results.csv", mime="text/csv", type="primary",
)
