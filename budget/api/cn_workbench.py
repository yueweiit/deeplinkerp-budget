"""Read-only CN budget adapter; budget plans and quotas never become expense totals."""
from __future__ import annotations

from decimal import Decimal

import frappe
from frappe import _
from frappe.utils import cint, get_first_day, get_last_day, getdate

from budget.integrations.budget_quota_local_source import BudgetQuotaLocalSource
from budget.services.oa_budget_sync import COMPANY_LEVEL_INDEX, _json_list
from budget.services.organization_mapping import schema_state

READ_ROLES = ("System Manager", "Accounts Manager")
# Names are preserved from the approval source; no fuzzy legal-company assignment.
_IDS = "IF(JSON_VALID(dept_path_ids), dept_path_ids, '[]')"
_NAMES = "IF(JSON_VALID(dept_path_names), dept_path_names, '[]')"
# A company-level approval intentionally has no department display name. Require
# matching organization identity/path, rather than treating every blank as valid.
COMPANY_LEVEL = f"""COALESCE((reviewed_organization_level = 'company'
    AND NULLIF(TRIM(company), '') IS NOT NULL AND company <> '待确认'
    AND NULLIF(TRIM(department_id), '') IS NOT NULL) OR CASE WHEN COALESCE(reviewed_organization_level, '') = ''
    AND JSON_TYPE({_IDS}) = 'ARRAY'
    AND JSON_TYPE({_NAMES}) = 'ARRAY' AND JSON_LENGTH({_IDS}) BETWEEN 1 AND {COMPANY_LEVEL_INDEX + 1}
    AND JSON_LENGTH({_IDS}) = JSON_LENGTH({_NAMES})
    THEN NULLIF(TRIM(department_id), '') = JSON_UNQUOTE(JSON_EXTRACT({_IDS}, CONCAT('$[', JSON_LENGTH({_IDS}) - 1, ']')))
        AND NULLIF(TRIM(company), '') = TRIM(JSON_UNQUOTE(JSON_EXTRACT({_NAMES}, CONCAT('$[', JSON_LENGTH({_NAMES}) - 1, ']'))))
        AND company <> '待确认'
    ELSE 0 END, 0)"""
ISSUES = f"""(COALESCE(company, '') IN ('', '待确认') OR (COALESCE(TRIM(department), '') = '' AND NOT ({COMPANY_LEVEL}))
    OR budget_month IS NULL OR ABS(COALESCE(total_amount, amount, 0) - COALESCE(detail_amount, 0)) > 0.01)"""


def _check(quota=False):
    frappe.only_for(("System Manager",) if quota else READ_ROLES)


def normalize_filters(filters=None):
    values = frappe.parse_json(filters) if isinstance(filters, str) else (filters or {})
    if not isinstance(values, dict):
        frappe.throw(_("Invalid budget filters"))
    start = get_first_day(getdate(values.get("from_date")))
    end = get_last_day(getdate(values.get("to_date")))
    if start > end:
        frappe.throw(_("Start date must not be after end date"))
    return dict(from_date=str(start), to_date=str(end), company=str(values.get("company") or "").strip(),
                budget_type=values.get("budget_type") if values.get("budget_type") in ("production", "non_production") else "",
                keyword=str(values.get("keyword") or "")[:200], department_id=str(values.get("department_id") or "")[:140])


def ledger_where(values, review=False, *, issues=ISSUES):
    where = ["budget_month BETWEEN %(from_date)s AND %(to_date)s"]
    if review:
        # Undated budgets remain findable by application date, never counted as dated budgets.
        where[0] = "(budget_month BETWEEN %(from_date)s AND %(to_date)s OR (budget_month IS NULL AND application_date BETWEEN %(from_date)s AND %(to_date)s))"
        where.append(issues)
    for field in ("company", "department_id"):
        if values[field]:
            where.append(f"{field} = %({field})s")
    if values["budget_type"]:
        where.append("budget_kind = %(budget_type)s")
    if values["keyword"]:
        where.append("(form_no LIKE %(keyword_like)s OR department LIKE %(keyword_like)s)")
    return " AND ".join(where), {**values, "keyword_like": "%" + values["keyword"] + "%"}


def quota_filters(values):
    """Keep list, totals and export on the same department drill-down scope."""
    return dict(start_month=values["from_date"][:7], end_month=values["to_date"][:7],
                company_name=values["company"], budget_type=values["budget_type"],
                department_id=values["department_id"], keyword=values["keyword"])


def review_columns():
    # Only internal SQL literals are substituted. Never reference a column that
    # has not been migrated; existing organization paths remain authoritative.
    level = "reviewed_organization_level" if schema_state()["level_column"] else "''"
    return level, ISSUES.replace("reviewed_organization_level", level)


@frappe.whitelist()
def get_options():
    _check()
    companies = {row.company for row in frappe.get_list("Budget Ledger Entry", fields=["company"], group_by="company", limit_page_length=0) if row.company}
    quota = frappe.session.user == "Administrator" or "System Manager" in frappe.get_roles()
    if quota:
        companies.update(row.company_name for row in frappe.get_list("Budget Quota", fields=["company_name"], group_by="company_name", filters={"status": "effective"}, limit_page_length=0) if row.company_name)
    ready = schema_state()["ready"]
    return {"companies": sorted(companies), "can_read_quota": quota,
            "can_review_organization": quota and ready, "organization_review_ready": ready}


