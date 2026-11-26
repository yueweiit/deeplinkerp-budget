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


def _read(filename, content_base64, mapping):
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
		parsed = formats.parse_statement(payload, filename, mapping)
	except formats.StatementError as exc:
		frappe.throw(str(exc))
	return hashlib.sha256(payload).hexdigest(), parsed["mapping_used"], parsed


def _sign(doc, filename, file_hash, mapping, version):
	fields = [
		PREVIEW_VERSION,
		frappe.session.user,
		doc.name,
		doc.company,
		doc.account,
		str(version),
		Path(filename).suffix.lower(),
		file_hash,
		mapping,
	]
	payload = json.dumps(fields, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
	return hmac.new(_secret(), payload, hashlib.sha256).hexdigest()


def _ready(parsed):
	return bool(parsed["rows"]) and not parsed["errors"] and not parsed["needs_mapping"]


@frappe.whitelist(methods=["POST"])
def preview_import(book, filename, content_base64, mapping=None):
	doc = _store().authorize_book(book)
	file_hash, mapping, parsed = _read(filename, content_base64, mapping)
	version = doc.revision
	result = {
		k: parsed[k]
		for k in ("opening", "opening_date", "account_label", "headers", "suggested_mapping", "needs_mapping")
	}
	rows = parsed["rows"]
	result.update(
		version=version,
		row_count=len(rows),
		error_count=len(parsed["errors"]),
		errors=parsed["errors"][:MAX_PREVIEW_ROWS],
		rows=[
			{k: v for k, v in row.items() if k not in ("raw_cells", "source_key", "weak_file_proof")}
			for row in rows[:MAX_PREVIEW_ROWS]
		],
		income_total=format(sum((Decimal(r["income"] or 0) for r in rows), Decimal(0)), ".2f"),
		expense_total=format(sum((Decimal(r["expense"] or 0) for r in rows), Decimal(0)), ".2f"),
		preview_id=_sign(doc, filename, file_hash, mapping, version) if _ready(parsed) else "",
	)
	return result


@frappe.whitelist(methods=["POST"])
def apply_import(
	book, filename, content_base64, mapping=None, preview_id=None, version=None, request_id=None
):
	doc = _store().authorize_book(book)
	file_hash, mapping, parsed = _read(filename, content_base64, mapping)
	if (
		not _ready(parsed)
		or version is None
		or not isinstance(preview_id, str)
		or not re.fullmatch(r"[0-9a-f]{64}", preview_id)
		or not hmac.compare_digest(_sign(doc, filename, file_hash, mapping, version), preview_id)
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
