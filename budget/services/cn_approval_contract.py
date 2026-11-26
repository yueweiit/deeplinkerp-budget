"""Read-only CN approval presentation. Header totals are never added to detail totals."""
from __future__ import annotations

import hashlib
import json
import re
from datetime import date, datetime, timezone
from decimal import Decimal

from deeplinkerp_branding.services import mexico_expense_templates as templates
from deeplinkerp_branding.services import mexico_expense_classification as classification
from deeplinkerp_branding.services import expense_template_mapping as bindings
from deeplinkerp_branding.services.mexico_expense_contract import exact_amount, approval_status, organization_id
from deeplinkerp_branding.services.mexico_expense_projection import text

KINDS = ("budget", "operating", "purchase")
CURRENCIES = {"人民币": "CNY", "人民币rmb": "CNY", "人民币cny": "CNY", "rmb": "CNY", "cny": "CNY",
	"美元": "USD", "美元usd": "USD", "usd": "USD", "美元dólar": "USD", "dólar": "USD",
	"比索": "MXN", "比索peso": "MXN", "peso": "MXN", "mxn": "MXN"}
BUDGET_TABLES = {"TableField_1VIO6UO9ZJI80": "hr", "TableField_MM293T6SVHC0": "office", "TableField_7MP8ZOFXR3S0": "operation"}
ALLOCATION_TABLES = {"TableField_1T6MS9XEQUE80", "TableField_G2ELEALN0S80"}
ISSUES = {
	"structure": "Form fields are ambiguous or incomplete",
	"amount": "Application amount is missing or invalid",
	"currency": "Currency is missing or unrecognized",
	"period": "Reporting month or application date is missing",
	"classification": "Business classification needs review",
	"organization": "Organization has not been matched to a company",
	"detail_amount": "Detail amounts are incomplete",
	"detail_mismatch": "Detail total differs from the application amount",
	"detail_currency": "Detail and application currencies differ; amounts cannot be compared directly",
	"status": "Approval status needs review",
}


def normalized(value):
	return "".join(text(value).split()).casefold()


def amount(value):
	return None if value in (None, "") else exact_amount(value)


def currency(value):
	return CURRENCIES.get(normalized(value))


def decode(value, default):
	if value in (None, ""):
		return default
	return json.loads(value) if isinstance(value, str) else value


def pick(fields, ids=(), prefixes=(), *, issues=None):
	matched = [field for field in fields if field.get("id", field.get("key")) in ids]
	if not matched:
		matched = [field for field in fields if any(text(field.get("name", field.get("label"))).startswith(p) for p in prefixes)]
	populated = [field for field in matched if field.get("value") not in (None, "", [], "[]")]
	if len(populated) > 1:
		if issues is not None:
			issues.append("structure")
		return {}
	return (populated or matched or [{}])[0]


def iso_date(value, *, month=False):
	value = text(value)
	try:
		if month and re.fullmatch(r"\d{4}-\d{2}", value):
			value += "-01"
		return date.fromisoformat(value).isoformat()[:7 if month else 10]
	except ValueError:
		return None


def approval_finished_at(raw):
	"""Only the source completion instant may order approved budget versions."""
	try:
		value = datetime.fromisoformat(str(raw.get("finishTime") or ""))
		return value.astimezone(timezone.utc).isoformat() if value.tzinfo else None
	except (ValueError, TypeError):
		return None


def _table(field, family, issues, *, is_amount, department_allocation=False, template=None, table_kind=None, header_currency=None):
	template = template or {}
	try:
		rows = decode(field.get("value"), [])
		if not isinstance(rows, list) or len(rows) > 2000:
			raise ValueError()
		output = []
		for index, row in enumerate(rows):
			cells = row.get("rowValue")
			if not isinstance(cells, list) or any(not isinstance(cell, dict) for cell in cells):
				raise ValueError()
			if bindings.missing_children(template, table_kind, cells):
				issues.append("structure")
			cell = lambda prefixes: pick(cells, prefixes=prefixes, issues=issues).get("value")
			def mapped(role, fallback):
				if bindings.child_configured(template, table_kind, role):
					return bindings.child(template, table_kind, role, cells).get("value")
				return fallback()
			visible = [{"label": text(c.get("label")), "value": text(c.get("value"))[:2000]}
				for c in cells if c.get("label") and not text(c.get("key")).startswith("TextNote") and c.get("value") not in (None, "")]
			if is_amount and department_allocation:
				value = bindings.child(template, table_kind, "amount", cells, *templates.ALLOCATION_AMOUNT_FIELD).get("value")
			else:
				value = mapped("amount", lambda: cell(("人民币金额", "Monto en yuanes")) if family == "budget" else cell(("金额", "总金额", "Monto", "Importe"))) if is_amount else None
			output.append({"index": index + 1, "amount": amount(value), "cells": visible,
				"label": text(mapped("label", lambda: cell(("明细项目", "预算明细", "部门", "Ítem", "Departamento")))),
				"original_amount": amount(mapped("original_amount", lambda: cell(("原币金额", "Monto en moneda")))) if is_amount else None,
				"currency": currency(mapped("currency", lambda: cell(("币种", "Moneda")))) if is_amount else None,
				"exchange_rate": text(mapped("exchange_rate", lambda: cell(("汇率", "Tipo de cambio", "Tasa de cambio")))) if is_amount else ""})
			if department_allocation:
				output[-1]["amount_currency"] = bindings.allocation_currency(template, header_currency)
			elif family == "operating" and (table_kind in bindings.decode(template)
					or table_kind in bindings.decode(template).get("table_fields", {})):
				output[-1]["amount_currency"] = header_currency
			if bindings.child_configured(template, table_kind, "entity"):
				owner = bindings.child(template, table_kind, "entity", cells)
				from deeplinkerp_branding.services.mexico_expense_contract import _entity
				label, multiple = _entity(owner.get("value"))
				if not label or multiple:
					issues.append("structure")
				output[-1].update(entity=label, entity_id=organization_id(owner), label=label)
		return output
	except (ValueError, TypeError, AttributeError):
		issues.append("structure")
		return []


