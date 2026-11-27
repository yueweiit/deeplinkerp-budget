"""Permission-scoped file preview, idempotent import, and Excel-compatible journal export."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import re
from datetime import date
from decimal import Decimal
from pathlib import Path

import frappe

from budget.services import bank_journal_io as formats

PREVIEW_VERSION = 1
MAX_PREVIEW_ROWS = 100
ROWS_SCOPE = "bank-journal-rows/v1:CNY"


def _store():
	from budget.api import bank_journal

	return bank_journal


def _secret():
	from frappe.utils.password import get_encryption_key

	key = get_encryption_key()
	return key if isinstance(key, bytes) else key.encode()


def _current_month():
	from frappe.utils import today

	return str(today())[:7]


def _read(filename, content_base64, mapping, *, header_row=None, suggest_layout=None):
	if not isinstance(content_base64, str) or len(content_base64) > 4 * ((formats.MAX_BYTES + 2) // 3):
		frappe.throw("请选择 6 MB 以内的银行流水文件")
	if not isinstance(filename, str) or not filename or len(filename) > 255:
		frappe.throw("文件名无效")
	try:
		payload = base64.b64decode(content_base64, validate=True)
	except ValueError, binascii.Error:
		frappe.throw("文件上传不完整，请重新选择文件")
	if isinstance(mapping, str):
		try:
			mapping = json.loads(mapping) if mapping else None
		except ValueError:
			frappe.throw("列映射格式无效")
	if mapping is not None and (not isinstance(mapping, dict) or len(mapping) > len(formats.ALIASES)):
		frappe.throw("列映射格式无效")
	try:
		parsed = formats.parse_statement(
			payload,
			filename,
			mapping,
			header_row=None if header_row == "" else header_row,
			suggest_layout=suggest_layout,
		)
	except formats.StatementError as exc:
		frappe.throw(str(exc))
	columns = parsed["mapping_used"]
	binding = columns if parsed["header_row"] is None else [parsed["header_row"], columns]
	return hashlib.sha256(payload).hexdigest(), binding, parsed


def _sign(doc, input_type, content_hash, mapping, version):
	fields = [
		PREVIEW_VERSION,
		frappe.session.user,
		doc.name,
		doc.company,
		doc.account,
		str(version),
		input_type,
		content_hash,
		mapping,
	]
	payload = json.dumps(fields, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
	return hmac.new(_secret(), payload, hashlib.sha256).hexdigest()


def _ready(parsed):
	return bool(parsed["rows"]) and not parsed["errors"] and not parsed["needs_mapping"]


def _preview(parsed, version, token):
	rows = parsed["rows"]
	return {
		"version": version,
		"row_count": len(rows),
		"error_count": len(parsed["errors"]),
		"errors": parsed["errors"][:MAX_PREVIEW_ROWS],
		"rows": [
			{k: v for k, v in row.items() if k not in ("raw_cells", "source_key", "weak_file_proof")}
			for row in rows[:MAX_PREVIEW_ROWS]
		],
		"income_total": format(sum((Decimal(r["income"] or 0) for r in rows), Decimal(0)), ".2f"),
		"expense_total": format(sum((Decimal(r["expense"] or 0) for r in rows), Decimal(0)), ".2f"),
		"preview_id": token,
	}


@frappe.whitelist(methods=["POST"])
def preview_import(book, filename, content_base64, mapping=None, header_row=None, use_ai=None):
	ai_requested = use_ai in (1, "1")
	doc = _store().authorize_book(book, "create" if ai_requested else "read")
	suggest_layout = None
	if ai_requested:
		from budget.services.bank_journal_ai import infer_layout

		suggest_layout = infer_layout
	file_hash, mapping, parsed = _read(
		filename, content_base64, mapping, header_row=header_row, suggest_layout=suggest_layout
	)
	version = doc.revision
	token = _sign(doc, Path(filename).suffix.lower(), file_hash, mapping, version) if _ready(parsed) else ""
	return {
		**{
			k: parsed[k]
			for k in (
				"opening",
				"opening_date",
				"account_label",
				"headers",
				"suggested_mapping",
				"mapping_used",
				"header_row",
				"mapping_source",
				"ai_notice",
				"needs_mapping",
			)
		},
		**_preview(parsed, version, token),
	}


@frappe.whitelist(methods=["POST"])
def apply_import(
	book, filename, content_base64, mapping=None, preview_id=None, version=None, request_id=None, header_row=None
):
	doc = _store().authorize_book(book)
	file_hash, mapping, parsed = _read(filename, content_base64, mapping, header_row=header_row)
	if (
		not _ready(parsed)
		or version is None
		or not isinstance(preview_id, str)
		or not re.fullmatch(r"[0-9a-f]{64}", preview_id)
		or not hmac.compare_digest(
			_sign(doc, Path(filename).suffix.lower(), file_hash, mapping, version), preview_id
		)
	):
		frappe.throw("流水文件、列映射或账本预览已变化，请重新预览后导入")
	if not isinstance(request_id, str) or not request_id.strip():
		frappe.throw("导入请求缺少操作编号")
	return _store().apply_batch(
		book,
		parsed["rows"],
		"Import",
		version,
		request_id,
		opening=parsed["opening"],
		opening_date=parsed["opening_date"],
		template=parsed["template"],
	)


def _read_rows(book, company, account, currency, rows, extra):
	# Frappe adds cmd to every RPC request. Reject other application arguments explicitly.
	if extra.keys() - {"cmd"}:
		frappe.throw("请求含未定义的参数")
	if not isinstance(book, str) or not book:
		frappe.throw("请选择有效的公司银行账簿")
	doc = _store().authorize_book(book)
	if doc.company != company or doc.account != account:
		frappe.throw("所选账簿与指定公司或银行账户不一致")
	if currency != "CNY":
		frappe.throw("请明确使用此账簿的人民币币种代码 CNY")
	try:
		parsed = formats.parse_rows(rows)
	except formats.StatementError as exc:
		frappe.throw(str(exc))
	for row in parsed["rows"]:
		if row["date"] < str(doc.opening_date):
			parsed["errors"].append(
				{"row": row["source_row"], "field": "date", "message": "流水日期早于账簿启用日"}
			)
	return doc, parsed


@frappe.whitelist(methods=["POST"])
def preview_rows(book=None, company=None, account=None, currency=None, rows=None, **extra):
	"""Preview extracted bank transactions against the explicitly selected RMB book."""
	doc, parsed = _read_rows(book, company, account, currency, rows, extra)
	version = int(doc.revision or 0)
	token = _sign(doc, ROWS_SCOPE, parsed["input_hash"], None, version) if not parsed["errors"] else ""
	return {
		"book": doc.name,
		"company": doc.company,
		"account": doc.account,
		"currency": currency,
		"opening": parsed["opening"],
		"opening_date": parsed["opening_date"],
		**_preview(parsed, version, token),
	}


@frappe.whitelist(methods=["POST"])
def apply_rows(
	book=None,
	company=None,
	account=None,
	currency=None,
	rows=None,
	preview_id=None,
	version=None,
	request_id=None,
	**extra,
):
	"""Confirm an unchanged extraction with the same native writer used by file imports."""
	doc, parsed = _read_rows(book, company, account, currency, rows, extra)
	if (
		parsed["errors"]
		or type(version) is not int
		or version < 0
		or not isinstance(preview_id, str)
		or not re.fullmatch(r"[0-9a-f]{64}", preview_id)
		or not hmac.compare_digest(_sign(doc, ROWS_SCOPE, parsed["input_hash"], None, version), preview_id)
	):
		frappe.throw("流水、账簿或预览参数已变化，请重新预览后提交")
	return _store().apply_batch(
		doc.name,
		parsed["rows"],
		"Import",
		version,
		request_id,
		opening=parsed["opening"],
		opening_date=parsed["opening_date"],
		request_facts=[ROWS_SCOPE, parsed["input_hash"]],
	)


@frappe.whitelist()
def export_journal(book, month=None):
	period = None if month == "all" else (month or _current_month())
	if period:
		if not isinstance(period, str) or not re.fullmatch(r"\d{4}-\d{2}", period):
			frappe.throw("导出月份无效")
		try:
			date.fromisoformat(period + "-01")
		except ValueError:
			frappe.throw("导出月份无效")
	data = _store().export_rows(book, period)
	content = formats.export_journal(
		data["rows"],
		opening=data["opening"],
		account_label=data["account"],
		template=data.get("template"),
		month=period,
	)
	label = re.sub(r'[/\\:*?"<>|\x00-\x1f]', "_", str(data["account"]))[:128]
	return {
		"filename": f"{label}银行日记账-{period or '全账簿'}.csv",
		"content_base64": base64.b64encode(content).decode("ascii"),
	}
