"""Permission-scoped, read-only CN analytics over approval snapshots."""
from __future__ import annotations

import re
from collections import Counter, defaultdict
from copy import deepcopy
from decimal import Decimal

import frappe
from frappe import _

from .cn_approval_contract import KINDS, ISSUES, iso_date

STATUSES = ("approved", "pending", "rejected", "terminated", "withdrawn", "unknown")
SUMMARY_FIELDS = ("key", "kind", "number", "applicant", "entity", "company", "department", "application_date",
	"month", "status", "amount", "currency", "category", "classification", "issues", "description")


def require_reader():
	frappe.only_for(("System Manager", "Accounts Manager"))


def access_context():
	manager = frappe.session.user == "Administrator" or "System Manager" in frappe.get_roles()
	companies = set(frappe.get_list("Company", filters={"is_group": 0}, pluck="name", limit_page_length=0))
	# Identity discovery is server-only and separate from permission filtering.
	# Otherwise a known but forbidden company would look like an unmapped source.
	known_companies = set(frappe.get_all("Company", pluck="name", limit_page_length=0))
	# Exact server-managed aliases only. Source names never become legal companies by fuzzy matching.
	aliases = {}
	for item in frappe.conf.get("cn_approval_company_map") or []:
		if not isinstance(item, dict):
			frappe.throw(_("CN company mappings are ambiguous. Ask an administrator to review them."))
		key = (item.get("corp_id"), item.get("source_name"))
		if not all(key) or not item.get("company") or key in aliases:
			frappe.throw(_("CN company mappings are ambiguous. Ask an administrator to review them."))
		aliases[key] = item["company"]
	from .ledger_sync_settings import current_source
	source = current_source()
	organizations = {(item["corp_id"], item["department_id"]): item["company"]
		for item in source.get("organization_mappings", []) if item["corp_id"] == source["corp_id"]
		and (not source.get("bound_fingerprint") or source["bound_fingerprint"] == source["fingerprint"])}
	return {"manager": manager, "companies": companies, "known_companies": known_companies,
		"aliases": aliases, "organizations": organizations}


def authorized(rows, context):
	output = []
	for original in rows:
		row = deepcopy(original)
		company = context["aliases"].get((row["corp_id"], row["entity"]))
		if not company and row.get("entity_id"):
			company = context.get("organizations", {}).get((row["corp_id"], row["entity_id"]))
		if not company and row["entity"] in context["known_companies"]:
			company = row["entity"]
		if company and company not in context["companies"] or not company and not context["manager"]:
			continue
		row["company"] = company or ""
		if not company:
			row["issues"] = sorted(set(row["issues"]) | {"organization"})
		output.append(row)
	return output


def filters(values=None):
	values = frappe.parse_json(values) if isinstance(values, str) else values or {}
	if not isinstance(values, dict) or set(values) - {"from_month", "to_month", "kind", "status", "entity", "currency", "keyword", "category", "issue", "review"}:
		frappe.throw(_("Invalid CN approval filters."))
	month = frappe.utils.today()[:7]
	result = {key: str(values.get(key) or "").strip() for key in ("kind", "status", "entity", "currency", "keyword", "category", "issue")}
	result.update(from_month=str(values.get("from_month") or month), to_month=str(values.get("to_month") or month),
		review=values.get("review") in (True, 1, "1"))
	if (iso_date(result["from_month"], month=True) != result["from_month"] or iso_date(result["to_month"], month=True) != result["to_month"]
		or result["from_month"] > result["to_month"] or result["kind"] not in ("", *KINDS)
		or result["status"] not in ("", *STATUSES) or result["issue"] not in ("", *ISSUES)
		or result["currency"] and not re.fullmatch(r"[A-Z]{3}", result["currency"])
		or any(len(value) > 500 for value in result.values() if isinstance(value, str))):
		frappe.throw(_("Invalid CN approval filters."))
	return result


