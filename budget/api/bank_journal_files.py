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

PREVIEW_VERSION = 2
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


def _exclusions(value):
	if value in (None, ""):
		return []
	if isinstance(value, str) and len(value) <= formats.MAX_MOVEMENTS * 8:
		try:
			value = json.loads(value)
		except ValueError:
			frappe.throw("排除流水行号的格式无效")
	if (
		not isinstance(value, list) or len(value) > formats.MAX_MOVEMENTS
		or any(type(i) is not int or not 1 <= i <= formats.MAX_ROWS for i in value)
		or len(set(value)) != len(value)
	):
		frappe.throw("请提供有效且唯一的原表行号")
	return sorted(value)


def _read(
	filename, content_base64, mapping, *, header_row=None, suggest_layout=None, edits=None,
	review_offset=None, currency=None, excluded_rows=None, suggest_currency=None, auto_exclude=False,
):
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
	excluded = _exclusions(excluded_rows)
	try:
		parsed = formats.parse_statement(
			payload,
			filename,
			mapping,
			header_row=None if header_row == "" else header_row,
			suggest_layout=suggest_layout,
			edits=edits,
			review_offset=review_offset,
			currency=currency,
			excluded_rows=excluded,
			suggest_currency=suggest_currency,
			auto_exclude=auto_exclude,
		)
	except formats.StatementError as exc:
		frappe.throw(str(exc))
	columns = parsed["mapping_used"]
	binding = columns if parsed["header_row"] is None else [parsed["header_row"], columns]
	if edits:
		binding = [binding, edits]
	if parsed["excluded_source_rows"]:
		binding = [binding, {"excluded": parsed["excluded_source_rows"]}]
	return hashlib.sha256(payload).hexdigest(), binding, parsed


