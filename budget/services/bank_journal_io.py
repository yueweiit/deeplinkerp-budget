"""Bank statement import and the bilingual bank journal CSV layout."""

from __future__ import annotations

import codecs
import csv
import hashlib
import io
import json
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from pathlib import Path
from zipfile import BadZipFile, ZipFile

from budget.services.bank_journal_contract import TEXT, as_date, clean_row, references

MAX_BYTES = 6 * 1024 * 1024
MAX_MOVEMENTS = 10000
MAX_ROWS = 20000
MAX_COLS = 160
MAX_CELLS = 1500000
MAX_UNPACKED = 48 * 1024 * 1024
HEADER_SCAN = 40
REVIEW_PAGE_SIZE = 100
REVIEW_FIELDS = ("date", "summary", "income", "expense", "balance")
CENT = Decimal("0.01")
HEADERS = (
	"Años\n年",
	"Mes\n月",
	"Día\n日",
	"DING TALK \n钉钉申请单号",
	"类别",
	"二级明细",
	"三级明细",
	"所属项目",
	"Resumen\n摘要",
	"ABONOS\n收入",
	"CARGOS\n支出",
	"SALDO\n余额",
	"发票寄送信息",
	"归属部门",
)
KEYS = (
	"year",
	"month",
	"day",
	"refs",
	"category",
	"level2",
	"level3",
	"project",
	"summary",
	"income",
	"expense",
	"balance",
	"invoice",
	"dept",
)
TEXT_COLS = {4: "category", 5: "level2", 6: "level3", 7: "project", 8: "summary", 12: "invoice", 13: "dept"}
DATE_PARTS = ("year", "month", "day")
ALIASES = {
	"year": ("年", "年份", "year", "año", "años", "ano", "anos", "yyyy"),
	"month": ("月", "月份", "month", "mes", "mm"),
	"day": ("日", "号", "day", "día", "dia", "dd"),
	"date": (
		"日期",
		"交易日期",
		"交易日",
		"交易时间",
		"记账日期",
		"记帐日期",
		"入账日期",
		"入账时间",
		"交易日期时间",
		"账务日期",
		"交易年月日",
		"bookingdate",
		"postingdate",
		"transactiondate",
		"date",
		"fecha",
	),
	"summary": (
		"摘要",
		"交易摘要",
		"业务摘要",
		"交易说明",
		"交易用途",
		"用途",
		"附言",
		"description",
		"memo",
		"resumen",
	),
	"remark": ("备注", "补充备注", "备注说明", "note", "notes", "remark", "remarks", "observaciones"),
	"income": (
		"收入",
		"收入金额",
		"转入金额",
		"入账金额",
		"收入发生额",
		"收款金额",
		"存入",
		"收入人民币",
		"creditamount",
		"abonos",
		"abono", "income", "credit", "credits", "deposit", "deposits", "depósito", "depósitos",
		"deposito", "depositos", "crédito", "créditos", "存款金额", "存入金额",
	),
	"expense": (
		"支出",
		"支出金额",
		"转出金额",
		"付出金额",
		"支出发生额",
		"付款金额",
		"支取",
		"支出人民币",
		"debitamount",
		"cargos",
		"cargo", "expense", "debit", "debits", "withdrawal", "withdrawals", "retiro", "retiros",
		"débito", "débitos", "取款金额", "取出金额",
	),
	"amount": ("交易金额", "发生金额", "发生额", "金额", "交易金额人民币", "amount"),
	"direction": ("收支方向", "收支类型", "交易方向", "收付方向", "direction"),
	"balance": ("余额", "账户余额", "交易余额", "账面余额", "本次余额", "balance", "saldo"),
	"refs": ("钉钉单号", "钉钉申请单号", "钉钉审批单号", "钉钉审批编号"),
	"currency": ("币种", "交易币种", "账户币种", "币别", "货币", "货币代码", "currency", "currencycode", "accountcurrency", "moneda"),
	"bank_ref": (
		"银行流水号",
		"交易流水号",
		"交易序号",
		"银行交易流水号",
		"交易参考号",
		"交易识别码",
		"银行交易号",
		"bankreference",
	),
	"category": ("类别",),
	"level2": ("二级明细",),
	"level3": ("三级明细",),
	"project": ("所属项目", "项目"),
	"invoice": ("发票寄送信息",),
	"dept": ("归属部门", "部门"),
}
MONEY = re.compile(r"[+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?\Z", re.ASCII)
LEDGER_MARKER = "DeepLinkERP/bank-journal/ledger-balance/v1:"
CURRENCY_NAMES = {
	"人民币": "CNY", "人民币元": "CNY", "RMB": "CNY", "156": "CNY",
	"美元": "USD", "美金": "USD", "US$": "USD", "840": "USD",
	"墨西哥比索": "MXN", "墨西哥披索": "MXN", "PESO MEXICANO": "MXN",
	"PESOS MEXICANOS": "MXN", "MX$": "MXN", "MEX$": "MXN", "484": "MXN",
	"欧元": "EUR", "€": "EUR", "英镑": "GBP", "£": "GBP", "日元": "JPY",
	"港元": "HKD", "港币": "HKD", "HK$": "HKD", "澳元": "AUD", "A$": "AUD",
	"加元": "CAD", "CA$": "CAD", "新加坡元": "SGD", "S$": "SGD",
}
DOLLAR_CURRENCIES = frozenset(
	"ARS AUD BSD BBD BMD BND BZD CAD CLP COP CUP DOP FJD GYD HKD JMD KYD LRD MXN NAD NZD SGD "
	"SBD SRD TTD TWD USD UYU XCD".split()
)
AMBIGUOUS_SYMBOLS = {"$": DOLLAR_CURRENCIES, "¥": {"CNY", "JPY"}, "￥": {"CNY", "JPY"}}
MONEY_PREFIX = re.compile(
	"^(?:" + "|".join(re.escape(key) for key in sorted(CURRENCY_NAMES, key=len, reverse=True) if not key.isdigit())
	+ r"|[A-Z]{3}(?![A-Z])|[$¥￥])", re.I | re.ASCII
)


class StatementError(ValueError):
	"""Friendly error suitable for showing next to a source cell."""

	def __init__(self, message, field="file"):
		super().__init__(message)
		self.field = field


class Formula(str):
	"""An inert Excel expression and its optional last calculated value."""

	cached = None
	issue = "公式单元格请先粘贴为值"


def _str(value):
	if value is None:
		return ""
	if isinstance(value, Formula):
		raise StatementError("公式单元格请先粘贴为值")
	return str(value)


def _currency_token(token):
	token = str(token).strip().upper()
	if token in AMBIGUOUS_SYMBOLS:
		return token
	if token in CURRENCY_NAMES:
		return CURRENCY_NAMES[token]
	if re.fullmatch(r"[A-Z]{3}", token, re.ASCII):
		return token
	raise StatementError("无法识别来源币种，请核对文件中的货币代码", "currency")


def _check_currency(token, currency):
	if token and currency:
		compatible = AMBIGUOUS_SYMBOLS.get(token, {token})
		if currency not in compatible:
			raise StatementError(f"来源币种 {token} 与账簿币种 {currency} 不符", "currency")