def select(rows, f):
	selected = []
	for row in rows:
		period = row["month"]
		if period is None:
			# Review can expose undated approvals; normal period statistics cannot.
			if not f["review"]:
				continue
		elif not f["from_month"] <= period <= f["to_month"]:
			continue
		if any(f[key] and row[key] != f[key] for key in ("kind", "status", "entity", "currency", "category")):
			continue
		if f["review"] and not row["issues"] or f["issue"] and f["issue"] not in row["issues"]:
			continue
		if f["keyword"] and f["keyword"].casefold() not in " ".join(str(row.get(k) or "") for k in ("number", "entity", "description", "applicant", "category")).casefold():
			continue
		selected.append(row)
	return sorted(selected, key=lambda row: (row["application_date"] or row["month"] or "", row["number"], row["key"]), reverse=True)


def money_totals(rows):
	totals, incomplete = defaultdict(Decimal), Counter()
	for row in rows:
		unit = row["currency"] or "unknown"
		if row["amount"] is None or not row["currency"] or "structure" in row["issues"]:
			incomplete[unit] += 1
		else:
			totals[unit] += Decimal(row["amount"])
	return [{"currency": unit, "amount": str(totals[unit]) if not incomplete[unit] else None,
		"known_amount": str(totals[unit]), "incomplete": incomplete[unit]} for unit in sorted(totals.keys() | incomplete.keys())]


def aggregate(rows):
	cards = []
	for kind in KINDS:
		group = [row for row in rows if row["kind"] == kind]
		cards.append({"kind": kind, "count": len(group), "approved": sum(row["status"] == "approved" for row in group),
			"pending": sum(row["status"] == "pending" for row in group),
			"totals": money_totals([row for row in group if row["status"] == "approved" and row["month"]])})
	groups, months = defaultdict(list), defaultdict(list)
	for row in rows:
		if row["status"] == "approved" and row["month"]:
			groups[(row["kind"], row["category"])].append(row)
			months[(row["month"], row["kind"])].append(row)
	return {"cards": cards, "count": len(rows), "review_count": sum(bool(row["issues"]) for row in rows),
		"issues": dict(Counter(issue for row in rows for issue in row["issues"])),
		"status_counts": dict(Counter(row["status"] for row in rows)),
		"categories": [{"kind": kind, "label": category, "count": len(group), "totals": money_totals(group)} for (kind, category), group in sorted(groups.items())],
		"months": [{"month": month, "kind": kind, "count": len(group), "totals": money_totals(group)} for (month, kind), group in sorted(months.items(), key=lambda item: (item[0][0] or "", item[0][1]))]}


def public_row(row):
	return {key: row[key] for key in SUMMARY_FIELDS if key in row}


def load(*, refresh=False):
	from ..integrations.cn_approval_source import get_snapshot
	require_reader()
	context = access_context()
	snapshot = get_snapshot(refresh=refresh)
	return snapshot, authorized(snapshot["rows"], context), context


def overview(values=None, start=0, *, refresh=False):
	f = filters(values)
	try:
		start = int(start)
	except (ValueError, TypeError):
		frappe.throw(_("Invalid CN approval filters."))
	if start < 0 or start > 20000:
		frappe.throw(_("Invalid CN approval filters."))
	snapshot, rows, context = load(refresh=refresh)
	selected = select(rows, f)
	return {"filters": f, "fetched_at": snapshot["fetched_at"], "summary": aggregate(selected),
		"rows": [public_row(row) for row in selected[start:start + 25]], "total": len(selected), "start": start, "page_length": 25,
		"options": {"entities": sorted({row["entity"] for row in rows if row["entity"]}),
			"currencies": sorted({row["currency"] for row in rows if row["currency"]}),
			"categories": sorted({row["category"] for row in rows if row["category"]})},
		"issue_labels": ISSUES, "can_export": context["manager"], "can_refresh": True}


def detail(key):
	if not isinstance(key, str) or not re.fullmatch(r"[a-f0-9]{64}", key):
		frappe.throw(_("Invalid approval identity."))
	_snapshot, rows, _context = load()
	row = next((row for row in rows if row["key"] == key), None)
	if row is None:
		frappe.throw(_("This approval is unavailable or you do not have permission to view it."), frappe.PermissionError)
	from deeplinkerp_branding.services.mexico_expenses import _dingtalk_url
	return {**public_row(row), "facts": row["facts"], "tables": row["tables"], "detail_total": row["detail_total"],
		"detail_currency": row["detail_currency"], "budget_snapshot": row["budget_snapshot"],
		"attachments": row["attachments"], "approval_url": _dingtalk_url(row)}
