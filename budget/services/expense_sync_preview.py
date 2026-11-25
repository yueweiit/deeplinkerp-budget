"""Bounded preview of one approval template, without financial-node inference."""
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import frappe
from frappe import _

from deeplinkerp_branding.services import mexico_expenses as mx
from deeplinkerp_branding.services.regional_expenses import application_date_info, management_date
from . import cn_approval_contract as contract, cn_approval_settings as settings


def preview(row, business_timezone, corporations, processors):
	try:
		ZoneInfo(business_timezone)
	except (ValueError, TypeError, ZoneInfoNotFoundError):
		frappe.throw(_("Invalid business timezone"))
	templates = {}
	for processor in processors:
		prefix = "application" if processor == "cn" else "expense"
		item = {key: row[key] for key in ("corp_id", "process_code", "approval_type", "template_name")}
		item.update(enabled=0, field_mapping=row.get(prefix + "_fields") or "{}",
			classification_fields=row.get(prefix + "_classification") or "")
		if processor == "cn":
			templates[processor] = settings.clean_templates([item], corporations)[0]
		else:
			try:
				templates[processor] = mx.templates.normalize(item, require_finance=False)
			except (ValueError, TypeError):
				frappe.throw(_("Invalid approval template"))
	url, allowed = mx.http.configuration()
	if row["corp_id"] not in allowed:
		frappe.throw(_("Duplicate or unauthorized approval template"))
	# Both adapters inspect exactly the same sample. Never count it twice or
	# publish raw approval fields, task records or attachments to the browser.
	rows, _page = mx.http.read_template_page(url, row["corp_id"], row["process_code"], limit=3)
	issues = set()
	if not rows:
		issues.add(_("No sample approvals found; save this template disabled first"))
	for raw in rows:
		applied = None
		if "mx" in templates:
			source, lines = mx.projection.project(raw, templates["mx"], business_timezone, include_finance=False)
			problems = {issue for line in lines for issue in line["base_issues"]}
			applied, date_state = application_date_info(raw)
			applied, _basis = management_date(applied, date_state, source.get("request_date"), allow_submission=True)
			if not applied:
				problems.add("period")
			issues.update(_(contract.ISSUES["period"] if key == "period" else mx.contract.ISSUES.get(key, "Needs review"))
				for key in problems)
		if "cn" in templates:
			item = contract.project(raw, templates["cn"])
			# Shared templates use the effective management date, not the CN
			# adapter's stricter legacy date rule. All other issues stay visible.
			issues.update(_(contract.ISSUES[key]) for key in item["issues"] if key != "period" or not applied)
	return [{"rule": "approval", "count": len(rows), "issues": sorted(issues)}]