def _sign(doc, input_type, content_hash, mapping, version):
	fields = [
		PREVIEW_VERSION,
		frappe.session.user,
		doc.name,
		doc.company,
		doc.account,
		doc.currency,
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


def _filename_account(filename, company, abbr):
	"""Suggest a display name only. The caller must confirm it before creating a book."""
	if not isinstance(filename, str) or len(filename) > 255:
		return ""
	name = Path(filename.replace("\\", "/")).stem.strip()
	# A download may contain both a dated suffix and a ledger label in either order.
	name = re.sub(
		r"(?:[\s_.-]*(?:(?:银行)?日记[账帐]|银行流水|(?:银行)?对账单|bank[ _-]*statement|"
		r"(?:19|20)\d{2}[-.年]\d{1,2}(?:[-.月]\d{1,2}日?)?))+$", "", name, flags=re.I,
	).strip(" ._-")
	for prefix in (company, abbr):
		if prefix and name.startswith(prefix):
			rest = name[len(prefix):]
			if prefix == company or re.search(r"[\u3400-\u9fff]$", prefix) or rest[:1] in {" ", "-", "_"}:
				name = rest.strip(" ._-")
				break
	return name[:140] or "银行账户"


def _bank_account_currencies(company, account, own_number):
	"""Return None for unknown accounts, an empty set for contradictory account details."""
	rows = frappe.db.sql(
		"""SELECT DISTINCT ledger.account_currency, bank.bank_account_no
		   FROM `tabBank Account` bank
		   JOIN `tabAccount` ledger ON ledger.name = bank.account AND ledger.company = bank.company
		   WHERE bank.company = %(company)s AND bank.is_company_account = 1 AND bank.disabled = 0
		     AND ledger.disabled = 0 AND ledger.is_group = 0
		     AND (bank.account_name = %(account)s OR bank.name = %(account)s OR bank.bank_account_no = %(account)s
		          OR bank.bank_account_no = NULLIF(%(own_number)s, ''))""",
		{"company": company, "account": account, "own_number": own_number},
	)
	if not rows:
		return None
	if own_number:
		exact = {code for code, number in rows if number == own_number and code}
		if exact:
			return exact
		rows = [(code, number) for code, number in rows if not number]
	return {code for code, _ in rows if code}


def _file_book_defaults(company, filename, edited_account):
	from budget.api.bank_journal import supported_currencies

	company_info = frappe.db.get_value("Company", company, ["abbr", "default_currency"], as_dict=True)
	if not company_info:
		frappe.throw("所选公司已变更，请重新选择公司。")
	file_account = _filename_account(filename, company, company_info.abbr)
	scopes = frappe.conf.get("bank_journal_currency_defaults") or {}
	preferred = scopes.get(company, {}) if isinstance(scopes, dict) else {}

	def suggest_currency(source_account, own_number, tokens):
		enabled = set(supported_currencies())
		for token in tokens:
			enabled.intersection_update(formats.AMBIGUOUS_SYMBOLS.get(token, {token}))
		account = edited_account if isinstance(edited_account, str) else source_account or file_account
		account = account.strip()
		confirmed = preferred.get(account) if isinstance(preferred, dict) else None
		if account != source_account:
			own_number = ""
		native = _bank_account_currencies(company, account, own_number) if account else None
		if native is not None:
			enabled.intersection_update(native)
		if isinstance(confirmed, str) and confirmed:
			return confirmed if confirmed in enabled else None
		if native is None and company_info.default_currency in enabled:
			return company_info.default_currency
		return next(iter(enabled)) if len(enabled) == 1 else None

	return file_account, suggest_currency


def _new_file_book(company, parsed, account, opening, opening_date, currency, *, suggest_opening=False):
	statement_opening = parsed["opening"]
	if statement_opening is None and not parsed["errors"]:
		movement = Decimal(0)
		for row in parsed["rows"]:
			movement += Decimal(row["income"] or 0) - Decimal(row["expense"] or 0)
			if row["balance"] is not None:
				statement_opening = format(Decimal(row["balance"]) - movement, ".2f")
				break
	book_opening = statement_opening if opening is None else opening
	if book_opening is None and suggest_opening and parsed["rows"] and not parsed["errors"]:
		book_opening = "0.00"
	doc, issues = _store().book_settings(
		company,
		parsed["account_label"] if account is None else account,
		book_opening,
		parsed["opening_date"] if opening_date is None else opening_date,
		currency=currency,
	)
	adjustment = None
	if doc.opening is not None and statement_opening is not None:
		difference = Decimal(doc.opening) - Decimal(statement_opening)
		if difference:
			closing = Decimal(doc.opening) + sum(
				Decimal(r["income"] or 0) - Decimal(r["expense"] or 0) for r in parsed["rows"]
			)
			adjustment = dict(
				file_opening=statement_opening, book_opening=doc.opening,
				difference=format(difference, ".2f"), closing=format(closing, ".2f"),
			)
	return doc, issues, adjustment


def _file_currency(doc, requested):
	if requested is not None:
		if not isinstance(requested, str):
			frappe.throw("请选择有效的账簿币种。")
		requested = requested.strip().upper()
	if doc is None:
		return requested or None
	code = _store().book_currency(doc)
	if requested not in (None, "", code):
		frappe.throw(f"所选币种与此账簿的 {code} 不一致。")
	return code


def _ledger_marker(book):
	return hmac.new(_secret(), ("bank-journal:ledger-balance:v1:" + book).encode(), hashlib.sha256).hexdigest()


def _marked_book(doc, parsed):
	marker = parsed.get("ledger_book")
	if not marker or doc is None:
		return None
	if not hmac.compare_digest(marker, _ledger_marker(doc.name)):
		frappe.throw("这份账面余额导出属于其他账簿，请选择对应的账簿。")
	return doc.name


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
			{k: v for k, v in row.items() if k not in ("raw_cells", "source_key", "weak_file_proof", "file_review", "_legacy_source_keys")}
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
	currency=None, excluded_rows=None, suggest_book_defaults=None, auto_exclude=None,
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
	currency = _file_currency(doc, currency)
	file_defaults = doc is None and suggest_book_defaults in (1, "1")
	file_account, suggest_currency = _file_book_defaults(company, filename, account) if file_defaults else ("", None)
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
		currency=currency, excluded_rows=excluded_rows,
		suggest_currency=suggest_currency,
		auto_exclude=auto_exclude in (1, "1"),
	)
	if file_defaults and currency is None:
		currency = parsed.get("effective_currency")
	ledger_book = _marked_book(doc, parsed)
	if doc and mode == "append" and _ready(parsed):
		if ledger_book and not _store().ledger_export_matches(doc, parsed["rows"]):
			parsed["errors"].append({"field": "balance", "row": parsed["rows"][0]["source_row"],
				"message": "这份账面导出与当前流水有差异。请核对或选择按文件日期覆盖。"})
		elif not ledger_book and any(row.get("_legacy_source_keys") for row in parsed["rows"]):
			parsed["errors"].extend(_store().legacy_import_issues(doc, parsed["rows"]))
	book_issues, target = [], {}
	if doc is None:
		if file_defaults and account is None and not parsed["account_label"] and not parsed.get("account_conflict"):
			account = file_account
		doc, book_issues, adjustment = _new_file_book(
			company, parsed, account, opening, opening_date, currency, suggest_opening=file_defaults
		)
		if doc.account and _store().book_exists(doc.name):
			book_issues.append({"field": "account", "message": "该账户已有账簿，请选择该账户后导入银行流水。"})
		target = {
			"book_settings": {k: doc[k] for k in ("account", "opening", "opening_date", "currency")},
			"book_issues": book_issues, "opening_adjustment": adjustment,
		}
	_check_opening_date(doc, parsed)
	if mode == "replace_range" and _ready(parsed):
		impact, _, issues = _store().replacement_impact(
			doc, parsed["rows"], parsed["opening"], **({"ledger_book": ledger_book} if ledger_book else {})
		)
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
		"currency": currency, "suggested_currency": parsed.get("suggested_currency"),
		"excluded_count": len(parsed["excluded_source_rows"]),
		"excluded_source_rows": parsed["excluded_source_rows"],
		**review,
		**target,
	}


