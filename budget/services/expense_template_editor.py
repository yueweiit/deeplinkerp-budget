"""Bounded template discovery and financial preview; never publish raw OA payloads."""
import json
import re
from decimal import Decimal

import frappe
from frappe import _

from deeplinkerp_branding.services import expense_template_mapping as bindings
from deeplinkerp_branding.services import mexico_expense_templates as templates
from . import expense_sync_preview as preview
from deeplinkerp_branding.services.regional_expenses import application_date_info, management_date

HEADER_LABELS = {
	"entity": "Expense-bearing entity", "amount": "Total amount", "currency": "Currency",
	"application_date": "Application date", "budget_month": "Budget month", "description": "Description",
	"expense_type": "Expense category", "operating_category": "Operating category", "selling_category": "Selling category",
	"purchase_category": "Purchase category", "personnel_category": "Personnel category", "tax_category": "Tax category",
	"utilities_category": "Utilities category", "logistics_category": "Logistics category", "services_category": "Services category",
	"planned": "Planned payment date", "payee": "Payee", "payment_terms": "Payment terms", "salary_type": "Payroll type",
	"budget": "Budget snapshot", "budget_used": "Used budget snapshot", "delivery": "Delivery date",
	"production": "Production type", "request_type": "Request type",
	"allocation_table": "Department allocation details", "salary_table": "Payroll details", "social_table": "Social contribution details",
	"office_table": "Office expense details", "items_table": "Purchase items", "processors_table": "Processor details",
	"expense_table": "Expense details", "hr_table": "Personnel budget details", "budget_office_table": "Office budget details",
	"operation_table": "Operating budget details",
}
CHILD_LABELS = {"entity": "Department or entity", "amount": "Detail amount", "description": "Detail note",
	"quantity": "Quantity", "unit_price": "Unit price", "label": "Budget item",
	"original_amount": "Original amount", "currency": "Currency", "exchange_rate": "Exchange rate"}


def components(raw):
	return preview.contract.decode(raw.get("form_component_values") or (raw.get("raw_payload") or {}).get("formComponentValues"), [])


def catalog(rows):
	headers, tables = {}, {}
	def metadata(field, child=False):
		identity = field.get("key" if child else "id")
		if not isinstance(identity, str) or not re.fullmatch(bindings.ID_PATTERN, identity):
			return None
		kind = field.get("componentType") or (re.split("[-_]", identity, maxsplit=1)[0] if child else "")
		if kind.startswith(("DDPhoto", "DDAttachment", "TextNote")):
			return None
		return {"id": identity, "label": str(field.get("label" if child else "name") or identity)[:200], "type": kind}
	for raw in rows:
		fields = components(raw)
		if not isinstance(fields, list) or len(fields) > 500 or any(not isinstance(f, dict) for f in fields):
			raise ValueError("Invalid form components")
		for field in fields:
			item = metadata(field)
			if not item: continue
			headers[item["id"]] = item
			if item["type"] != "TableField": continue
			target = tables.setdefault(item["id"], {})
			entries = preview.contract.decode(field.get("value"), [])
			if not isinstance(entries, list) or len(entries) > 2000:
				raise ValueError("Invalid detail table")
			for entry in entries:
				cells = entry.get("rowValue") if isinstance(entry, dict) else None
				if not isinstance(cells, list) or len(cells) > 200 or any(not isinstance(c, dict) for c in cells):
					raise ValueError("Invalid detail table")
				for cell in cells:
					if value := metadata(cell, True): target[value["id"]] = value
			if len(headers) + sum(map(len, tables.values())) > 1500:
				raise ValueError("Too many template fields")
	return {"fields": list(headers.values()), "tables": {key: list(value.values()) for key, value in tables.items()}}


