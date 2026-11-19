"""Project CN budget approvals into quotas without a separate budget database.

Reuse ledger parsing, organization assignments and department/month supersession.
TERMINATED + agree remains eligible, matching the historical quota contract.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal, ROUND_HALF_UP

from .oa_budget_parser import BUDGET_PROCESS_CODE, dedupe_latest, parse_budget_instance

CENT = Decimal("0.01")


def money(value):
	amount = Decimal(str(value or 0))
	if not amount.is_finite():
		raise ValueError("Invalid budget quota amount")
	return amount.quantize(CENT, rounding=ROUND_HALF_UP)


def organization(entry, path):
	from .oa_budget_sync import COMPANY_LEVEL_INDEX, _presentation

	ids, names = path.get("ids") or [], path.get("names") or []
	if len(ids) != len(names):
		raise ValueError("Invalid budget organization path")
	company, department = _presentation(entry.get("dept_name") or "", names)
	company_id = ids[min(COMPANY_LEVEL_INDEX, len(ids) - 1)] if ids else ""
	if path.get("reviewed_organization_level"):
		company, department = path["reviewed_company"], path["reviewed_department"]
		# A reviewed company name does not prove an OA company ID.
		company_id = entry.get("dept_id") if path["reviewed_organization_level"] == "company" else ""
	return dict(company_name=company, company_dept_id=company_id,
		department_name=department, department_id=entry.get("dept_id") or "",
		group_dept_id=ids[1] if len(ids) > 1 else "", group_name=names[1] if len(names) > 1 else "",
		department_source="operating_oa", organization_is_current=bool(ids or path.get("reviewed_organization_level")),
		department_path_ids=ids, department_path_names=names)


def project(instances, paths, source):
	"""Return quota/subject rows; reject incomplete identities before any writes."""
	parsed, seen = [], set()
	for row in instances:
		if row.get("corp_id") != source["corp_id"] or row.get("process_code") != BUDGET_PROCESS_CODE:
			raise ValueError("Budget approval source identity mismatch")
		if row.get("deleted_at") or row.get("status") not in {"COMPLETED", "TERMINATED"} or row.get("result") != "agree":
			continue
		item = parse_budget_instance(row)
		entry = item["entry"]
		region = "".join(str(entry.get("execution_region") or "").split()).casefold()
		if region not in {"中国", "中国china", "china", "cn"}:
			continue
		if not entry["form_no"] or not entry["process_instance_id"] or entry["form_no"] in seen:
			raise ValueError("Duplicate or missing budget approval identity")
		seen.add(entry["form_no"])
		# Missing/invalid dates cannot silently become an effective undated quota.
		date.fromisoformat(entry["budget_month"] or "")
		created = datetime.fromisoformat(str(row.get("create_time") or "").replace("Z", "+00:00"))
		entry["source_created_at"] = created.replace(tzinfo=timezone.utc).isoformat() if created.tzinfo is None else created.astimezone(timezone.utc).isoformat()
		parsed.append((row, item))
	# Retain the same tie order as the ledger parser.
	latest = {entry["form_no"] for entry in dedupe_latest([item["entry"] for _, item in parsed])}
	quotas, subjects = [], []
	for row, item in parsed:
		entry, details = item["entry"], item["details"]
		form = entry["form_no"]
		groups = {}
		for detail in details:
			key = (detail["detail_group"], detail.get("detail_code") or "",
				detail.get("operation_expense") or detail.get("detail_item") or detail["detail_group"])
			groups[key] = groups.get(key, Decimal(0)) + money(detail.get("amount"))
		for index, ((kind, code, name), amount) in enumerate(groups.items(), 1):
			subjects.append(dict(quota_id=form, line_no=index, subject_type=kind,
				subject_code=code, subject_name=name, amount=amount, source_table="oa_approval"))
		total, detail_total = money(entry["amount"]), sum(groups.values(), Decimal(0))
		quotas.append(dict(id=form, source_form_no=form, corp_id=source["corp_id"],
			process_instance_id=entry["process_instance_id"], source_process_code=BUDGET_PROCESS_CODE,
			budget_type=entry["budget_kind"], budget_month=entry["budget_month"],
			budget_year=int(entry["budget_month"][:4]), total_amount=total, detail_amount=detail_total,
			detail_amount_mismatch=abs(total - detail_total) > CENT,
			status="effective" if form in latest else "deactivated",
			approval_status=row["status"], approval_result=row["result"],
			owner_user_id=row.get("originator_user_id"), owner_name=entry["creator_name"],
			source_created_at=entry["source_created_at"], source_updated_at=row.get("updated_at"),
			**organization(entry, paths.get(entry.get("dept_id")) or {})))
	return quotas, subjects
