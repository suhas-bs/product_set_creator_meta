"""
meta_catalog_api.py — Meta Catalog & Product Set helpers
Lookup FSNs (retailer_id) → get Meta product IDs → create product sets.
"""
import json
import time
import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

GRAPH       = "https://graph.facebook.com"
API_VERSION = "v22.0"


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


def _get(path: str, token: str, timeout=(10, 30), **params) -> dict:
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


# ── Public API ────────────────────────────────────────────────────────────────

def lookup_fsn(token: str, catalog_id: str, fsn: str) -> tuple[str | None, str | None, str | None]:
    """
    Look up a single FSN (retailer_id) in the catalog.
    Returns (meta_product_id, product_name, error).
    """
    import urllib.parse
    filter_str = json.dumps({"retailer_id": {"eq": fsn}})
    d = _get(
        f"{catalog_id}/products",
        token,
        filter=filter_str,
        fields="id,retailer_id,name",
        limit=1,
    )
    if err := _err(d):
        return None, None, err
    items = d.get("data", [])
    if not items:
        return None, None, "not found in catalog"
    item = items[0]
    return item.get("id"), item.get("name", ""), None


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

    filter_obj = {"retailer_id": {"is_any": fsn_list}}
    d = _post(
        f"{catalog_id}/product_sets",
        token,
        data={
            "name":   name,
            "filter": json.dumps(filter_obj),
        },
    )
    if err := _err(d):
        return None, err
    ps_id = d.get("id")
    if not ps_id:
        return None, f"no id in response: {d}"
    return ps_id, None