@frappe.whitelist()
def get_data(filters=None, start=0, view="overview", tab="ledger"):
    _check(tab == "quota")
    f = normalize_filters(filters)
    start = max(0, cint(start))
    if tab == "quota":
        source = BudgetQuotaLocalSource()
        scope = quota_filters(f)
        result = source.list_quotas(page=start // 20 + 1, page_size=20, **scope)
        result["summary"] = source.get_summary(**scope)
        result["total_count"] = result["total"]
        return result
    level, issues = review_columns()
    where, params = ledger_where(f, review=view == "review", issues=issues)
    totals = frappe.db.sql(f"""SELECT COUNT(*) AS total_count, COALESCE(SUM(COALESCE(total_amount, amount, 0)), 0) AS amount,
        COUNT(DISTINCT NULLIF(NULLIF(company, ''), '待确认')) AS company_count,
        SUM(CASE WHEN {issues} THEN 1 ELSE 0 END) AS review_count
        FROM `tabBudget Ledger Entry` WHERE {where}""", params, as_dict=True)[0]
    if view == "overview":
        review_where, review_params = ledger_where(f, review=True, issues=issues)
        totals["review_count"] = frappe.db.sql(f"SELECT COUNT(*) FROM `tabBudget Ledger Entry` WHERE {review_where}", review_params)[0][0]
    rows = frappe.db.sql(f"""SELECT name, form_no, company, department, department_id, budget_kind, budget_month,
        application_date, status, total_amount, amount, detail_amount, dept_path_ids, dept_path_names,
        {level} AS reviewed_organization_level FROM `tabBudget Ledger Entry`
        WHERE {where} ORDER BY budget_month DESC, application_date DESC, name DESC LIMIT %(limit)s OFFSET %(offset)s""",
        {**params, "limit":20, "offset":start}, as_dict=True)
    for row in rows:
        row["issues"] = issues_for(row)
    groups = {}
    if view == "overview":
        for group in ("company", "budget_month", "budget_kind"):
            groups[group] = frappe.db.sql(f"""SELECT {group} AS label, COUNT(*) AS count,
                SUM(COALESCE(total_amount, amount, 0)) AS amount FROM `tabBudget Ledger Entry`
                WHERE {where} GROUP BY {group} ORDER BY {group}""", params, as_dict=True)
    return {**totals, "rows": rows, "groups": groups, "filters": f}


def is_company_level(row):
    ids, names = _json_list(row.get("dept_path_ids")), _json_list(row.get("dept_path_names"))
    company = str(row.get("company") or "").strip()
    if row.get("reviewed_organization_level"):
        return (row["reviewed_organization_level"] == "company" and bool(company) and company != "待确认"
                and bool(str(row.get("department_id") or "").strip()))
    return (bool(company) and company != "待确认" and 0 < len(ids) <= COMPANY_LEVEL_INDEX + 1
            and len(ids) == len(names) and bool(str(row.get("department_id") or "").strip())
            and str(row["department_id"]).strip() == ids[-1] and company == names[-1].strip())


def issues_for(row):
    issues = []
    if not row.get("company") or row["company"] == "待确认":
        issues.append("Budget company is missing")
    if not str(row.get("department") or "").strip() and not is_company_level(row):
        issues.append("Budget department is missing")
    if not row.get("budget_month"):
        issues.append("Budget month is missing")
    total = row.get("total_amount") if row.get("total_amount") is not None else row.get("amount")
    if abs(Decimal(str(total or 0)) - Decimal(str(row.get("detail_amount") or 0))) > Decimal("0.01"):
        issues.append("Budget amount differs from detail total")
    return issues


@frappe.whitelist()
def export_data(filters=None, view="details", tab="ledger"):
    """Bounded export of the same scope; a missing source must never produce zero totals."""
    import base64
    from frappe.utils.xlsxutils import make_xlsx

    _check(tab == "quota")
    f = normalize_filters(filters)
    if tab == "quota":
        where, params = BudgetQuotaLocalSource()._filters(**quota_filters(f))
        rows = frappe.db.sql(f"""SELECT q.form_no, q.budget_month, q.company_name, q.department_name, q.budget_type, q.total_amount, q.detail_amount
            FROM `tabBudget Quota` q WHERE {where} ORDER BY q.budget_month DESC, q.name LIMIT 10001""", params)
    else:
        issues = review_columns()[1]
        where, params = ledger_where(f, review=view == "review", issues=issues)
        rows = frappe.db.sql(f"""SELECT form_no, budget_month, company, department, budget_kind, COALESCE(total_amount, amount, 0), detail_amount
            FROM `tabBudget Ledger Entry` WHERE {where} ORDER BY budget_month DESC, name LIMIT 10001""", params)
    if len(rows) > 10000:
        frappe.throw(_("Narrow the date range to export at most 10000 budget records"))
    header = [_(label) for label in ("Approval Number", "Budget Month", "Subsidiary", "Department", "Budget Type", "Amount", "Detail Amount")]
    content = [header]
    for row in rows:
        values = list(row)
        values[4] = _("Production Budget") if values[4] == "production" else _("Non-production Budget")
        # Keep spreadsheet data as text when it could otherwise become a formula.
        values = ["'" + value if isinstance(value, str) and value.startswith(("=", "+", "-", "@")) else value for value in values]
        content.append(values)
    return {"content":base64.b64encode(make_xlsx(content, _("Budget")).getvalue()).decode("ascii")}