def _money_prefix(text):
	match = MONEY_PREFIX.match(text.lstrip("(").strip())
	return _currency_token(match[0]) if match else None


def _money(value, field, *, title=False, currency="CNY"):
	if isinstance(value, Formula):
		raise StatementError(value.issue, field)
	text = _str(value).strip()
	if text in ("", "-", "—", "--"):
		return None
	bracketed = text.startswith("(") and text.endswith(")")
	if bracketed:
		text = text[1:-1].strip()
	prefix = MONEY_PREFIX.match(text)
	if prefix:
		_check_currency(_currency_token(prefix[0]), currency)
		text = text[prefix.end():].strip()
	if bracketed or (text.startswith("(") and text.endswith(")")):
		text = "-" + (text if bracketed else text[1:-1])
	if len(text) > 40 or not MONEY.fullmatch(text):
		raise StatementError("金额格式不正确，请填写该账簿币种的金额", field)
	try:
		amount = Decimal(text.replace(",", ""))
		rounded = amount.quantize(CENT)
	except InvalidOperation:
		raise StatementError("金额超出支持范围", field) from None
	if abs(amount) >= Decimal("1e13") or (rounded != amount and not title):
		raise StatementError("金额须在支持范围内，最多保留两位小数", field)
	return format(rounded, ".2f")


def _date(value):
	if isinstance(value, (date, datetime)):
		return value.isoformat()[:10]
	text = _str(value).strip()
	match = re.fullmatch(
		r"(\d{4})[-/.年](\d{1,2})[-/.月](\d{1,2})日?(?:[ T]\d{1,2}:\d{2}(?::\d{2}(?:\.\d+)?)?)?", text
	)
	if not match:
		match = re.fullmatch(r"(\d{4})(\d\d)(\d\d)", text)
	try:
		if match:
			return date(*map(int, match.groups())).isoformat()
	except ValueError:
		pass
	raise StatementError("日期无效，请使用年、月、日", "date")


def _references(value):
	if not _str(value).strip():
		return []
	if not isinstance(value, str) or re.search(r"(?i)\d+(?:\.\d*)?e[+-]?\d+", value):
		raise StatementError("钉钉申请单号须为完整文本，请检查表格数字精度", "refs")
	try:
		return references(value)
	except ValueError as exc:
		raise StatementError(str(exc), "refs") from exc


class _ExcelRow(list):
	"""Keep number-format currency evidence separate from the original typed cell values."""

	__slots__ = ("currencies",)


@lru_cache(maxsize=256)
def _format_currencies(fmt):
	tokens = set()
	# Excel's locale suffix (e.g. -409) does not identify the currency.
	for code, literal in re.findall(r'\[\$([^\]]*)\]|"([^"]+)"', fmt):
		token = (code.split("-", 1)[0] if code else literal).strip()
		if token:
			try:
				tokens.add(_currency_token(token))
			except StatementError:
				pass
	plain = re.sub(r'\[[^\]]*\]|"[^"]*"', " ", fmt).replace("\\", "")
	for token in re.findall(r"(?:MEX|MX|US|HK|CA|A|S)\$|[$￥¥€£]|\b[A-Z]{3}\b(?=\s*[#0])", plain):
		tokens.add(_currency_token(token))
	return frozenset(tokens)


def _excel_row(cells):
	values = _ExcelRow(Formula(c.value) if c.data_type == "f" else c.value for c in cells)
	values.currencies = {}
	for i, c in enumerate(cells):
		if c.value is not None and (c.data_type in ("n", "f")) and c.number_format not in (None, "General"):
			tokens = _format_currencies(c.number_format)
			if tokens:
				values.currencies[i] = tokens
	return values


def _xlsx(content):
	from openpyxl import load_workbook

	try:
		with ZipFile(io.BytesIO(content)) as z:
			members = z.infolist()
			if len(members) > 1200 or sum(i.file_size for i in members) > MAX_UNPACKED:
				raise StatementError("Excel 解压后过大，请分文件导入")
			if any(i.filename.endswith("vbaProject.bin") for i in members):
				raise StatementError("请选择不含宏的 .xlsx 文件")
		with io.BytesIO(content) as stream:
			wb = load_workbook(stream, read_only=True, data_only=False, keep_links=False)
			try:
				sheets = [ws for ws in wb.worksheets if ws.sheet_state == "visible"]
				if len(sheets) != 1:
					raise StatementError("请将需要导入的工作表单独存为一个文件")
				ws = sheets[0]
				if (ws.max_row or 0) > MAX_ROWS or (ws.max_column or 0) > MAX_COLS:
					raise StatementError("工作表过大，请清除未用单元格或分文件导入")
				# Do not let incorrect sheet dimensions omit trailing transactions.
				ws.reset_dimensions()
				rows = _limited(_excel_row(line) for line in ws.iter_rows())
				if any(isinstance(cell, Formula) for row in rows for cell in row):
					with io.BytesIO(content) as cached_stream:
						cached_wb = load_workbook(cached_stream, read_only=True, data_only=True, keep_links=False)
						try:
							cached_sheet = cached_wb[ws.title]
							cached_sheet.reset_dimensions()
							for row, cached in zip(rows, cached_sheet.values, strict=True):
								for idx, cell in enumerate(row):
									if isinstance(cell, Formula):
										cell.cached = cached[idx]
						finally:
							cached_wb.close()
				return rows
			finally:
				wb.close()
	except StatementError:
		raise
	except (BadZipFile, KeyError, ValueError, OSError, SyntaxError, TypeError) as exc:
		raise StatementError("文件不是可读取的 .xlsx 工作簿") from exc


def _limited(lines):
	rows = []
	cells = 0
	for line in lines:
		cells += len(line)
		if len(rows) >= MAX_ROWS or len(line) > MAX_COLS or cells > MAX_CELLS:
			raise StatementError("流水文件过大，请按月份拆分")
		if any(isinstance(x, str) and len(x) > 20000 for x in line):
			raise StatementError("单元格文字过长，请检查文件格式")
		rows.append(line)
	return rows


def _read(content, filename):
	if not isinstance(content, bytes) or not 0 < len(content) <= MAX_BYTES:
		raise StatementError("请选择大小不超过 6 MB 的流水文件")
	ext = Path(str(filename)).suffix.lower()
	if ext == ".xlsx":
		return _xlsx(content), False
	if ext != ".csv":
		raise StatementError("支持 .csv 和 .xlsx，旧版 .xls 请先另存")
	for codec in ("utf-8-sig", "gb18030"):
		try:
			text = content.decode(codec)
			break
		except UnicodeDecodeError:
			continue
	else:
		raise StatementError("CSV 编码无法识别，请另存为 UTF-8")
	if "\x00" in text:
		raise StatementError("CSV 含有无效字符，请另存为 UTF-8")
	# The original journal is comma-delimited, even where its first row is sparse.
	first = next((line for line in text.splitlines() if line.strip()), "")
	delim = "\t" if first.count("\t") > first.count(",") else ","
	if delim == "," and ";" in first and first.count(";") > first.count(","):
		delim = ";"
	try:
		return _limited(
			csv.reader(io.StringIO(text, newline=""), delimiter=delim, strict=True)
		), text.endswith(("\n", "\r"))
	except csv.Error as exc:
		raise StatementError("CSV 引号或分隔符格式错误") from exc


