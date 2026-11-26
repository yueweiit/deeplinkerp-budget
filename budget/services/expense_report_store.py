"""Private, replaceable reporting data; approval evidence stays with its source.

Readers hold a shared publication lock for their request. The publisher takes
the exclusive lock and updates changed lines and the version in one transaction.
No permission is granted on these DocTypes: all reads use the reporting service.
"""
import hashlib
import json
from decimal import Decimal

import frappe

STATE = "Expense Report State"
LINE = "Expense Report Line"
CURRENT = "current"
MAX_LINES = 50000


def encode(value):
	return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def digest(value):
	return hashlib.sha256(encode(value).encode()).hexdigest()


def installed():
	return bool(getattr(frappe.local, "site", None)) and frappe.db.table_exists(STATE) and frappe.db.table_exists(LINE)


def state(*, lock=None):
	sql = "select * from `tabExpense Report State` where name=%s"
	if lock == "read":
		frappe.db.multisql({"mariadb": sql + " LOCK IN SHARE MODE",
			"postgres": sql.replace("`", '"') + " FOR SHARE"}, (CURRENT,), as_dict=True)
		# InnoDB locking reads see the latest committed row, while a request
		# may already have established an older repeatable-read snapshot when
		# checking Company permissions/source versions. Read metadata through
		# that same snapshot as the line queries, after securing the lock.
		rows = frappe.db.sql(sql, (CURRENT,), as_dict=True)
	else:
		rows = frappe.db.sql(sql + (" FOR UPDATE" if lock == "write" else ""), (CURRENT,), as_dict=True)
	return rows[0] if rows else None


def pack(row):
	values = {"name": row["key"], "source_key": row["source_key"], "company": row.get("company") or "",
		"scope_company": row.get("scope_company") or "", "reporting_month": row.get("month") or "",
		"is_legacy": int(bool(row.get("is_legacy"))),
		"application_date": row.get("application_date"), "kind": row["kind"], "approval_status": row["status"],
		"management_category": row.get("management_category") or "", "source_category_key": row["source_category_key"],
		"source_path_json": encode(row.get("source_path") or []), "currency": row.get("currency") or "",
		"original_amount": row.get("amount") or "0", "original_amount_known": int(row.get("amount") is not None),
		"eligible": int(row["eligible"]), "has_issues": int(bool(row["issues"])),
		"amount_cny": row["amounts"]["CNY"] or "0", "amount_mxn": row["amounts"]["MXN"] or "0",
		"cny_known": int(row["amounts"]["CNY"] is not None), "mxn_known": int(row["amounts"]["MXN"] is not None)}
	# Small per-line presentation data and an internal provenance revision only.
	# No raw approvals, attachments, workflow evidence or allocation plans.
	from .expense_management import PUBLIC
	values["detail_json"] = encode({key: row.get(key) for key in (
		*PUBLIC, "source_key", "budget_categories_complete", "allocation_modified")})
	values["row_hash"] = digest(values)
	return values


def unpack(row):
	return json.loads(row["detail_json"])


def publish(rows, values):
	"""Caller owns commit/rollback. Readers cannot see a partially replaced set."""
	if len(rows) > MAX_LINES or len({row["key"] for row in rows}) != len(rows):
		raise ValueError("Invalid expense reporting line identities or size")
	if not state(lock="write"):
		raise RuntimeError("Expense reporting state is not installed")
	previous = dict(frappe.db.sql("select name, row_hash from `tabExpense Report Line`"))
	packed = [pack(row) for row in rows]
	changed = [row for row in packed if previous.get(row["name"]) != row["row_hash"]]
	removed = set(previous) - {row["name"] for row in packed}
	# Delete/reinsert only derived records that changed. The transaction also
	# removes approvals/allocations no longer present in the authoritative set.
	delete = sorted(removed | {row["name"] for row in changed if row["name"] in previous})
	for offset in range(0, len(delete), 500):
		frappe.db.delete(LINE, {"name": ["in", delete[offset:offset + 500]]})
	if changed:
		columns = list(changed[0]) + ["creation", "modified", "owner", "modified_by", "docstatus", "idx"]
		now = frappe.utils.now_datetime()
		frappe.db.bulk_insert(LINE, columns,
			[tuple(row.get(key) for key in columns[:-6]) + (now, now, "Administrator", "Administrator", 0, 0)
				for row in changed], chunk_size=500)
	frappe.db.set_value(STATE, CURRENT, values)
	return {"lines": len(rows), "changed": len(changed), "removed": len(removed)}


