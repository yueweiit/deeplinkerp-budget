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


def _file_edits(edits):
	if edits in (None, ""):
		return {}
	if isinstance(edits, str):
		if len(edits) > formats.MAX_BYTES:
			frappe.throw("流水修改内容过多，请分批导入")
		try:
			edits = json.loads(edits)
		except ValueError:
			frappe.throw("流水修改格式无效")
	if not isinstance(edits, dict) or len(edits) > formats.MAX_MOVEMENTS:
		frappe.throw("流水修改格式无效")
	rows = {}
	size = 0
	allowed_fields = set(formats.REVIEW_FIELDS)
	for index, values in edits.items():
		if not re.fullmatch(r"[1-9][0-9]{0,4}", str(index), re.ASCII) or int(index) > formats.MAX_ROWS:
			frappe.throw("修改的流水行号无效")
		if not isinstance(values, dict) or not values or values.keys() - allowed_fields:
			frappe.throw("流水修改包含无效字段")
		if ("income" in values) != ("expense" in values):
			frappe.throw("修改金额时请提供该行的收入和支出")
		for key, value in values.items():
			if not isinstance(value, str) or len(value) > (12000 if key == "summary" else 80) or "\x00" in value:
				frappe.throw("流水修改值无效")
			size += len(value)
		rows[int(index)] = values
	if size > formats.MAX_BYTES:
		frappe.throw("流水修改内容过多，请分批导入")
	return rows


def _read(filename, content_base64, mapping, *, header_row=None, suggest_layout=None, edits=None, review_offset=None):
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
	edits = _file_edits(edits)
	try:
		parsed = formats.parse_statement(
			payload,
			filename,
			mapping,
			header_row=None if header_row == "" else header_row,
			suggest_layout=suggest_layout,
			edits=edits,
			review_offset=review_offset,
		)
	except formats.StatementError as exc:
		frappe.throw(str(exc))
	columns = parsed["mapping_used"]
	binding = columns if parsed["header_row"] is None else [parsed["header_row"], columns]
	if edits:
		binding = [binding, edits]
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


def _check_opening_date(doc, parsed):
	for row in parsed["rows"]:
		if doc.opening_date and row["date"] < str(doc.opening_date):
			parsed["errors"].append(
				{"row": row["source_row"], "field": "date", "message": "流水日期早于账簿启用日"}
			)


def _file_target(book, company, permission, settings, mode):
	if mode not in ("append", "replace_range"):
		frappe.throw("请选择有效的导入方式。")
	if mode == "replace_range":
		if not book:
			frappe.throw("按日期覆盖只能用于已有账簿。")
		_store().authorize_replacement()
	if not book:
		_store().authorize_book_creation(company)
		return None
	if company is not None or any(value is not None for value in settings):
		frappe.throw("请选择已有账簿，或指定公司导入新账簿。")
	return _store().authorize_book(book, permission)


def _new_file_book(company, parsed, account, opening, opening_date):
	statement_opening = parsed["opening"]
	if statement_opening is None and not parsed["errors"]:
		movement = Decimal(0)
		for row in parsed["rows"]:
			movement += Decimal(row["income"] or 0) - Decimal(row["expense"] or 0)
			if row["balance"] is not None:
				statement_opening = format(Decimal(row["balance"]) - movement, ".2f")
				break
	doc, issues = _store().book_settings(
		company,
		parsed["account_label"] if account is None else account,
		statement_opening if opening is None else opening,
		parsed["opening_date"] if opening_date is None else opening_date,
	)
	if doc.opening is not None and statement_opening is not None and Decimal(doc.opening) != Decimal(statement_opening):
		issues.append({"field": "opening", "message": "期初余额与文件流水不符，请核对流水或期初余额。"})
	return doc, issues


def _file_scope(filename, mapping, new_book, mode):
	if new_book is None:
		prefix = "replace-range/v1:" if mode == "replace_range" else ""
		return prefix + Path(filename).suffix.lower(), mapping
	return "new-book:" + Path(filename).suffix.lower(), [mapping, new_book.opening, new_book.opening_date]


def _preview(parsed, version, token):
	rows = parsed["rows"]
	return {
		"version": version,
		"row_count": len(rows),
		"error_count": len(parsed["errors"]),
		"errors": parsed["errors"][:MAX_PREVIEW_ROWS],
		"rows": [
			{k: v for k, v in row.items() if k not in ("raw_cells", "source_key", "weak_file_proof", "file_review")}
			for row in rows[:MAX_PREVIEW_ROWS]
		],
		"income_total": format(sum((Decimal(r["income"] or 0) for r in rows), Decimal(0)), ".2f"),
		"expense_total": format(sum((Decimal(r["expense"] or 0) for r in rows), Decimal(0)), ".2f"),
		"preview_id": token,
	}


