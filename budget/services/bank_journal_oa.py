"""Read-only, month-bounded DingTalk lookup for the selected company's bank book."""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from datetime import datetime, time
from decimal import Decimal
from zoneinfo import ZoneInfo

from .bank_journal_contract import as_date, money, month_bounds

CN = ZoneInfo("Asia/Shanghai")
ARCHIVE_LIMIT = 4000
OFFICIAL = "https://aflow.dingtalk.com/dingtalk/mobile/homepage.htm?showmenu=false&dd_progress=false#/approval?procInstId="


def unique_links(requested, pairs):
	matches = defaultdict(set)
	for number, instance in pairs:
		if number in requested:
			matches[number].add(instance)
	return {
		number: OFFICIAL + next(iter(ids))
		for number, ids in matches.items()
		if len(ids) == 1 and re.fullmatch(r"[A-Za-z0-9_-]{1,200}", next(iter(ids)) or "")
	}


def _scope(company):
	import frappe

	from budget.integrations.cn_approval_source import configuration

	from .cn_approval_workbench import access_context

	context = access_context()
	if (
		not frappe.has_permission("CN Cash Journal Entry", ptype="read")
		or company not in context["companies"]
	):
		frappe.throw("无权查看该公司的日记账。", frappe.PermissionError)
	_, configured = configuration()
	corps = {corp for (corp, _), target in context["aliases"].items() if company == target}
	corps.update(corp for (corp, _), target in context["organizations"].items() if company == target)
	return context, {(t["corp_id"], t["process_code"]): t for t in configured if t["corp_id"] in corps}


def _fetch(sql, params):
	from budget.integrations.oa_approval_source import OAApprovalSourceConfig, postgres_connection_kwargs
	from budget.integrations.pg import fetch

	return fetch(
		postgres_connection_kwargs(OAApprovalSourceConfig.from_site_config()),
		sql,
		params,
		label="bank journal OA",
	)


def resolve_refs(company, refs):
	"""Resolve exact numbers supplied on a permitted bank record; never expose OA fields here.

	A paying company can fund another department. Its bank statement supplies the
	business number, and DingTalk controls access to the linked original.
	"""
	if not refs:
		return {}
	_, templates = _scope(company)
	requested = sorted(set(refs))
	matches = []
	for corp in sorted({k[0] for k in templates}):
		codes = [code for org, code in templates if org == corp]
		for offset in range(0, len(requested), 500):
			rows = _fetch(
				"""
                SELECT raw_payload->>'businessId' AS number, process_instance_id AS instance,
                       raw_payload->>'processInstanceId' AS archived_instance
                FROM ding_approval_instance
                WHERE deleted_at IS NULL AND corp_id=%s AND process_code=ANY(%s)
                  AND raw_payload->>'businessId'=ANY(%s)
            """,
				(corp, codes, requested[offset : offset + 500]),
			)
			for row in rows:
				instance = (
					row["instance"] if row["archived_instance"] in ("", None, row["instance"]) else None
				)
				matches.append((row["number"], instance))
	return unique_links(set(requested), matches)


def candidate(item, completed_on):
	if (
		item.get("kind") not in ("operating", "purchase")
		or item.get("status") != "approved"
		or item.get("currency") != "CNY"
		or not item.get("number")
		or {"structure", "currency", "amount"} & set(item.get("issues") or [])
	):
		return None
	try:
		amount = money(item.get("amount"), positive=True)
	except ValueError:
		return None
	path = item.get("classification") or []
	facts = {f["label"]: f["value"] for f in item.get("facts", [])}
	return {
		"source_key": item["key"],
		"refs": [item["number"]],
		"number": item["number"],
		"date": None,
		"income": None,
		"expense": None,
		"application_amount": amount,
		"application_date": item.get("application_date"),
		"approved_date": completed_on,
		"category": "费用" if item["kind"] == "operating" else "",
		"level2": path[0] if path else "",
		"level3": " / ".join(path[1:]),
		"project": facts.get("Project", ""),
		"dept": item.get("department") or "",
		"summary": item.get("description") or item["number"],
		"invoice": "",
		"selectable": True,
		"reason": "请核实本账户的实际付款日期和金额。",
	}


def prefill_payment(row, payments, month):
	if len(payments) != 1:
		return
	payment = payments[0]
	paid_date = str(payment["payment_date"])
	if not paid_date.startswith(month + "-"):
		return
	amount = money(payment["bank_amount"], positive=True)
	if Decimal(amount) > Decimal(row["application_amount"]):
		return
	row.update(
		date=paid_date,
		expense=amount,
		bank_ref=payment.get("bank_reference") or "",
		reason="已按本公司、本银行账户的付款登记填入实付日期和金额。",
	)


