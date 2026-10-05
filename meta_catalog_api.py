"""
meta_catalog_api.py — Meta Catalog & Product Set helpers
Batch FSN lookup (100 per call) + product set CRUD.
"""
import json
import time
import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

GRAPH       = "https://graph.facebook.com"
API_VERSION = "v22.0"
BATCH_SIZE  = 100          # FSNs per catalog/products call


def _url(path: str) -> str:
    return f"{GRAPH}/{API_VERSION}/{path}"


def _err(d: dict) -> str | None:
    if "error" not in d:
        return None
    e = d["error"]
    msg = e.get("message", str(d))
    for k in ("error_user_msg", "error_user_title"):
        if e.get(k):
            msg += f" | {e[k]}"
    return msg


def _get(path: str, token: str, timeout=(10, 60), **params) -> dict:
    try:
        r = requests.get(
            _url(path),
            params={"access_token": token, **params},
            timeout=timeout,
            verify=False,
        )
        return r.json()
    except Exception as e:
        return {"error": {"message": str(e)}}


def _get_all_pages(path: str, token: str, **params) -> list[dict]:
    """Follow pagination cursors and collect all items."""
    items = []
    url   = _url(path)
    p     = {"access_token": token, **params}
    while url:
        try:
            r = requests.get(url, params=p, timeout=(10, 60), verify=False)
            d = r.json()
        except Exception as e:
            break
        items.extend(d.get("data", []))
        url = d.get("paging", {}).get("next")
        p   = {}            # next URL already has params baked in
    return items


def _post(path: str, token: str, data: dict, timeout=(15, 60)) -> dict:
    try:
        r = requests.post(
            _url(path),
            params={"access_token": token},
            data=data,
            timeout=timeout,
            verify=False,
        )
        return r.json()
    except Exception as e:
        return {"error": {"message": str(e)}}


# ── Product Sets ───────────────────────────────────────────────────────────────

def get_product_sets(token: str, catalog_id: str) -> tuple[list[dict], str | None]:
    """
    Return all product sets in the catalog.
    Each item: {id, name, product_count, filter}.
    """
    items = _get_all_pages(
        f"{catalog_id}/product_sets",
        token,
        fields="id,name,product_count,filter",
        limit=200,
    )
    if not items and not isinstance(items, list):
        return [], "Failed to fetch product sets"
    return items, None


# ── FSN Lookup (batch) ─────────────────────────────────────────────────────────

def lookup_fsns_batch(
    token: str,
    catalog_id: str,
    fsns: list[str],
    batch_size: int = BATCH_SIZE,
    progress_cb=None,
) -> tuple[dict[str, str], list[str]]:
    """
    Look up multiple FSNs in batches of `batch_size`.
    Returns:
      matched   — {fsn: meta_product_id}
      unmatched — [fsn, ...]
    progress_cb(done, total) called after each batch if provided.
    """
    matched:   dict[str, str] = {}
    unmatched: list[str]      = []

    chunks = [fsns[i:i + batch_size] for i in range(0, len(fsns), batch_size)]

    for ci, chunk in enumerate(chunks):
        filter_str = json.dumps({"retailer_id": {"is_any": chunk}})
        d = _get(
            f"{catalog_id}/products",
            token,
            filter=filter_str,
            fields="id,retailer_id",
            limit=batch_size,
        )

        if _err(d):
            # treat whole chunk as unmatched on API error
            unmatched.extend(chunk)
        else:
            found_fsns = {item["retailer_id"]: item["id"] for item in d.get("data", [])}
            for fsn in chunk:
                if fsn in found_fsns:
                    matched[fsn] = found_fsns[fsn]
                else:
                    unmatched.append(fsn)

        if progress_cb:
            progress_cb(min((ci + 1) * batch_size, len(fsns)), len(fsns))

        time.sleep(0.3)   # gentle rate-limit buffer between batch calls

    return matched, unmatched


# ── Create Product Set ─────────────────────────────────────────────────────────

def create_product_set(
    token: str,
    catalog_id: str,
    name: str,
    fsn_list: list[str],
) -> tuple[str | None, str | None]:
    """
    Create a product set filtered by retailer_id (FSN list).
    Returns (product_set_id, error).
    """
    if not fsn_list:
        return None, "no valid FSNs to add"

    d = _post(
        f"{catalog_id}/product_sets",
        token,
        data={
            "name":   name,
            "filter": json.dumps({"retailer_id": {"is_any": fsn_list}}),
        },
    )
    if err := _err(d):
        return None, err
    ps_id = d.get("id")
    return (ps_id, None) if ps_id else (None, f"no id in response: {d}")