@frappe.whitelist(methods=["POST"])
def preview_import(
	book=None, filename=None, content_base64=None, mapping=None, header_row=None, use_ai=None, edits=None,
	review_offset=None, company=None, account=None, opening=None, opening_date=None, mode="append",
):
	ai_requested = use_ai in (1, "1")
	if review_offset is not None:
		if (
			not re.fullmatch(r"[0-9]{1,5}", str(review_offset), re.ASCII)
			or int(review_offset) >= formats.MAX_MOVEMENTS
		):
			frappe.throw("预览行范围无效")
		review_offset = int(review_offset)
	permission = "create" if ai_requested or edits or review_offset is not None else "read"
	doc = _file_target(book, company, permission, (account, opening, opening_date), mode)
	suggest_layout = None
	if ai_requested:
		from budget.services.bank_journal_ai import infer_layout

		suggest_layout = infer_layout
	file_hash, mapping, parsed = _read(
		filename,
		content_base64,
		mapping,
		header_row=header_row,
		suggest_layout=suggest_layout,
		edits=edits,
		review_offset=review_offset,
	)
	book_issues, target = [], {}
	if doc is None:
		doc, book_issues = _new_file_book(company, parsed, account, opening, opening_date)
		if doc.account and _store().book_exists(doc.name):
			book_issues.append({"field": "account", "message": "该账户已有账簿，请选择该账户后导入银行流水。"})
		target = {"book_settings": {k: doc[k] for k in ("account", "opening", "opening_date")}, "book_issues": book_issues}
	_check_opening_date(doc, parsed)
	if mode == "replace_range" and _ready(parsed):
		impact, _, issues = _store().replacement_impact(doc, parsed["rows"], parsed["opening"])
		target["impact"] = impact
		parsed["errors"].extend(issues)
	review = {}
	if review_offset is not None:
		visible = {row["source_row"] for row in parsed["review_rows"]}
		review = {
			"review_rows": parsed["review_rows"],
			"review_total": parsed["review_total"],
			"review_page_size": formats.REVIEW_PAGE_SIZE,
			"review_errors": [issue for issue in parsed["errors"] if issue.get("row") in visible],
		}
	version = doc.revision
	scope, binding = _file_scope(filename, mapping, None if book else doc, mode)
	token = _sign(doc, scope, file_hash, binding, version) if _ready(parsed) and not book_issues else ""
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
		**review,
		**target,
	}


@frappe.whitelist(methods=["POST"])
def apply_import(
	book=None, filename=None, content_base64=None, mapping=None, preview_id=None, version=None,
	request_id=None, header_row=None, edits=None, company=None, account=None, opening=None, opening_date=None, mode="append",
):
	doc = _file_target(book, company, "create", (account, opening, opening_date), mode)
	file_hash, mapping, parsed = _read(filename, content_base64, mapping, header_row=header_row, edits=edits)
	issues = []
	if doc is None:
		doc, issues = _new_file_book(company, parsed, account, opening, opening_date)
	_check_opening_date(doc, parsed)
	scope, binding = _file_scope(filename, mapping, None if book else doc, mode)
	if (
		not _ready(parsed)
		or issues
		or version is None
		or not isinstance(preview_id, str)
		or not re.fullmatch(r"[0-9a-f]{64}", preview_id)
		or not hmac.compare_digest(
			_sign(doc, scope, file_hash, binding, version), preview_id
		)
	):
		frappe.throw("流水文件、列对应、修改值或账簿预览已变化，请重新校验后导入")
	if not isinstance(request_id, str) or not request_id.strip():
		frappe.throw("导入请求缺少操作编号")
	options = {} if book else {"new_book": doc, "request_facts": [scope, preview_id]}
	if mode == "replace_range":
		options = {"replace_range": True, "request_facts": [scope, preview_id]}
	result = _store().apply_batch(
		doc.name,
		parsed["rows"],
		"Import",
		version,
		request_id,
		opening=parsed["opening"],
		opening_date=parsed["opening_date"],
		template=parsed["template"],
		**options,
	)
	return result if book else {**result, "book": doc.name}


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
	_check_opening_date(doc, parsed)
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