def sample_preview(raw, configs, timezone, index):
	issues, lines = set(), []
	source, applied = None, None
	if "mx" in configs:
		source, lines = preview.mx.projection.project(raw, configs["mx"], timezone, include_finance=False)
		issues.update(preview.mx.contract.ISSUES.get(issue, "Needs review") for line in lines for issue in line["base_issues"])
		applied, state = application_date_info(raw, configs["mx"])
		applied, _basis = management_date(applied, state, source.get("request_date"), allow_submission=True)
		if not applied: issues.add(preview.contract.ISSUES["period"])
	if "cn" in configs:
		cn = preview.contract.project(raw, configs["cn"])
		issues.update(preview.contract.ISSUES[key] for key in cn["issues"] if key != "period" or not applied)
		if source is None:
			source = {"total_amount": cn["amount"], "currency": cn["currency"]}
			lines = [{"source_entity": detail.get("label"), "amount": detail["amount"],
				"currency": detail.get("amount_currency", "CNY"), "base_issues": []}
				for table in cn["tables"] if table["is_amount"] for detail in table["rows"]]
	for config in configs.values():
		fields = components(raw)
		resolved, _errors = templates.resolve(fields, config, schema=bindings.schema(config["approval_type"]))
		for key, ids in bindings.decode(config).items():
			if key in bindings.RESERVED: continue
			if not resolved.get(key):
				issues.add(_("Sample {0}: mapped field missing or ambiguous: {1}").format(index, _(HEADER_LABELS.get(key, key))))
		bound = {field.get("id"): key for key, field in resolved.items() if key.endswith("_table") and field}
		for field in fields:
			if field.get("componentType") != "TableField" or field.get("value") in (None, "", [], "[]"): continue
			kind = bound.get(field.get("id"))
			if not kind:
				issues.add(_("Sample {0}: unmapped detail table: {1}").format(index, str(field.get("name") or field.get("id"))[:200]))
			for entry in preview.contract.decode(field.get("value"), []):
				for role in bindings.decode(config).get("table_fields", {}).get(kind, {}):
					if not bindings.child(config, kind, role, entry["rowValue"]):
						issues.add(_("Sample {0}: mapped detail field missing or ambiguous: {1} / {2}").format(index,
							_(HEADER_LABELS.get(kind, kind)), _(CHILD_LABELS[role])))
	values = [Decimal(line["amount"]) for line in lines if line.get("amount") is not None]
	currencies = {line.get("currency") for line in lines}
	return {"index": index, "header_total": source.get("total_amount"), "currency": source.get("currency"),
		"detail_total": str(sum(values, Decimal(0))) if lines and len(values) == len(lines) and len(currencies) == 1 else None,
		"detail_currency": next(iter(currencies)) if len(currencies) == 1 else None,
		"line_count": len(lines), "issues": sorted({_(issue) for issue in issues}),
		"lines": [{"entity": str(line.get("source_entity") or "")[:200], "amount": line.get("amount"),
			"currency": line.get("currency")} for line in lines[:20]]}


def inspect(row, business_timezone, corporations, processors):
	configs = preview.template_configs(row, business_timezone, corporations, processors)
	url, allowed = preview.mx.http.configuration()
	if row["corp_id"] not in allowed:
		frappe.throw(_("Duplicate or unauthorized approval template"))
	rows, _page = preview.mx.http.read_template_page(url, row["corp_id"], row["process_code"], limit=3)
	if len(rows) > 3:
		frappe.throw(_("Template sample exceeds the preview limit."))
	try:
		result = catalog(rows)
		samples = [sample_preview(raw, configs, business_timezone, index) for index, raw in enumerate(rows, 1)]
	except (ValueError, TypeError, KeyError, AttributeError):
		frappe.throw(_("Template sample structure is invalid. Review the OA template."))
	kind = row["approval_type"]
	schema = bindings.schema(kind)
	result.update(sample_count=len(rows), samples=samples,
		processors=list(configs), defaults={}, fallbacks={},
		headers=[{"key": key, "label": _(HEADER_LABELS[key]), "types": list(spec[2])} for key, spec in schema.items() if key in HEADER_LABELS],
		table_roles={key: [{"key": role, "label": _(CHILD_LABELS[role]), "types": list(bindings.CHILD_TYPES[role])} for role in roles]
			for key, roles in bindings.TABLE_ROLES[kind].items()})
	for config in configs.values():
		for raw in rows:
			resolved, _errors = templates.resolve(components(raw), config, schema=schema)
			for key, field in resolved.items():
				if field.get("id"):
					result["defaults"].setdefault(key, [])
					if field["id"] not in result["defaults"][key]: result["defaults"][key].append(field["id"])
	for raw in rows:
		resolved, _errors = templates.resolve(components(raw), {"field_mapping": "{}"}, schema=schema)
		for key, field in resolved.items():
			if field.get("id"):
				result["fallbacks"].setdefault(key, [])
				if field["id"] not in result["fallbacks"][key]: result["fallbacks"][key].append(field["id"])
	return result
