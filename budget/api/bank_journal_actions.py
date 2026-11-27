"""Confirm a selected bank book's month of DingTalk expenditure candidates."""

from __future__ import annotations

import hmac
import json

import frappe

from budget.api import bank_journal as store
from budget.services import bank_journal_oa as oa
from budget.services.bank_journal_contract import digest, month_bounds


def _month(month):
	return month_bounds(month, frappe.utils.today())[0]


def _token(doc, month, version, rows):
	from frappe.utils.password import get_encryption_key

	content = digest([doc.name, doc.company, doc.account, month, str(version), frappe.session.user, rows])
	key = get_encryption_key()
	return hmac.digest(key if isinstance(key, bytes) else key.encode(), content.encode(), "sha256").hex()


def _preview(doc, month, version):
	raw = oa.month_candidates(doc, month)
	token = _token(doc, month, version, raw)
	stored = store.book_ref_rows(doc.name, [r["number"] for r in raw])
	used = {ref for r in stored for ref in r["refs"]}
	prior = store.known_source_keys(doc.name, "oa", [r["source_key"] for r in raw])
	for row in raw:
		if row["number"] in used or row["source_key"] in prior:
			row.update(
				selectable=False,
				already_recorded=True,
				reason="账簿已有该单号或同步记录。分次付款可在新增行中填写同一单号。",
			)
	return raw, token


@frappe.whitelist()
def preview_sync(book, month=None):
	doc = store.authorize_book(book, "create")
	month = _month(month)
	version = int(doc.revision or 0)
	rows, token = _preview(doc, month, version)
	return {
		"rows": rows,
		"total": len(rows),
		"errors": [],
		"preview_id": token,
		"version": version,
		"company": doc.company,
		"account": doc.account,
		"month": month,
	}


@frappe.whitelist(methods=["POST"])
def apply_sync(book, month, preview_id, version, request_id, selection):
	doc = store.authorize_book(book, "create")
	month = _month(month)
	selections = json.loads(selection) if isinstance(selection, str) else selection
	if not isinstance(selections, list) or not 1 <= len(selections) <= oa.ARCHIVE_LIMIT:
		frappe.throw("请先选择本账户实际支付的审批。")
	keys = set()
	for chosen in selections:
		if (
			not isinstance(chosen, dict)
			or set(chosen) != {"source_key", "date", "expense"}
			or not isinstance(chosen["source_key"], str)
			or chosen["source_key"] in keys
		):
			frappe.throw("选择的支出重复或无效。")
		keys.add(chosen["source_key"])
	facts = [month, preview_id, str(version), selections]
	previous = store.batch_replay(book, "oa", version, request_id, facts)
	if previous is not None:
		return previous
	rows, expected = _preview(doc, month, version)
	if not isinstance(preview_id, str) or not hmac.compare_digest(preview_id, expected):
		frappe.throw("钉钉原单或账簿已变化，请重新同步。")
	eligible = {r["source_key"]: r for r in rows if r["selectable"]}
	if not keys.issubset(eligible):
		frappe.throw("所选审批已处理或不属于本次预览，请刷新。")
	resolved = [
		oa.confirmed_row(eligible[s["source_key"]], s["date"], s["expense"], month) for s in selections
	]
	return store.apply_batch(book, resolved, "oa", version, request_id, request_facts=facts)