def project(row, template):
	"""Project an approval from a configured template, regardless of execution region."""
	family = template["approval_type"]
	if family not in KINDS or row.get("corp_id") != template["corp_id"] or row.get("process_code") != template["process_code"]:
		raise ValueError("Unexpected CN approval identity")
	if not row.get("process_instance_id"):
		raise ValueError("Missing CN approval identity")
	raw = row.get("raw_payload") or {}
	if not isinstance(raw, dict):
		raise ValueError("Invalid approval envelope")
	fields = decode(row.get("form_component_values") or raw.get("formComponentValues"), [])
	if not isinstance(fields, list) or len(fields) > 500 or any(not isinstance(f, dict) for f in fields):
		raise ValueError("Invalid approval fields")
	issues = []
	mapping = bindings.decode(template)
	value = lambda ids=(), prefixes=(): pick(fields, ids, prefixes, issues=issues).get("value")
	def mapped_field(key, ids=(), prefixes=()):
		# Explicit bindings are authoritative, including invalid/missing targets.
		# Only unmapped fields may use legacy prefix matching and its diagnostics.
		if key in mapping:
			return resolved.get(key, {})
		return pick(fields, ids, prefixes, issues=issues)
	if family == "budget":
		resolved, errors = templates.resolve(fields, template, schema=bindings.BUDGET_FIELDS)
		issues.extend(errors)
		total = amount(mapped_field("amount", ("MoneyField_1JA1U3NP3Q4G0", "MoneyField_NAO8CPC23000"), ("预算总金额", "Presupuesto Total")).get("value"))
		unit = "CNY"
		entity_field = mapped_field("entity", ("DepartmentField-K0C3VCCH",), ("部门", "Departamento"))
		entity = text(entity_field.get("value"))
		period = iso_date(mapped_field("budget_month", ("DDDateField_RG3T9I5UX1S0",), ("预算月份", "Mes presupuestario")).get("value"), month=True)
		applied = iso_date(mapped_field("application_date", ("DDDateField_TFZ1WT5RP9C0",), ("申请日期", "Fecha de solicitud")).get("value"))
		category = text(mapped_field("expense_type", ("DDSelectField_1Y8RI4CCPWN40",), ("预算类型", "Tipo de presupuesto")).get("value"))
		path = [{"label": category}] if category else []
		description = text(mapped_field("description", ("TextareaField_1JHTRP1E89TS0",), ("备注", "Observaciones")).get("value"))
	else:
		# CN scope comes from the configured corporation and template. Ignore
		# legacy region bindings without changing the shared MX field resolver.
		resolved, errors = templates.resolve(fields, template,
			schema={key: spec for key, spec in templates.FIELDS.items() if key != "region"})
		issues.extend(errors)
		get = lambda key: resolved.get(key, {}).get("value")
		total, unit, entity = amount(get("amount")), currency(get("currency")), text(get("entity"))
		entity_field = resolved.get("entity") or {}
		applied = iso_date(mapped_field("application_date", *bindings.APPLICATION_DATE[:2]).get("value"))
		period = applied[:7] if applied else None
		path, errors = classification.extract({**row, "form_component_values": fields}, template)
		issues.extend(errors)
		category = path[0]["label"] if path else ""
		description = text(get("description"))
	status = approval_status(row.get("status"), row.get("result"), bool(row.get("deleted_at")))
	if str(row.get("status") or "").upper() in {"CANCELED", "CANCELLED"}:
		status = "withdrawn"
	if status not in ("approved", "pending", "rejected", "terminated", "withdrawn"):
		status = "unknown"
		issues.append("status")
	if not total:
		# exact_amount returns '0.00' for a known zero; only missing/invalid is an issue.
		issues.append("amount")
	if not unit:
		issues.append("currency")
	if not period:
		issues.append("period")
	if not path:
		issues.append("classification")
	if len([f.get("id") for f in fields if f.get("id")]) != len({f.get("id") for f in fields if f.get("id")}):
		issues.append("structure")
	tables = []
	allocation_tables = set(ALLOCATION_TABLES)
	allocation_id = None
	if family == "operating":
		# Follow the same explicit binding as the shared expense-line adapter.
		allocation_tables.difference_update(templates.FIELDS["allocation_table"][0])
		if allocation_id := resolved.get("allocation_table", {}).get("id"):
			allocation_tables.add(allocation_id)
	for field in fields:
		if field.get("componentType") != "TableField" or not field.get("value"):
			continue
		is_amount = (field.get("id") in BUDGET_TABLES or any(text(field.get("name")).startswith(p)
			for p in ("人资预算", "办公场地预算", "预算项目"))) if family == "budget" else field.get("id") in allocation_tables
		table_kind = next((key for key, selected in resolved.items() if key.endswith("_table") and selected.get("id") == field.get("id")), None)
		if family == "budget":
			replaced = {identity for key in mapping if key in bindings.BUDGET_FIELDS and key.endswith("_table") for identity in bindings.BUDGET_FIELDS[key][0]}
			if field.get("id") in replaced: is_amount = False
			if table_kind in bindings.TABLE_ROLES["budget"]: is_amount = True
		elif family == "operating" and table_kind in bindings.TABLE_ROLES[family] and (
			table_kind in mapping or table_kind in mapping.get("table_fields", {})):
			is_amount = True
		rows = _table(field, family, issues, is_amount=is_amount,
			department_allocation=bool(allocation_id and field.get("id") == allocation_id),
			template=template, table_kind=table_kind, header_currency=unit)
		tables.append({"label": text(field.get("name")), "source_table_id": field.get("id"), "group": BUDGET_TABLES.get(field.get("id")),
			"is_amount": is_amount, "rows": rows})
		if family == "budget" and table_kind in bindings.TABLE_ROLES["budget"]:
			tables[-1]["group"] = {"hr_table": "hr", "budget_office_table": "office", "operation_table": "operation"}[table_kind]
	monetary = [detail for table in tables if table["is_amount"] for detail in table["rows"]]
	detail_total = None
	if monetary:
		if any(detail["amount"] is None for detail in monetary):
			issues.append("detail_amount")
		else:
			detail_total = str(sum((Decimal(detail["amount"]) for detail in monetary), Decimal(0)))
			if any(detail.get("amount_currency", "CNY") != unit for detail in monetary):
				issues.append("detail_currency")
			elif total is not None and abs(Decimal(total) - Decimal(detail_total)) > Decimal("0.01"):
				issues.append("detail_mismatch")
	entity_id = organization_id(entity_field)
	identity = hashlib.sha256(json.dumps([row["corp_id"], row["process_instance_id"]]).encode()).hexdigest()
	facts = []
	for key, label in (("production", "Production type"), ("request_type", "Request type"),
		("salary_type", "Payroll type"), ("payment_terms", "Payment terms"),
		("planned", "Planned payment date"), ("delivery", "Delivery date"), ("payee", "Payee")):
		field_value = text(resolved.get(key, {}).get("value"))
		if field_value:
			facts.append({"label": label, "value": field_value})
	project_name = text(value(("TextField_97MDU018WPC0",), ("项目归属", "Pertenencia del proyecto")))
	if project_name:
		facts.append({"label": "Project", "value": project_name})
	invoice = text(value(("DDSelectField_UXVS17GPVOW0",), ("是否有发票",)))
	if invoice:
		facts.append({"label": "Invoice declared", "value": invoice})
	attachments = []
	for field in fields:
		if field.get("componentType") == "DDAttachment":
			try:
				items = decode(field.get("value"), [])
				if not isinstance(items, list):
					raise ValueError()
				attachments.extend(text(item.get("fileName")) for item in items if isinstance(item, dict) and item.get("fileName"))
			except (ValueError, TypeError):
				issues.append("structure")
	return {"key": identity, "corp_id": row["corp_id"], "process_code": row["process_code"],
		"_routing_source": {"row": row, "template": template} if family != "budget" else None,
		"process_instance_id": row["process_instance_id"], "kind": family,
		"number": text(row.get("business_id") or raw.get("businessId") or row["process_instance_id"]),
		"applicant": text(row.get("originator_user_name")), "entity": entity, "entity_id": entity_id,
		"department_id": text(row.get("originator_dept_id")), "department": text(row.get("originator_dept_name")),
		"application_date": applied, "month": period, "status": status, "approved_at": approval_finished_at(raw) if status == "approved" else None,
		"amount": total, "currency": unit,
		"category": category, "classification": [node["label"] for node in path],
		"description": description[:12000], "facts": facts, "tables": tables, "detail_total": detail_total,
		"detail_currency": next(iter({detail.get("amount_currency", "CNY") for detail in monetary})) if monetary and len({detail.get("amount_currency", "CNY") for detail in monetary}) == 1 else None,
		"budget_snapshot": {key: amount(resolved.get(source, {}).get("value")) for key, source in
			(("budget", "budget"), ("used", "budget_used"))}, "attachments": attachments[:100],
		"issues": sorted(set(issues)), "updated_at": text(row.get("updated_at"))}