def where(context, f, *, categories=True):
	"""Always apply current Company permissions, including secondary ownership."""
	companies = sorted(context["companies"])
	params = {}
	if companies:
		params["companies"] = companies
		owned = "company in %(companies)s"
		scope = "(scope_company='' or scope_company in %(companies)s)"
	else:
		owned, scope = "1=0", "scope_company=''"
	if context["manager"]:
		owned = f"({owned} or company='')"
	conditions = [owned, scope]
	if context.get("legacy_keys"):
		conditions.append("(is_legacy=0 or name in %(legacy_keys)s)")
		params["legacy_keys"] = sorted(context["legacy_keys"])
	else:
		conditions.append("is_legacy=0")
	if f["company"]:
		conditions.append("company=%(company)s")
		params["company"] = f["company"]
	if f.get("region"):
		names = sorted(name for name, region in context.get("company_regions", {}).items()
			if name in context["companies"] and region == f["region"])
		conditions.append("company in %(region_companies)s" if names else "1=0")
		if names:
			params["region_companies"] = names
	if categories:
		if f["category"]:
			conditions.append("management_category=%(category)s")
			params["category"] = "" if f["category"] == "unclassified" else f["category"]
		if f["source_category"]:
			conditions.append("source_category_key=%(source_category)s")
			params["source_category"] = f["source_category"]
	return " and ".join(conditions), params


def period(f, *, undated=False):
	return "(reporting_month=%(month)s or reporting_month='')" if undated else "reporting_month=%(month)s"


def detail_condition(f):
	if f["view"] == "review":
		return "has_issues=1 and approval_status in ('approved','pending','unknown')"
	return "kind!='budget'" + (" and approval_status='approved' and eligible=1" if f["measure"] == "approved" else "")


def select(conditions, params, *, limit=MAX_LINES + 1, start=0):
	return [unpack(row) for row in frappe.db.sql(
		"select detail_json from `tabExpense Report Line` where " + conditions
		+ " order by application_date desc, name desc limit %(limit)s offset %(start)s",
		{**params, "limit": limit, "start": start}, as_dict=True)]


def count(conditions, params):
	return frappe.db.sql("select count(*) from `tabExpense Report Line` where " + conditions, params)[0][0]


def summaries(conditions, params, *, group=None, review_currency=None):
	assert group in (None, "company", "management_category", "source_category_key", "reporting_month")
	assert review_currency in (None, "CNY", "MXN")
	review = "has_issues"
	if review_currency:
		# Match expense_regions.needs_currency_review without loading all detail
		# rows. An absent/unknown issue list remains blocked, not assumed FX-only.
		review = (f"case when eligible=1 and {review_currency.lower()}_known=1 "
			"and JSON_EXTRACT(detail_json, '$.issues')=%(fx_only_issues)s then 0 else has_issues end")
		params = {**params, "fx_only_issues": encode(["exchange_rate"])}
	approved = "kind!='budget' and approval_status='approved' and eligible=1"
	columns = [f"count(case when {approved} then 1 end) as line_count",
		f"count(distinct case when {approved} then source_key end) as applications",
		"count(case when kind!='budget' and approval_status='pending' then 1 end) as pending",
		f"count(case when kind!='budget' and approval_status in ('approved','pending','unknown') and ({review})=1 then 1 end) as review",
		"count(case when kind!='budget' and approval_status='approved' and eligible=0 then 1 end) as excluded"]
	for currency in ("cny", "mxn"):
		columns.append(f"case when count(case when {approved} and {currency}_known=0 then 1 end)>0 then null "
			f"else coalesce(sum(case when {approved} then amount_{currency} else 0 end),0) end as {currency}")
	if group:
		columns.insert(0, group)
	rows = frappe.db.sql("select " + ",".join(columns) + " from `tabExpense Report Line` where " + conditions
		+ (" group by " + group if group else ""), params, as_dict=True)
	return [{**({group: row[group]} if group else {}), "count": row.line_count, "applications": row.applications,
		"pending": row.pending, "review": row.review, "excluded": row.excluded,
		"amounts": {currency.upper(): None if row[currency] is None else str(Decimal(row[currency]).quantize(Decimal("0.01")))
			for currency in ("cny", "mxn")}} for row in rows]