def _normal(value):
	return re.sub(r"[\s_()（）]", "", str(value or "")).lower()


def _qualified_header(part):
	match = re.fullmatch(r"(.+?)(?:\s*[（(\[]\s*([^()（）\[\]]+?)\s*[）)\]]|[\s/]+([A-Z]{3}))", part.strip())
	if match:
		try:
			return match[1], _currency_token(match[2] or match[3])
		except StatementError:
			pass
	return part, None


def _header_terms(value):
	if not isinstance(value, str) or isinstance(value, Formula):
		return set()
	parts = [value, *value.splitlines()]
	terms = {_normal(part) for part in parts}
	for part in parts:
		terms.add(_normal(_qualified_header(part)[0]))
	return terms


def _suggest(header):
	names = [_header_terms(x) for x in header]
	result = {}
	for key, aliases in ALIASES.items():
		picks = [i for i, terms in enumerate(names) if terms.intersection(aliases)]
		if len(picks) == 1:
			result[key] = picks[0]
	return result


def _usable(mapping):
	has_date = "date" in mapping or all(part in mapping for part in DATE_PARTS)
	return has_date and any(k in mapping for k in ("income", "expense", "amount"))


def _mapping(header, mapping):
	if not isinstance(mapping, dict) or mapping.keys() - ALIASES.keys():
		raise StatementError("列映射含未知字段")
	result = {}
	for key, value in mapping.items():
		if value in (None, ""):
			continue
		if isinstance(value, bool) or not re.fullmatch(r"\d{1,3}", str(value)):
			raise StatementError("列映射仅接受表头列序号")
		col = int(value)
		if not 0 <= col < len(header):
			raise StatementError("列映射超出表头范围")
		result[key] = col
	if len(set(result.values())) != len(result):
		raise StatementError("同一来源列只能映射到一个字段")
	if "date" in result and any(k in result for k in DATE_PARTS):
		raise StatementError("请选择完整日期列，或分别选择年、月、日三列")
	if "amount" in result and any(k in result for k in ("income", "expense")):
		raise StatementError("请在收支分列与交易金额中选择一种金额口径")
	return result


def _at(row, index):
	return row[index] if index is not None and index < len(row) else ""


def _movement(income, expense):
	a, b = Decimal(income or 0), Decimal(expense or 0)
	if a < 0 or b < 0 or bool(a) == bool(b):
		raise StatementError("每笔流水只能有一个正数收入或支出", "amount")
	return (income if a else None), (expense if b else None)


def _bank_reference(value):
	ref = _str(value).strip()
	if isinstance(value, (int, float, Decimal)):
		number = Decimal(str(value))
		if abs(number) >= Decimal("1e15") or number != number.to_integral_value():
			raise StatementError("银行流水号可能已丢失数字精度，请使用原始文本编号", "bank_ref")
		return format(number, ".0f")
	if re.fullmatch(r"(?i)[0-9]+(?:\.\d+)?e[+-]?\d+", ref):
		raise StatementError("请使用完整银行流水号，不要使用科学计数法", "bank_ref")
	return ref


def _file_date(read, cols):
	if "date" in cols:
		return read("date")
	return "-".join(_str(read(k)).strip() for k in DATE_PARTS)


def _file_amounts(read, cols, currency):
	income = _money(read("income"), "income", currency=currency)
	expense = _money(read("expense"), "expense", currency=currency)
	if "amount" in cols:
		value = _money(read("amount"), "amount", currency=currency)
		if value is None:
			return None, None
		amount = Decimal(value)
		direction = _str(read("direction")).strip().lower()
		if "direction" in cols and not direction:
			raise StatementError("收支方向为空", "direction")
		if direction in ("支出", "付", "付款", "转出", "出", "借", "借方", "d", "debit"):
			expense = str(abs(amount))
		elif direction in ("收入", "收", "收款", "转入", "入", "贷", "贷方", "c", "credit"):
			if amount < 0:
				raise StatementError("收入方向与负金额冲突", "direction")
			income = str(amount)
		elif direction:
			raise StatementError("无法识别该收支方向", "direction")
		elif amount < 0:
			expense = str(-amount)
		else:
			income = str(amount)
	return income, expense


def _parse_row(raw, cols, original, currency, edits=None, *, allow_amountless=False):
	def read(key):
		return _at(raw, cols.get(key))

	edits = edits or {}
	day = _date(edits["date"] if "date" in edits else _file_date(read, cols))
	if "income" in edits:
		income, expense = (_money(edits[key], key, currency=currency) for key in ("income", "expense"))
	else:
		income, expense = _file_amounts(read, cols, currency)
	balance = _money(edits.get("balance", read("balance")), "balance", currency=currency)
	if "balance" in edits and balance is None and not _blank(read("balance")):
		raise StatementError("原流水有余额，请填写核对后的余额", "balance")
	value = {
		"date": day,
		"income": income,
		"expense": expense,
		"balance": balance,
		"refs": _references(read("refs")),
		"bank_ref": _bank_reference(read("bank_ref")),
	}
	value.update({k: _str(edits.get(k, read(k))).strip() for k in TEXT})
	if "summary" not in cols and "summary" not in edits:
		value["summary"] = value["remark"]
	if len(value["bank_ref"]) > 140:
		raise StatementError("银行流水号过长", "bank_ref")
	if allow_amountless and income is None and expense is None:
		if any(len(value[k]) > 12000 or "\x00" in value[k] for k in TEXT):
			raise StatementError("流水文本过长或包含无效字符。", "row")
	else:
		value["income"], value["expense"] = _movement(income, expense)
		try:
			clean_row(value)
		except ValueError as exc:
			raise StatementError(str(exc), "row") from exc
	if original:
		value["raw_cells"] = [_str(_at(raw, i)) for i in range(15 if "remark" in cols else 14)]
	return value


def _review_values(raw, cols, currency):
	"""Expose individual source cells even when their row cannot yet be imported."""
	def read(key):
		return _at(raw, cols.get(key))

	def display(value):
		return "" if value is None else str(value).strip()[:12000]

	day = read("date")
	if "date" not in cols:
		day = "-".join(display(read(k)) for k in DATE_PARTS)
	try:
		day = _date(day)
	except StatementError:
		pass
	try:
		income, expense = _file_amounts(read, cols, currency)
	except StatementError:
		income, expense = read("income"), read("expense")
	return {
		"date": display(day),
		"summary": display(read("summary")),
		"remark": display(read("remark")),
		"income": display(income),
		"expense": display(expense),
		"balance": display(read("balance")),
	}


