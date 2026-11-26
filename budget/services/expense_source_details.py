"""Small original-line previews from an already authorized local source.

Never return raw forms, sibling expenses or allocation plans. These previews
are resolved on detail requests, not stored in administrator-built reports.
"""
from decimal import Decimal, InvalidOperation
from json import JSONDecodeError

from deeplinkerp_branding.services import mexico_expenses as mx
from deeplinkerp_branding.services import regional_expenses as regional

from .cn_approval_management import cell_value
from .expense_management import normalized_source_path


def decode(value):
	try:
		return regional.decode(value)
	except JSONDecodeError:
		return None


def same_amount(left, right):
	try:
		left, right = Decimal(str(left)), Decimal(str(right))
		return left.is_finite() and right.is_finite() and left == right
	except (InvalidOperation, ValueError):
		return False


def current_line(row, amount, currency, current=None):
	# A reusable reporting publication can lag behind a source refresh. Do not
	# present a newer allocation as the origin of an older displayed amount.
	if not same_amount(row.get("amount"), amount) or row.get("currency") != currency:
		return False
	current = current or {}
	if "source_path" in row and "source_path" in current and normalized_source_path(row) != normalized_source_path(current):
		return False
	return all((row.get(key) or "") == (current.get(key) or "")
		for key in ("company", "status", "description", "line_label")
		if key in current and key in row)


def preview(original, *, origin, table_label="", row_number="", split_count=1):
	return {"origin": origin, "table_label": table_label or "", "row_number": str(row_number or ""),
		"amount": str(original["amount"]) if original.get("amount") is not None else None,
		"currency": original.get("currency") or "", "description": original.get("description") or "",
		"source_entity": original.get("source_entity") or "", "split_count": split_count}


def projected_preview(original, source, *, split_count=1):
	if original.get("table_id") == "header":
		return preview(original, origin="header", split_count=split_count)
	# Table IDs stay internal; use the saved form's user-facing label when known.
	table_label = ""
	tables = (decode(source.get("details_json") or "{}") or {}).get("tables", [])
	# Labels cannot be inferred from a row number shared by several tables.
	if len(tables) == 1 and any(str(item.get("row_id")) == str(original.get("source_row_id")) for item in tables[0].get("rows", [])):
		table_label = tables[0].get("label") or ""
	return preview(original, origin="detail", table_label=table_label,
		row_number=original.get("source_row_id"), split_count=split_count)


def original_expense(kind, source, selected, row):
	if kind == "regional":
		# Amounts and display labels do not identify a source version: another
		# original line may occupy the same allocation index after a refresh.
		# Old publications without this internal revision cannot prove provenance.
		if (source.get("state") != "confirmed" or not selected.get("source_available", True)
				or not row.get("allocation_modified")
				or row["allocation_modified"] != str(source.get("modified") or "")):
			return None
		facts = decode(source.get("facts_json") or "null")
		if not isinstance(facts, list) or len(facts) != 2 or not isinstance(facts[0], dict) or not isinstance(facts[1], list):
			return None
		header, lines = facts
		matches = [line for line in lines if line.get("line_key") and line.get("line_key") == selected.get("line_key")]
		if len(matches) != 1:
			return None
		original = matches[0]
		currency = original.get("currency") or header.get("currency")
		current = {"company": selected.get("company"), "status": header.get("approval_status"),
			"description": original.get("description"), "line_label": original.get("description"),
			"source_path": regional.contract.management_path(original)}
		if not current_line(row, selected.get("amount"), currency, current):
			return None
		parts = [entry for entry in regional.decode(source.allocations_json) if entry.get("line_key") == original["line_key"]]
		return projected_preview({**original, "currency": currency}, header, split_count=len(parts))
	if kind == "native":
		if not current_line(row, selected.get("amount"), selected.get("currency"), selected):
			return None
		ti, di = selected.get("table_index", -1), selected.get("detail_index", -1)
		if ti < 0 or di < 0:
			return preview({**source, "source_entity": source.get("entity")}, origin="header")
		tables = source.get("tables") or []
		if ti >= len(tables) or di >= len(tables[ti].get("rows") or []):
			return None
		table, detail = tables[ti], tables[ti]["rows"][di]
		return preview({"amount": detail.get("amount"), "currency": detail.get("amount_currency") or source.get("currency"),
			"description": detail.get("description") or cell_value(detail, ("备注", "备注Nota", "说明", "Observaciones", "Descripción")) or detail.get("label") or "",
			"source_entity": detail.get("entity") or ""},
			origin="detail", table_label=table.get("label"), row_number=di + 1)
	if kind == "legacy":
		if not source.get("raw_json") or not selected.get("source_version") or selected["source_version"] != source.get("source_version"):
			return None
		raw = decode(source.raw_json)
		settings = mx._settings()
		template = next((item for item in mx._templates(settings) if item.get("corp_id") == source.get("corp_id")
			and item.get("process_code") == source.get("process_code")), None)
		if not isinstance(raw, dict) or not template:
			return None
		header, lines = mx.projection.project(raw, template, settings.business_timezone or "America/Mexico_City",
			ignore_region=True, include_finance=False)
		matches = [line for line in lines if line.get("line_key") == row["key"]]
		if len(matches) != 1 or not current_line(row, matches[0].get("amount"), matches[0].get("currency"),
			{"description": matches[0].get("description"), "line_label": matches[0].get("description"),
				"status": header.get("approval_status"), "source_path": regional.contract.management_path(matches[0])}):
			return None
		return projected_preview(matches[0], header)
	return None
