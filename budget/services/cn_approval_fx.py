"""CN reporting-month policy over the shared MX currency arithmetic/transport."""
from deeplinkerp_branding.services.mexico_expense_fx import CURRENCIES, Conversion, converted_amount

from .cn_approval_contract import iso_date

DATE_BASES = {"budget": "Budget month", "application": "Application month"}


def annotate(rows, target):
	"""Derive conversion issues from source facts without looking up rates."""
	output = []
	for row in rows:
		# The projection's month is the budget month for budgets, application
		# month for operating/purchase approvals. Never borrow another date.
		month = iso_date(row.get("month"), month=True)
		rate_date = f"{month}-01" if month else None
		usable = row.get("amount") is not None and "structure" not in row["issues"]
		issues = set(row["issues"]) - {"exchange_rate"}
		if usable and (row.get("currency") not in CURRENCIES or row["currency"] != target and not rate_date):
			issues.add("exchange_rate")
		output.append({**row, "issues": sorted(issues),
			"reporting_currency": target,
			"exchange_rate_date": rate_date,
			"fx_date_basis": "budget" if row["kind"] == "budget" else "application"})
	return output


def convert(rows, target, *, loader=None):
	converter = Conversion(target, loader)
	output = []
	for row in annotate(rows, target):
		usable = row.get("amount") is not None and "structure" not in row["issues"]
		factor = converter.rate(row.get("currency"), row["exchange_rate_date"]) if usable else None
		amount = converted_amount(row.get("amount"), factor)
		output.append({**row,
			"converted_amount": str(amount) if amount is not None else None,
			"exchange_rate": str(factor) if factor is not None else None,
			"fx_status": "converted" if amount is not None else "incomplete"})
	return output