def _record_key(*values):
	return hashlib.sha256(json.dumps(values, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def _first_nonempty(rows):
	return next((i for i, row in enumerate(rows) if any(c not in ("", None) for c in row)), None)


def _movement_decimal(row):
	return Decimal(row["income"] or 0) - Decimal(row["expense"] or 0)


def _order_and_validate(result, supplied_opening):
	rows = result["rows"]
	if not rows:
		return
	days = [r["date"] for r in rows]
	if days != sorted(days):
		if days == sorted(days, reverse=True):
			rows.reverse()
		else:
			result["errors"].append(
				{"row": rows[0]["source_row"], "field": "date", "message": "请按交易日期顺序排列流水"}
			)
			return
	elif days[0] == days[-1] and len(rows) > 1 and all(r["balance"] is not None for r in rows):
		# Several transactions on one date can be in either posting order.
		def chains(entries):
			previous = Decimal(entries[0]["balance"])
			for entry in entries[1:]:
				current = Decimal(entry["balance"])
				if previous + _movement_decimal(entry) != current:
					return False
				previous = current
			return True

		if not chains(rows) and chains(rows[::-1]):
			rows.reverse()
	balance = Decimal(supplied_opening) if supplied_opening is not None else None
	if balance is None and rows[0]["balance"] is not None:
		balance = Decimal(rows[0]["balance"]) - _movement_decimal(rows[0])
		result["opening"] = format(balance, ".2f")
	for row in rows:
		if balance is not None:
			balance += _movement_decimal(row)
		if row["balance"] is not None:
			stated = Decimal(row["balance"])
			if balance is not None and balance != stated:
				result["errors"].append(
					{
						"row": row["source_row"],
						"field": "balance",
						"message": "该行余额与前余额及本笔收支不符",
					}
				)
			balance = stated


# In earlier CNY import receipts these columns determined the bank-movement key.
# Reconstruct the historical key to recognize those receipts without changing them.
def _legacy_columns(header, cols):
	aliases = (
		("摘要", "交易摘要", "业务摘要", "交易说明", "交易用途", "用途", "附言", "备注", "description", "memo"),
		("余额", "账户余额", "交易余额", "账面余额", "本次余额", "balance"),
	)
	names = [re.sub(r"[\s_()（）]", "", str(cell or "")).lower() for cell in header]
	indices = []
	for choices in aliases:
		matches = [i for i, name in enumerate(names) if name in choices]
		indices.append(matches[0] if len(matches) == 1 else None)
	current = [cols.get("summary", cols.get("remark")), cols.get("balance")]
	return indices if indices != current else None


def _legacy_key(raw, source, columns, occurrences):
	summary, balance = columns
	try:
		old_balance = _money(_at(raw, balance), "balance", currency="CNY")
	except StatementError:
		return None
	identity = (source["date"], source["income"], source["expense"], old_balance, _str(_at(raw, summary)).strip())
	occurrences[identity] += 1
	return _record_key("bank-movement", *identity, occurrences[identity])


def _append_row(result, row, row_number, occurrences, seen_ids, *, source=None, repaired_key=None):
	if row["bank_ref"]:
		if row["bank_ref"] in seen_ids:
			raise StatementError("同一批次中银行流水号重复，请核对该行", "bank_ref")
		seen_ids.add(row["bank_ref"])
		key = _record_key("bank-reference", row["bank_ref"])
	elif repaired_key:
		key = repaired_key
	else:
		identity = tuple((source or row)[field] for field in ("date", "income", "expense", "balance", "summary"))
		occurrences[identity] += 1
		key = _record_key("bank-movement", *identity, occurrences[identity])
	row.update(source_row=row_number, source_key=key)
	result["rows"].append(row)


def _finish_rows(result):
	_order_and_validate(result, result["opening"])
	weak_rows = [r for r in result["rows"] if not r["bank_ref"] and r["balance"] is None]
	if weak_rows:
		# Without either bank ID or balance a matching movement in a different
		# statement can be a separate payment. Require the same file inventory.
		proof = _record_key("bank-inventory", sorted(r["source_key"] for r in result["rows"]))
		for row in weak_rows:
			row["weak_file_proof"] = proof
	result["opening_date"] = result["rows"][0]["date"]


_JSON_FIELDS = frozenset((*TEXT, "date", "income", "expense", "balance", "bank_ref", "refs"))
_JSON_MONEY = re.compile(r"-?(?:0|[1-9][0-9]{0,12})(?:\.[0-9]{1,2})?\Z")


def _json_movement(raw):
	if not isinstance(raw, dict):
		raise StatementError("每行必须是 JSON 对象", "row")
	for field, value in raw.items():
		if field not in _JSON_FIELDS:
			raise StatementError("流水含未定义的字段", field)
		if field in ("income", "expense", "balance"):
			if value is None:
				continue
			if not isinstance(value, str) or not _JSON_MONEY.fullmatch(value):
				raise StatementError("请使用精确到分的十进制金额字符串", field)
			if field != "balance" and Decimal(value) <= 0:
				raise StatementError("收入或支出金额必须大于零", field)
		elif field == "refs":
			if not isinstance(value, list):
				raise StatementError("钉钉单号须为字符串数组", field)
		elif not isinstance(value, str) or "\x00" in value or (field in TEXT and len(value) > 12000):
			raise StatementError("该字段须为有效文本", field)
	try:
		day = as_date(raw.get("date")).isoformat()
	except ValueError as exc:
		raise StatementError(str(exc), "date") from exc
	try:
		refs = references(raw.get("refs", []))
	except ValueError as exc:
		raise StatementError(str(exc), "refs") from exc
	for ref in refs:
		_references(ref)  # Apply the same precision-loss checks as the bank file reader.
	bank_ref = _bank_reference(raw.get("bank_ref", ""))
	if len(bank_ref) > 140:
		raise StatementError("银行流水号过长", "bank_ref")
	income, expense = _movement(_money(raw.get("income"), "income"), _money(raw.get("expense"), "expense"))
	return {
		**{field: raw.get(field, "").strip() for field in TEXT},
		"date": day,
		"refs": refs,
		"bank_ref": bank_ref,
		"income": income,
		"expense": expense,
		"balance": _money(raw.get("balance"), "balance"),
	}


def parse_rows(rows):
	"""Validate extracted JSON values and assign the same bank identities used by CSV/XLSX."""
	if not isinstance(rows, list) or not 1 <= len(rows) <= MAX_MOVEMENTS:
		raise StatementError("rows 须为包含 1 至 10,000 笔流水的 JSON 数组")
	try:
		payload = json.dumps(
			rows, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
		).encode()
	except (ValueError, TypeError, RecursionError) as exc:
		raise StatementError("rows 必须是可编码为 UTF-8 的 JSON 数组") from exc
	if len(payload) > MAX_BYTES:
		raise StatementError("单次流水 JSON 大小不能超过 6 MiB")
	result = {
		"rows": [],
		"errors": [],
		"opening": None,
		"opening_date": None,
		"input_hash": hashlib.sha256(payload).hexdigest(),
	}
	occurrences, seen_ids = Counter(), set()
	for number, raw in enumerate(rows, 1):
		try:
			_append_row(result, _json_movement(raw), number, occurrences, seen_ids)
		except StatementError as exc:
			result["errors"].append({"row": number, "field": exc.field, "message": str(exc)})
	if not result["errors"]:
		_finish_rows(result)
	return result


def _header_index(sheet, first, header_row):
	if isinstance(header_row, bool) or not re.fullmatch(r"\d{1,5}", str(header_row)):
		raise StatementError("表头行号无效")
	index = int(header_row) - 1
	if not first <= index < min(len(sheet), first + HEADER_SCAN):
		raise StatementError("表头行号超出可识别范围")
	return index


def _statement_account(preamble):
	# Only the bank's explicit own-account metadata above the transaction
	# header is eligible. A counterparty column or a file name is not a book.
	labels = {"银行账户名称": "name", "账簿名称": "name", "账户别名": "name", "开户银行": "bank",
			  "开户行": "bank", "本方账号": "number", "银行账号": "number", "账号": "number"}
	found = {"name": set(), "bank": set(), "number": set()}
	for row in preamble:
		for index, cell in enumerate(row):
			if not isinstance(cell, str) or isinstance(cell, Formula):
				continue
			parts = re.split(r"[:：]", cell.strip(), maxsplit=1)
			key = labels.get(parts[0].strip())
			if key:
				value = parts[1].strip() if len(parts) == 2 else ""
				if not value:
					value = _at(row, index + 1)
				if isinstance(value, str) and not isinstance(value, Formula) and value.strip():
					found[key].add(value.strip())
	if any(len(values) > 1 for values in found.values()):
		return ""
	if found["name"]:
		return next(iter(found["name"]))
	if found["number"]:
		return " ".join((*found["bank"], *found["number"]))
	return ""


def _blank(value):
	return value is None or (isinstance(value, str) and not value.strip())


@dataclass
class _SourceColumns:
	amounts: set[int]
	money: set[int]
	currencies: set[int]
	references: set[int]
	explained: set[int]
	omitted_amounts: dict[int, str]
	omitted_balances: set[int]


def _source_columns(header, mapping):
	# A manual/AI mapping cannot remove independent currency or movement
	# evidence in an explicitly labelled column, including repeated labels.
	terms = [_header_terms(cell) for cell in header]
	def indices(*keys):
		result = {mapping[k] for k in keys if k in mapping}
		aliases = {name for k in keys for name in ALIASES[k]}
		return result | {i for i, labels in enumerate(terms) if labels & aliases}

	amounts = indices("income", "expense", "amount")
	movement_keys = ("income", "expense", "amount")
	safe_names = {name for key, aliases in ALIASES.items() if key not in movement_keys for name in aliases}
	explained = {i for i, labels in enumerate(terms) if labels & safe_names}
	explained.update(mapping[k] for k in ("date", *DATE_PARTS, *movement_keys) if k in mapping)
	# A model can recognize a nonstandard balance/reference header. Retain its
	# source semantics when numeric values could otherwise be receipts.
	for key, words in (("balance", ("余款", "结余", "余额", "balance", "saldo")),
					   ("bank_ref", ("编号", "流水号", "序号", "reference", "ref", "número", "folio"))):
		if key in mapping and any(word in _normal(_at(header, mapping[key])) for word in words):
			explained.add(mapping[key])
	omitted = {}
	for key in movement_keys:
		for i, labels in enumerate(terms):
			if labels.intersection(ALIASES[key]) and mapping.get(key) != i:
				omitted[i] = key
	balances = indices("balance")
	return _SourceColumns(amounts, amounts | balances, indices("currency"), indices("bank_ref", "refs"),
						  explained, omitted, balances - {mapping.get("balance")})


def _possible_money(value):
	if isinstance(value, (Formula, int, float, Decimal)):
		return True
	text = str(value).strip()
	if text.startswith("=") or any(unicodedata.category(char) == "Sc" for char in text):
		return True
	if not any(char.isdigit() for char in text):
		return False
	text = re.sub(r"\s+[A-Za-z]{3}$", "", text)
	text = MONEY_PREFIX.sub("", text.lstrip("(").strip(), count=1)
	return bool(re.fullmatch(r"[+\-−]?[\d.,][\d.,\s'’]*(?:[eE][+-]?\d+)?\)?", text.strip()))


def _check_source_amounts(raw, cols, evidence, currency):
	for index in evidence.omitted_balances:
		value = _at(raw, index)
		if _blank(value):
			continue
		selected = _at(raw, cols.get("balance"))
		if _blank(selected) or _money(value, "balance", currency=currency) != _money(selected, "balance", currency=currency):
			raise StatementError(f"第 {index + 1} 列含有未经核对的余额，请检查余额列映射", "mapping")
	for index, key in evidence.omitted_amounts.items():
		value = _at(raw, index)
		if _blank(value):
			continue
		# A report can show a signed amount alongside its split presentation.
		# It may be left unmapped only if the two independently agree.
		if index not in cols.values() and ((key == "amount" and ("income" in cols or "expense" in cols))
				or (key in ("income", "expense") and "amount" in cols)):
			income, expense = _file_amounts(lambda k: _at(raw, cols.get(k)), cols, currency)
			expected = {"income": Decimal(income or 0), "expense": Decimal(expense or 0)}
			expected["amount"] = expected["income"] - expected["expense"]
			stated = _money(value, key, currency=currency)
			if stated is not None and Decimal(stated) == expected[key] and (income is not None or expense is not None):
				continue
		raise StatementError(f"第 {index + 1} 列含有收支金额，请核对金额列映射", "mapping")
	for index, value in enumerate(raw):
		if index not in evidence.explained and index not in evidence.omitted_amounts and not _blank(value) and _possible_money(value):
			raise StatementError(f"第 {index + 1} 列含有可能的金额，请核对并映射该列", "mapping")


def _row_currency_tokens(raw, evidence):
	for index in evidence.currencies:
		declared = _at(raw, index)
		if not _blank(declared):
			if isinstance(declared, Formula):
				raise StatementError("币种须为原始文本", "currency")
			yield _currency_token(declared)
	for index in evidence.money:
		value = _at(raw, index)
		yield from getattr(raw, "currencies", {}).get(index, ())
		if isinstance(value, str) and not isinstance(value, Formula):
			token = _money_prefix(value.strip())
			if token:
				yield token


def _metadata_currencies(preamble, header, evidence):
	for line in preamble:
		for index, cell in enumerate(line):
			if not isinstance(cell, str) or isinstance(cell, Formula):
				continue
			parts = re.split(r"[:：]", cell.strip(), maxsplit=1)
			if _normal(parts[0]) in ALIASES["currency"]:
				value = parts[1].strip() if len(parts) == 2 else ""
				value = value or _at(line, index + 1)
				if not _blank(value):
					yield _currency_token(value)
	for index in evidence.money:
		label = str(_at(header, index) or "")
		if "人民币" in label:
			yield "CNY"
		for part in (label, *label.splitlines()):
			_, token = _qualified_header(part)
			if token:
				yield token


def _statement_currency(result, sheet, start, evidence, metadata, selected):
	tokens = set(metadata)
	for row in sheet[start:]:
		try:
			tokens.update(_row_currency_tokens(row, evidence))
		except StatementError:
			pass  # Record the unreadable declaration on its source row.
	explicit = tokens - AMBIGUOUS_SYMBOLS.keys()
	if len(explicit) == 1:
		hint = next(iter(explicit))
		if all(hint in AMBIGUOUS_SYMBOLS[sym] for sym in tokens & AMBIGUOUS_SYMBOLS.keys()):
			result["suggested_currency"] = hint
	if len(explicit) > 1 and not selected:
		result["errors"].append({"row": start, "field": "currency", "message": "文件含多种来源币种，请按币种拆分流水"})
	elif not selected and len(tokens) > 1 and not explicit and not set.intersection(
		*(set(AMBIGUOUS_SYMBOLS[sym]) for sym in tokens)
	):
		result["errors"].append({"row": start, "field": "currency", "message": "文件中的金额币种符号互不相容"})
	currency = selected or (next(iter(explicit)) if len(explicit) == 1 else None)
	for token in metadata:
		try:
			_check_currency(token, currency)
		except StatementError as exc:
			result["errors"].append({"row": start, "field": exc.field, "message": str(exc)})
	return currency


def _resolve_balance(sheet, index, cols, currency):
	"""Replace only a verified previous-balance + receipt - payment in our bounded in-memory copy."""
	raw = sheet[index]
	cell = _at(raw, cols.get("balance"))
	if not isinstance(cell, Formula):
		return
	from openpyxl.utils import get_column_letter

	def reference(key, number):
		return rf"\$?{get_column_letter(cols[key] + 1)}\$?{number}"

	try:
		if not index or not {"income", "expense"} <= cols.keys():
			raise StatementError("无法核对该余额公式，请先粘贴为值")
		expected = rf"=\s*{reference('balance', index)}\s*\+\s*{reference('income', index+1)}\s*-\s*{reference('expense', index+1)}\s*"
		if not re.fullmatch(expected, cell, re.I | re.ASCII):
			raise StatementError("余额公式未引用上一行余额及本行收支，请先核对并粘贴为值")
		previous = _money(_at(sheet[index - 1], cols["balance"]), "balance", currency=currency)
		if previous is None:
			raise StatementError("余额公式引用的上一行缺少可核对的余额")
		amounts = []
		for key in ("income", "expense"):
			value = _at(raw, cols[key])
			if not _blank(value) and (isinstance(value, bool) or not isinstance(value, (int, float, Decimal))):
				raise StatementError("余额公式的收支须为数值或空单元格")
			amounts.append(Decimal(_money(value, key, currency=currency) or 0))
		computed = _money(Decimal(previous) + amounts[0] - amounts[1], "balance", currency=currency)
		if cell.cached is not None and (
			isinstance(cell.cached, bool) or not isinstance(cell.cached, (int, float, Decimal))
			or _money(cell.cached, "balance", title=True, currency=currency) != computed
		):
			raise StatementError("Excel 余额公式缓存与逐行核算不符，请重新计算后导入")
		raw[cols["balance"]] = computed
	except StatementError as exc:
		cell.issue = str(exc)


def _carry_period(label):
	return re.fullmatch(r"承(\d{4})年(\d{1,2})月期末余额", label.strip())


def _is_carry(raw, cols, evidence):
	label = _at(raw, cols.get("summary"))
	if not isinstance(label, str) or isinstance(label, Formula):
		return False
	if label.strip().lower() not in ("期初余额", "上期结转", "opening balance", "saldo inicial") and not _carry_period(label):
		return False
	return (all(_blank(_at(raw, cols.get(k))) for k in ("date", *DATE_PARTS))
		and all(_blank(_at(raw, index)) for index in evidence.amounts | evidence.references))


def _exclusions(values):
	if values is None:
		return set()
	if not isinstance(values, (list, tuple, set, frozenset)) or len(values) > MAX_MOVEMENTS:
		raise StatementError("排除行必须为有效的来源行号列表", "excluded_rows")
	result = set()
	for value in values:
		if not re.fullmatch(r"[1-9][0-9]{0,4}", str(value), re.ASCII) or int(value) > MAX_ROWS or int(value) in result:
			raise StatementError("排除行号重复或超出范围", "excluded_rows")
		result.add(int(value))
	return result


def _check_prior_rows(sheet, first, header_index, cols):
	layouts = [cols]
	for row in sheet[first:header_index]:
		for layout in layouts:
			try:
				_date(_file_date(lambda k: _at(row, layout.get(k)), layout))
			except StatementError:
				continue
			raise StatementError("所选表头前已有疑似流水，请核对表头行或按表拆分", "date")
		prior = _suggest(row)
		if _usable(prior):
			layouts.append(prior)


def _preferred_mapping(known, proposal):
	# Explicit, unique source labels constrain ambiguous model suggestions.
	result = dict(known)
	for key, index in proposal.items():
		if key not in result and index not in result.values():
			result[key] = index
	if "date" in result:
		if all(k in known for k in DATE_PARTS) and "date" not in known:
			result.pop("date")
		else:
			result = {k: v for k, v in result.items() if k not in DATE_PARTS}
	if "amount" in result and ("income" in result or "expense" in result):
		result = {k: v for k, v in result.items() if k not in ("amount", "direction")}
	return result


def _native_width(sheet, first):
	if first + 2 >= len(sheet):
		return 0
	header, carry = sheet[first + 1:first + 3]
	if list(header[:14]) != list(HEADERS):
		return 0
	width = 15 if _at(header, 14) == "备注" else 14
	# A named extra column, or a first transaction instead of an opening
	# row, is a different layout and goes through header mapping.
	if any(not _blank(v) for v in header[width:]) or any(not _blank(_at(carry, i)) for i in (0, 1, 2, 3, 9, 10)):
		return 0
	return width


def _can_exclude(raw, cols, original, currency, evidence, previous_balance):
	if any(not _blank(_at(raw, i)) for i in evidence.amounts | evidence.references):
		return False
	try:
		empty = _parse_row(raw, cols, original, currency, allow_amountless=True)
	except StatementError:
		return False
	return empty["balance"] is None or empty["balance"] == previous_balance


def _reviewed_movement(raw, cols, original, currency, changes, row_number, file_hash):
	if not changes:
		return _parse_row(raw, cols, original, currency), None, None
	# Retain the normal receipt identity after a correction. A previously
	# invalid source row has a stable identity within this exact file.
	source, repaired_key = None, None
	try:
		source = _parse_row(raw, cols, original, currency)
	except StatementError:
		repaired_key = _record_key("bank-file-row", file_hash, row_number)
	row = _parse_row(raw, cols, original, currency, changes)
	before = source or _review_values(raw, cols, currency)
	changed = {field: before[field] for field in changes if (before[field] or "") != (row[field] or "")}
	if changed:
		row["file_review"] = {"file_sha256": file_hash, "source_row": row_number, "original": changed}
	return row, source, repaired_key


def parse_statement(
	content, filename, mapping=None, *, header_row=None, suggest_layout=None, edits=None, review_offset=None,
	currency="CNY", excluded_rows=None,
):
	"""Parse a bank file, report each invalid row and return canonical decimal strings."""
	sheet, final_terminator = _read(content, filename)
	first = _first_nonempty(sheet)
	if first is None:
		raise StatementError("流水文件为空")
	result = dict(
		rows=[],
		template=None,
		opening=None,
		account_label="",
		headers=[],
		needs_mapping=False,
		suggested_mapping={},
		mapping_used=None,
		header_row=None,
		mapping_source=None,
		ai_notice="",
		opening_date=None,
		suggested_currency=None,
		ledger_book=None,
		errors=[],
		review_rows=[],
		review_total=0,
	)
	native_width = _native_width(sheet, first)
	original = bool(native_width)
	start = first
	carry_label = ""
	if currency is not None and (not isinstance(currency, str) or not re.fullmatch(r"[A-Z]{3}", currency, re.ASCII)):
		raise StatementError("请选择账簿币种的三位货币代码", "currency")
	if original:
		if header_row is not None:
			raise StatementError("原日记账模板使用固定表头")
		title, header, carry = sheet[first : first + 3]
		width = len(header)
		has_remark = native_width == 15
		marker = _at(title, 15)
		if isinstance(marker, str) and re.fullmatch(re.escape(LEDGER_MARKER) + r"[0-9a-f]{64}", marker):
			result["ledger_book"] = marker[len(LEDGER_MARKER):]
		if any(not _blank(cell)
			for row_index, row in enumerate(sheet)
			for column, cell in enumerate(row[native_width:], native_width)
			if not (row_index == first and column == 15 and result["ledger_book"])):
			raise StatementError(f"原表 {native_width} 个字段后还有内容，请核对额外列")
		result["account_label"] = _str(_at(title, 0)).strip()
		cols = {key: index for index, key in enumerate(KEYS)}
		if has_remark:
			cols["remark"] = 14
		start = first + 3
		result["headers"] = list(header[:native_width])
		evidence = _source_columns(header, cols)
		metadata = set(_row_currency_tokens(carry, evidence))
		title_currency = _money_prefix(_str(_at(title, 12)).strip())
		if title_currency:
			metadata.add(title_currency)
		currency = _statement_currency(result, sheet, start, evidence, metadata, currency)
		result["opening"] = _money(_at(carry, 11), "opening", currency=currency)
		if result["opening"] is None:
			raise StatementError("期初余额缺失", "opening")
		carry_label = _str(_at(carry, 8))
		result["template"] = dict(
			title=[_str(c) for c in title[:native_width]],
			carry=[_str(c) for c in carry[:native_width]],
			width=width,
			total_rows=len(sheet),
			trailing_newline=final_terminator,
			start_empty=first,
			closing_row=None,
			header_total=_money(_at(title, 12), "balance", title=True, currency=currency),
		)
		if has_remark:
			result["template"]["remark_column"] = True
	else:
		candidates = [
			(i, _suggest(row)) for i, row in enumerate(sheet[first : first + HEADER_SCAN], first)
		]
		header_index, suggested = max(candidates, key=lambda x: (_usable(x[1]), len(x[1])))
		if header_row is not None:
			header_index = _header_index(sheet, first, header_row)
			suggested = _suggest(sheet[header_index])
			result["header_row"] = header_index + 1
		elif mapping is None and suggest_layout and (
			not _usable(suggested) or any(k in suggested for k in DATE_PARTS)
			or len(suggested) < sum(not _blank(v) for v in sheet[header_index])
		):
			try:
				layout = suggest_layout(sheet, first)
				if layout:
					ai_row, proposal = layout
					ai_index = _header_index(sheet, first, ai_row)
					declared = _suggest(sheet[ai_index])
					ai_cols = _mapping(sheet[ai_index], _preferred_mapping(declared, proposal))
					if _usable(ai_cols):
						_check_prior_rows(sheet, first, ai_index, ai_cols)
						header_index, suggested = ai_index, ai_cols
						result.update(header_row=ai_row, mapping_source="deepseek")
			except StatementError as exc:
				result["ai_notice"] = str(exc)
		header = [_str(c) for c in sheet[header_index]]
		suggested = _preferred_mapping(suggested, {})
		result.update(headers=header, suggested_mapping=suggested, account_label=_statement_account(sheet[first:header_index]),
					  header_row=header_index + 1)
		requested = suggested if mapping is None else mapping
		if isinstance(requested, dict):
			# Keep checkable source evidence when a user remaps other columns.
			declared = _suggest(header)
			requested = {**requested, **{k: declared[k] for k in ("currency", "balance", "bank_ref") if k in declared}}
		cols = _mapping(header, requested)
		if not _usable(cols):
			result["needs_mapping"] = True
			return result
		_check_prior_rows(sheet, first, header_index, cols)
		start = header_index + 1
		result["mapping_used"] = cols
		evidence = _source_columns(header, cols)
		metadata = set(_metadata_currencies(sheet[first:header_index], header, evidence))
		currency = _statement_currency(result, sheet, start, evidence, metadata, currency)
	legacy_cols = _legacy_columns(header, cols) if not original and currency == "CNY" else None
	legacy_counts = Counter()
	occurrences = Counter()
	seen_ids = set()
	edits = edits or {}
	excluded = _exclusions(excluded_rows)
	if excluded.intersection(edits):
		raise StatementError("同一行不能同时排除和修改", "excluded_rows")
	unused_exclusions = set(excluded)
	unused_edits = set(edits)
	file_hash = hashlib.sha256(content).hexdigest() if edits else None
	closing_line = None
	previous_balance = result["opening"]
	earliest = None
	for i, raw in enumerate(sheet[start:], start + 1):
		if not any(c not in (None, "") for c in raw):
			continue
		transaction = False
		row = None
		changes = edits.get(i)
		excludable = False
		try:
			_resolve_balance(sheet, i - 1, cols, currency)
			if not original and _is_carry(raw, cols, evidence):
				_check_source_amounts(raw, cols, evidence, currency)
				if result["review_total"] or result["opening"] is not None:
					raise StatementError("期初余额行重复或不在流水开始位置", "opening")
				for token in _row_currency_tokens(raw, evidence):
					_check_currency(token, currency)
				result["opening"] = _money(_at(raw, cols.get("balance")), "opening", currency=currency)
				if result["opening"] is None:
					raise StatementError("期初余额缺失", "opening")
				carry_label = _str(_at(raw, cols.get("summary")))
				previous_balance = result["opening"]
				continue
			if original and not any(c not in ("", None) for j, c in enumerate(raw) if j != 11):
				if result["template"]["closing_row"] is not None:
					raise StatementError("发现重复的期末余额行", "balance")
				result["template"]["closing_row"] = _str(_at(raw, 11))
				closing_line = i
				continue
			if original and result["template"]["closing_row"] is not None:
				raise StatementError("期末余额行之后仍有流水", "date")
			transaction = True
			unused_edits.discard(i)
			for key, index in cols.items():
				cell = _at(raw, index)
				if isinstance(cell, Formula):
					raise StatementError(cell.issue, key)
			day_value = changes.get("date") if changes and "date" in changes else _file_date(lambda k: _at(raw, cols.get(k)), cols)
			day = _date(day_value)
			earliest = min(earliest, day) if earliest else day
			for token in _row_currency_tokens(raw, evidence):
				_check_currency(token, currency)
			_check_source_amounts(raw, cols, evidence, currency)
			excludable = _can_exclude(raw, cols, original, currency, evidence, previous_balance)
			if i in excluded:
				if not excludable:
					raise StatementError("只有收支原本为空且无其他冲突的流水可排除", "excluded_rows")
				unused_exclusions.discard(i)
			else:
				row, source, repaired_key = _reviewed_movement(raw, cols, original, currency, changes, i, file_hash)
				_append_row(result, row, i, occurrences, seen_ids, source=source, repaired_key=repaired_key)
				if legacy_cols and not row["bank_ref"] and not repaired_key:
					previous_key = _legacy_key(raw, source or row, legacy_cols, legacy_counts)
					if previous_key and previous_key != row["source_key"]:
						row["_legacy_source_key"] = previous_key
				if row["balance"] is not None:
					previous_balance = row["balance"]
				elif previous_balance is not None:
					previous_balance = format(Decimal(previous_balance) + _movement_decimal(row), ".2f")
		except StatementError as exc:
			result["errors"].append({"row": i, "field": exc.field, "message": str(exc)})
			if not excludable:
				previous_balance = None
		if transaction:
			index = result["review_total"]
			result["review_total"] += 1
			if review_offset is not None and review_offset <= index < review_offset + REVIEW_PAGE_SIZE:
				values = row if row is not None else {**_review_values(raw, cols, currency), **(changes or {})}
				result["review_rows"].append({"source_row": i, "excludable": excludable, "excluded": i in excluded,
					**{field: values[field] for field in (*REVIEW_FIELDS, "remark")}})
	if unused_exclusions:
		raise StatementError(f"第 {min(unused_exclusions)} 行不能排除，请核对来源收支、日期、余额和币种", "excluded_rows")
	if unused_edits:
		raise StatementError(f"第 {min(unused_edits)} 行不是可修改的流水行", "edits")
	if not result["rows"] and not result["errors"]:
		result["errors"].append({"row": start + 1, "field": "date", "message": "文件未包含交易流水"})
	if not result["errors"]:
		_finish_rows(result)
		if original:
			end = result["template"]["closing_row"]
			if end is not None and _money(end, "balance", currency=currency) != result["rows"][-1]["balance"]:
				result["errors"].append(
					{"row": closing_line, "field": "balance", "message": "期末余额与最后一笔流水不符"}
				)
	result["opening_date"] = earliest
	period = _carry_period(carry_label)
	if period and earliest:
		year, month = map(int, period.groups())
		if 1 <= month <= 12 and 1 <= year < 9999:
			result["opening_date"] = min(earliest, date(year + int(month == 12), month % 12 + 1, 1).isoformat())
	if result["review_total"] > MAX_MOVEMENTS:
		raise StatementError("单次最多导入 10,000 笔流水，请按月份拆分")
	return result


def _currency_amount(value, currency):
	prefix = "￥" if currency == "CNY" else currency + " "
	return "" if value is None else prefix + format(Decimal(str(value)), ",.2f")


def _keep_currency_format(value, currency):
	return currency == "CNY" or _str(value).lstrip().upper().startswith(currency)


def _excel_text(value):
	text = _str(value)
	if text.lstrip().startswith(("=", "+", "-", "@")) or text.startswith(("\t", "\r", "\n")):
		return "'" + text
	return text


def _export_cell(row, currency, has_remark):
	day = date.fromisoformat(str(row["date"])[:10])
	cells = [str(day.year), str(day.month), str(day.day), "\r\n".join(row.get("refs") or [])] + [""] * 10
	for i, key in TEXT_COLS.items():
		cells[i] = _excel_text(row.get(key) or "")
	for i, key in enumerate(("income", "expense", "balance"), 9):
		cells[i] = _currency_amount(row.get(key), currency)
	if has_remark:
		cells.append(_excel_text(row.get("remark") or ""))
	raw = row.get("raw_cells")
	if isinstance(raw, list) and len(raw) in (14, 15):
		if [_str(raw[i]).strip() for i in range(3)] == cells[:3]:
			cells[:3] = raw[:3]
		if _references(raw[3]) == (row.get("refs") or []):
			cells[3] = raw[3]
		for i, key in TEXT_COLS.items():
			if _str(raw[i]).strip() == _str(row.get(key)).strip():
				cells[i] = _excel_text(raw[i])
		if has_remark and len(raw) == 15 and _str(raw[14]).strip() == _str(row.get("remark")).strip():
			cells[14] = _excel_text(raw[14])
		for i, key in enumerate(("income", "expense", "balance"), 9):
			if not _keep_currency_format(raw[i], currency):
				continue
			actual = _money(row.get(key), key, currency=currency)
			try:
				previous = _money(raw[i], key, currency=currency)
			except StatementError:
				continue  # An invalid original cell may have been repaired during import.
			if previous == actual or (key != "balance" and actual is None and previous == "0.00"):
				cells[i] = raw[i]
	return cells


def export_journal(rows, *, opening, account_label, template=None, month=None, currency="CNY", ledger_book=None):
	"""Emit the CSV layout used by Excel, preserving untouched raw cell representations."""
	template = template or {}
	rows = list(rows)
	if not isinstance(currency, str) or not re.fullmatch(r"[A-Z]{3}", currency, re.ASCII):
		raise StatementError("请选择账簿币种的三位货币代码", "currency")
	has_remark = template.get("remark_column") or any(r.get("remark") for r in rows)
	headers = [*HEADERS, "备注"] if has_remark else list(HEADERS)
	if ledger_book is not None and (not isinstance(ledger_book, str) or not re.fullmatch(r"[0-9a-f]{64}", ledger_book)):
		raise StatementError("账面余额的账簿标识无效", "ledger_book")
	opening = _money(opening if opening is not None else "0", "opening", currency=currency)
	end = _money(rows[-1]["balance"], "balance", currency=currency) if rows else opening
	if end is None:
		raise StatementError("导出流水缺少期末余额", "balance")
	title = (list(template.get("title") or []) + [""] * len(headers))[:len(headers)]
	if not title[0]:
		title[0] = _excel_text(account_label)
	else:
		title[0] = _excel_text(title[0])
	title[11] = title[11] or "余额"
	if template.get("header_total") != end or not _keep_currency_format(title[12], currency):
		title[12] = end if currency == "CNY" else _currency_amount(end, currency)
	carry = (list(template.get("carry") or []) + [""] * len(headers))[:len(headers)]
	if month:
		prior = date.fromisoformat(str(month) + "-01") - timedelta(days=1)
		carry[8] = f"承{prior.year}年{prior.month}月期末余额"
	else:
		carry[8] = carry[8] or "期初余额"
	if not _keep_currency_format(carry[11], currency) or _money(carry[11], "opening", currency=currency) != opening:
		carry[11] = _currency_amount(opening, currency)
	title = [_excel_text(v) if i != 12 else v for i, v in enumerate(title)]
	if ledger_book:
		title += [""] * (15 - len(title)) + [LEDGER_MARKER + ledger_book]
	carry = [_excel_text(v) if i != 11 else v for i, v in enumerate(carry)]
	footer = [""] * len(headers)
	original_closing = template.get("closing_row")
	if _keep_currency_format(original_closing, currency) and _money(original_closing, "balance", currency=currency) == end:
		footer[11] = original_closing
	else:
		footer[11] = _currency_amount(end, currency)
	width = max(len(title), len(headers), min(MAX_COLS, int(template.get("width", 42))))
	saved_rows = max(0, min(MAX_ROWS, int(template.get("total_rows", 2225))))
	preamble = max(0, min(40, int(template.get("start_empty", 0))))
	buf = io.StringIO(newline="")
	writer = csv.writer(buf, lineterminator="\r\n")

	def write(line):
		writer.writerow(line + [""] * (width - len(line)))

	for _ in range(preamble):
		write([])
	write(title)
	write(headers)
	write(carry)
	for row in rows:
		write(_export_cell(row, currency, has_remark))
	write(footer)
	for _ in range(max(0, saved_rows - (preamble + 4 + len(rows)))):
		write([])
	text = buf.getvalue()
	return codecs.BOM_UTF8 + (text if template.get("trailing_newline") else text[:-2]).encode("utf-8")