@frappe.whitelist(methods=["POST"])
def apply_import(
	book=None, filename=None, content_base64=None, mapping=None, preview_id=None, version=None,
	request_id=None, header_row=None, edits=None, company=None, account=None, opening=None, opening_date=None, mode="append",
	currency=None, excluded_rows=None,
):
	doc = _file_target(book, company, "create", (account, opening, opening_date), mode)
	currency = _file_currency(doc, currency)
	file_hash, mapping, parsed = _read(
		filename, content_base64, mapping, header_row=header_row, edits=edits,
		currency=currency, excluded_rows=excluded_rows,
	)
	ledger_book = _marked_book(doc, parsed)
	issues, adjustment = [], None
	if doc is None:
		doc, issues, adjustment = _new_file_book(company, parsed, account, opening, opening_date, currency)
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
	if adjustment or parsed["excluded_source_rows"]:
		options["request_facts"] = [scope, preview_id]
		options["import_review"] = dict(
			file_sha256=file_hash, currency=currency, excluded_source_rows=parsed["excluded_source_rows"],
			**(adjustment or {}),
		)
	if ledger_book:
		options["ledger_book"] = ledger_book
	result = _store().apply_batch(
		doc.name,
		parsed["rows"],
		"Import",
		version,
		request_id,
		opening=adjustment["file_opening"] if adjustment else parsed["opening"],
		opening_date=parsed["opening_date"] if book else doc.opening_date,
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
	book_currency = _store().book_currency(doc)
	if currency != book_currency:
		frappe.throw(f"请明确使用此账簿的币种代码 {book_currency}")
	try:
		parsed = formats.parse_rows(rows)
	except formats.StatementError as exc:
		frappe.throw(str(exc))
	_check_opening_date(doc, parsed)
	return doc, parsed


@frappe.whitelist(methods=["POST"])
def preview_rows(book=None, company=None, account=None, currency=None, rows=None, **extra):
	"""Preview extracted bank transactions in the selected account's currency."""
	doc, parsed = _read_rows(book, company, account, currency, rows, extra)
	version = int(doc.revision or 0)
	token = _sign(doc, "bank-journal-rows/v1:" + currency, parsed["input_hash"], None, version) if not parsed["errors"] else ""
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
	scope = "bank-journal-rows/v1:" + currency
	if (
		parsed["errors"]
		or type(version) is not int
		or version < 0
		or not isinstance(preview_id, str)
		or not re.fullmatch(r"[0-9a-f]{64}", preview_id)
		or not hmac.compare_digest(_sign(doc, scope, parsed["input_hash"], None, version), preview_id)
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
		request_facts=[scope, parsed["input_hash"]],
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
		currency=data.get("currency", "CNY"),
		ledger_book=_ledger_marker(book) if Decimal(str(data.get("bank_balance_offset") or 0)) else None,
	)
	label = re.sub(r'[/\\:*?"<>|\x00-\x1f]', "_", str(data["account"]))[:128]
	return {
		"filename": f"{label}银行日记账-{period or '全账簿'}.csv",
		"content_base64": base64.b64encode(content).decode("ascii"),
	}