def _attach_registered_payments(doc, sources, month):
	import frappe

	source_type, payment_type = "Operating Expense Source", "Operating Expense Payment"
	if not sources or not all(
		frappe.db.exists("DocType", t) and frappe.has_permission(t, ptype="read")
		for t in (source_type, payment_type)
	):
		return
	accounts = frappe.get_list(
		"Account",
		filters={"company": doc.company, "account_type": "Bank", "is_group": 0},
		or_filters={"name": doc.account, "account_name": doc.account},
		pluck="name",
		limit_page_length=2,
	)
	if len(accounts) != 1:
		return
	matches = frappe.get_list(
		source_type,
		filters={"company": doc.company, "source_system": "dingtalk-oa", "source_id": ["in", list(sources)]},
		fields=["name", "source_id"],
		limit_page_length=0,
	)
	if not matches:
		return
	by_source = defaultdict(list)
	for payment in frappe.get_list(
		payment_type,
		filters={
			"source": ["in", [m.name for m in matches]],
			"company": doc.company,
			"status": "Registered",
			"bank_currency": "CNY",
			"currency": "CNY",
			"bank_account": accounts[0],
		},
		fields=["source", "payment_date", "bank_amount", "bank_reference"],
		limit_page_length=0,
	):
		by_source[payment.source].append(payment)
	totals = Counter(m.source_id for m in matches)
	for match in matches:
		if totals[match.source_id] == 1:
			prefill_payment(sources[match.source_id], by_source[match.name], month)


def month_candidates(doc, month):
	import frappe

	from .cn_approval_contract import project
	from .cn_approval_management import company_for
	from .cn_approval_workbench import require_reader

	require_reader()
	context, templates = _scope(doc.company)
	if not templates:
		frappe.throw("此公司尚未配置钉钉企业和审批模板。")
	_, first, following = month_bounds(month, frappe.utils.today())
	start, end = (datetime.combine(t, time.min, CN) for t in (first, following))
	from deeplinkerp_branding.services.operating_oa_source import application_id

	result = []
	sources = {}
	for corp in sorted({k[0] for k in templates}):
		codes = [
			code
			for (org, code), t in templates.items()
			if org == corp and t["approval_type"] in ("operating", "purchase")
		]
		if not codes:
			continue
		rows = _fetch(
			"""
            SELECT corp_id,process_code,process_instance_id,status,result,originator_dept_name,
                   originator_dept_id,originator_user_name,form_component_values,finish_time,
                   jsonb_build_object('businessId',raw_payload->>'businessId','finishTime',raw_payload->>'finishTime',
                                      'processInstanceId',raw_payload->>'processInstanceId') AS raw_payload
              FROM ding_approval_instance
             WHERE corp_id=%s AND process_code=ANY(%s) AND deleted_at IS NULL
               AND finish_time >= %s AND finish_time < %s AND upper(status)='COMPLETED' AND lower(result)='agree'
             ORDER BY finish_time,process_instance_id LIMIT %s
        """,
			(corp, codes, start, end, ARCHIVE_LIMIT + 1),
		)
		if len(rows) > ARCHIVE_LIMIT:
			frappe.throw("本月钉钉审批超过同步上限，请联系管理员。")
		for raw in rows:
			source = raw["raw_payload"]
			if not source.get("businessId") or source.get("processInstanceId") not in (
				None,
				"",
				raw["process_instance_id"],
			):
				continue
			projected = project(raw, templates[corp, raw["process_code"]])
			if company_for(projected, context) != doc.company:
				continue
			row = candidate(projected, raw["finish_time"].astimezone(CN).date().isoformat())
			if row is not None:
				result.append(row)
				sources[application_id(raw)] = row
	_attach_registered_payments(doc, sources, month)
	count = Counter(r["number"] for r in result)
	for r in result:
		if count[r["number"]] > 1:
			r.update(selectable=False, reason="原单编号对应多个审批实例。")
	return sorted(result, key=lambda row: (row["approved_date"], row["source_key"]), reverse=True)


def confirmed_row(candidate_row, paid_date, paid_amount, month):
	date = as_date(paid_date).isoformat()
	amount = money(paid_amount, positive=True)
	if not date.startswith(month + "-"):
		raise ValueError("实付日期须在所选账簿月份内。")
	if Decimal(amount) > Decimal(candidate_row["application_amount"]):
		raise ValueError("本次实付金额超过申请金额，请先核对原单。")
	if candidate_row.get("date") and (date, amount) != (candidate_row["date"], candidate_row["expense"]):
		raise ValueError("实付日期或金额与银行账户的付款登记不符。")
	return {
		**{
			key: candidate_row[key]
			for key in (
				"source_key",
				"refs",
				"category",
				"level2",
				"level3",
				"project",
				"summary",
				"invoice",
				"dept",
			)
		},
		"date": date,
		"income": None,
		"expense": amount,
		"bank_ref": candidate_row.get("bank_ref", ""),
	}
