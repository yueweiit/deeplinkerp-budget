"""Reuse the CN/MX approval archive reader; never query legacy budget HTTP APIs."""
from __future__ import annotations

import frappe
from frappe import _


def available():
    return bool(getattr(frappe.local, "site", None)) and "deeplinkerp_branding" in frappe.get_installed_apps()


def client():
    from deeplinkerp_branding.services import operating_oa_http
    return operating_oa_http


def configuration(corp_id=""):
    if not available():
        return "", str(corp_id or "").strip(), False
    http = client()
    url, corps = http.configuration()
    # A single configured corporation can be reused without entering it again.
    corp = str(corp_id or "").strip() or (corps[0] if len(corps) == 1 else "")
    return url, corp, corp in corps


def instances(source, check_deadline):
    from budget.services.oa_budget_parser import BUDGET_PROCESS_CODE, parse_budget_instance

    http = client()
    url, corp, allowed = configuration(source["corp_id"])
    if not allowed or url != source["connection"]["url"]:
        frappe.throw(_("Shared OA configuration changed. Reopen the sync settings."))
    rows, seen, cursors = [], set(), set()
    cursor, snapshot = None, None
    for _page in range(http.MAX_PAGES):
        check_deadline()
        page_rows, page = http.read_template_page(url, corp, BUDGET_PROCESS_CODE, cursor=cursor)
        if snapshot is not None and page["syncUntil"] != snapshot:
            frappe.throw(_("OA approval snapshot changed. Retry synchronization."))
        snapshot = page["syncUntil"]
        for row in page_rows:
            identity = row["process_instance_id"]
            if identity in seen:
                frappe.throw(_("OA approval pagination returned duplicate records."))
            seen.add(identity)
            if row.get("deleted_at") or row.get("status") != "COMPLETED" or row.get("result") != "agree":
                continue
            region = "".join(str(parse_budget_instance(row)["entry"].get("execution_region") or "").split()).casefold()
            if region in {"中国", "中国china", "china", "cn"}:
                rows.append(row)
        if not page["hasMore"]:
            return rows
        cursor = page["nextCursor"]
        if not cursor or cursor in cursors:
            frappe.throw(_("OA approval pagination did not advance."))
        cursors.add(cursor)
    frappe.throw(_("OA approval scan exceeded the page limit."))


def department_paths():
    """Keep proven paths already in this ledger. The shared API has no org-tree endpoint.

    New organization IDs keep their full OA name for review; never infer a legal
    company from a similar department name or call an unrelated database.
    """
    from budget.services.oa_budget_sync import _json_list

    paths = {}
    for row in frappe.get_all("Budget Ledger Entry", fields=["department_id", "dept_path_ids", "dept_path_names"],
                             order_by="synced_at desc", limit_page_length=0):
        ids, names = _json_list(row.get("dept_path_ids")), _json_list(row.get("dept_path_names"))
        if ids and len(ids) == len(names) and str(row.get("department_id") or "") == ids[-1]:
            paths.setdefault(str(row["department_id"]), {"ids": ids, "names": names})
    return paths
