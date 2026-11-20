"""Read-only CN approval workbench endpoints."""
import base64
from decimal import Decimal

import frappe
from frappe import _

from ..services import cn_approval_workbench as service


@frappe.whitelist()
def get_data(filters=None, start=0):
	service.require_reader()
	return service.overview(filters, start)


@frappe.whitelist(methods=["POST"])
def refresh_data(filters=None):
	service.require_reader()
	return service.overview(filters, refresh=True)


@frappe.whitelist()
def get_detail(key):
	service.require_reader()
	return service.detail(key)


@frappe.whitelist()
def export_data(filters=None):
	frappe.only_for("System Manager")
	from frappe.utils.xlsxutils import make_xlsx
	f = service.filters(filters)
	_snapshot, rows, _context = service.load()
	rows = service.select(rows, f)
	if len(rows) > 10000:
		frappe.throw(_("Export exceeds 10000 approvals. Narrow the filters."))
	headers = ("Approval number", "Approval form", "Source organization", "Company", "Application date", "Reporting month",
		"Approval status", "Currency", "Application amount", "Business classification", "Review issues")
	kinds = {"budget": _("Budget declaration"), "operating": _("Operating expenditure"), "purchase": _("Purchase expenditure")}
	statuses = {key: _(label) for key, label in zip(service.STATUSES, ("Approved", "In progress", "Rejected", "Terminated", "Withdrawn", "Unknown"))}
	data = [[_(key) for key in headers]]
	for row in rows:
		data.append([row["number"], kinds[row["kind"]], row["entity"], row["company"], row["application_date"], row["month"],
			statuses[row["status"]], row["currency"], Decimal(row["amount"]) if row["amount"] is not None else None, row["category"],
			"; ".join(_(service.ISSUES[issue]) for issue in row["issues"])])
	# Keep source strings literal; approval numbers and source labels are untrusted.
	data = [["'" + value if isinstance(value, str) and value.startswith(("=", "+", "-", "@")) else value for value in line] for line in data]
	# Frappe's Data Export mode preserves literal source strings instead of
	# stripping HTML after formula protection (which could expose a leading '=').
	return {"filename": "cn-approvals.xlsx", "content": base64.b64encode(make_xlsx(data, "Data Export").getvalue()).decode()}
