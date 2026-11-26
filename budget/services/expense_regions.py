"""Currency presentation derived from authorized Company.country values."""
from decimal import Decimal

import frappe

REGIONS = {
	"cn": {"label": "China", "currency": "CNY", "country": "China"},
	"mx": {"label": "Mexico", "currency": "MXN", "country": "Mexico"},
}


def company_regions(context):
	allowed = set(context["companies"])
	if not allowed:
		return {}
	countries = {value["country"]: key for key, value in REGIONS.items()}
	return {row.name: countries.get(row.country, "") for row in frappe.get_list("Company",
		filters={"name": ["in", sorted(allowed)]}, fields=["name", "country"], limit_page_length=0)
		if row.name in allowed}


def company_names(mapping, filters):
	return sorted(name for name in mapping if (not filters.get("company") or name == filters["company"])
		and (not filters.get("region") or mapping[name] == filters["region"]))


def descriptors(mapping, filters):
	names = company_names(mapping, filters)
	return [{"region": key, "label": value["label"], "currency": value["currency"],
		"companies": [name for name in names if mapping[name] == key]}
		for key, value in REGIONS.items() if any(mapping[name] == key for name in names)]


def annotate(row, mapping):
	region = mapping.get(row.get("company"), "")
	currency = REGIONS.get(region, {}).get("currency")
	return {**row, "region": region, "display_currency": currency}


def needs_currency_review(row, currency):
	"""Ignore only an unrelated FX failure; retain every source validation issue."""
	issues = set(row["issues"])
	if row["eligible"] and row["amounts"].get(currency) is not None and issues == {"exchange_rate"}:
		return False
	return bool(issues)


def annotate_response(response, mapping):
	for field in ("company_rows", "budgets", "rows"):
		if field in response:
			response[field] = [annotate(row, mapping) for row in response[field]]
	totals = {row["region"]: row["summary"]["amounts"][row["currency"]] for row in response["regions"]}
	for row in response["company_rows"]:
		total, value = totals.get(row["region"]), row["amounts"].get(row["display_currency"])
		row["regional_share"] = (str(min(Decimal(100), max(Decimal(0), Decimal(value) / Decimal(total) * 100))
			.quantize(Decimal("0.1"))) if total is not None and Decimal(total) > 0 and value is not None else None)
	response["company_rows"].sort(key=lambda row: (list(REGIONS).index(row["region"]) if row["region"] in REGIONS else len(REGIONS),
		row["amounts"].get(row["display_currency"]) is None, -Decimal(row["amounts"].get(row["display_currency"]) or 0), row["company"]))
	return response
