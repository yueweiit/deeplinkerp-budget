"""Bank statement import and the bilingual domestic bank journal CSV layout."""

from __future__ import annotations

import codecs
import csv
import hashlib
import io
import json
import re
from collections import Counter
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from zipfile import BadZipFile, ZipFile

from budget.services.bank_journal_contract import TEXT, as_date, clean_row, references

MAX_BYTES = 6 * 1024 * 1024
MAX_MOVEMENTS = 10000
MAX_ROWS = 20000
MAX_COLS = 160
MAX_CELLS = 1500000
MAX_UNPACKED = 48 * 1024 * 1024
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
ALIASES = {
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
	),
	"summary": (
		"摘要",
		"交易摘要",
		"业务摘要",
		"交易说明",
		"交易用途",
		"用途",
		"附言",
		"备注",
		"description",
		"memo",
	),
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
	),
	"amount": ("交易金额", "发生金额", "发生额", "金额", "交易金额人民币", "amount"),
	"direction": ("收支方向", "收支类型", "交易方向", "收付方向", "direction"),
	"balance": ("余额", "账户余额", "交易余额", "账面余额", "本次余额", "balance"),
	"refs": ("钉钉单号", "钉钉申请单号", "钉钉审批单号", "钉钉审批编号"),
	"currency": ("币种", "交易币种", "货币", "货币代码", "currency"),
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


class StatementError(ValueError):
	"""Friendly error suitable for showing next to a source cell."""

	def __init__(self, message, field="file"):
		super().__init__(message)
		self.field = field


class Formula(str):
	"""Excel formula cell: only mapped, literal source data may be imported."""


def _str(value):
	if value is None:
		return ""
	if isinstance(value, Formula):
		raise StatementError("公式单元格请先粘贴为值")
	return str(value)


def _money(value, field, *, title=False):
	text = _str(value).strip()
	if text in ("", "-", "—", "--"):
		return None
	text = re.sub(r"^(人民币|RMB|CNY|[¥￥])\s*", "", text, flags=re.I)
	if text.startswith("(") and text.endswith(")"):
		text = "-" + text[1:-1]
	if len(text) > 40 or not MONEY.fullmatch(text):
		raise StatementError("金额格式不正确，请使用人民币金额", field)
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
				return _limited(
					[Formula(c.value) if c.data_type == "f" else c.value for c in line]
					for line in ws.iter_rows()
				)
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


def _suggest(header):
	names = [_normal(x) for x in header]
	result = {}
	for key, aliases in ALIASES.items():
		picks = [i for i, name in enumerate(names) if name in aliases]
		if len(picks) == 1:
			result[key] = picks[0]
	return result


def _usable(mapping):
	return "date" in mapping and any(k in mapping for k in ("income", "expense", "amount"))


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


def _parse_row(raw, cols, original):
	def read(key):
		return _at(raw, cols.get(key))

	day = (
		_date("-".join(_str(read(k)).strip() for k in ("year", "month", "day")))
		if original
		else _date(read("date"))
	)
	currency = _str(read("currency")).strip().upper()
	if currency and currency not in ("人民币", "人民币元", "CNY", "RMB", "156"):
		raise StatementError("该账簿使用人民币，请核实来源币种", "currency")
	income = _money(read("income"), "income")
	expense = _money(read("expense"), "expense")
	if "amount" in cols:
		value = _money(read("amount"), "amount")
		if value is None:
			raise StatementError("交易金额为空", "amount")
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
	income, expense = _movement(income, expense)
	value = {
		"date": day,
		"income": income,
		"expense": expense,
		"balance": _money(read("balance"), "balance"),
		"refs": _references(read("refs")),
		"bank_ref": _bank_reference(read("bank_ref")),
	}
	value.update({k: _str(read(k)).strip() for k in TEXT_COLS.values()})
	try:
		clean_row(value)
	except ValueError as exc:
		raise StatementError(str(exc), "row") from exc
	if len(value["bank_ref"]) > 140:
		raise StatementError("银行流水号过长", "bank_ref")
	if original:
		value["raw_cells"] = [_str(_at(raw, i)) for i in range(14)]
	return value


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


def _append_row(result, row, row_number, occurrences, seen_ids):
	if row["bank_ref"]:
		if row["bank_ref"] in seen_ids:
			raise StatementError("同一批次中银行流水号重复，请核对该行", "bank_ref")
		seen_ids.add(row["bank_ref"])
		key = _record_key("bank-reference", row["bank_ref"])
	else:
		identity = (row["date"], row["income"], row["expense"], row["balance"], row["summary"])
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


def parse_statement(content, filename, mapping=None):
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
		opening_date=None,
		errors=[],
	)
	original = len(sheet) > first + 2 and list(sheet[first + 1][:14]) == list(HEADERS)
	start = first
	if original:
		title, header, carry = sheet[first : first + 3]
		width = len(header)
		if any(any(c not in ("", None) for c in row[14:]) for row in sheet):
			raise StatementError("原表 14 个字段后还有内容，请核对额外列")
		result["account_label"] = _str(_at(title, 0)).strip()
		result["opening"] = _money(_at(carry, 11), "opening")
		if result["opening"] is None:
			raise StatementError("期初余额缺失", "opening")
		cols = {key: index for index, key in enumerate(KEYS)}
		start = first + 3
		result["headers"] = list(HEADERS)
		result["template"] = dict(
			title=[_str(c) for c in title[:14]],
			carry=[_str(c) for c in carry[:14]],
			width=width,
			total_rows=len(sheet),
			trailing_newline=final_terminator,
			start_empty=first,
			closing_row=None,
			header_total=_money(_at(title, 12), "balance", title=True),
		)
	else:
		candidates = [(i, _suggest(row)) for i, row in enumerate(sheet[first : first + 40], first)]
		header_index, suggested = max(candidates, key=lambda x: (_usable(x[1]), len(x[1])))
		header = [_str(c) for c in sheet[header_index]]
		result.update(headers=header, suggested_mapping=suggested)
		# Bank reports may provide both the signed amount and the two direction columns.
		if mapping is None and "amount" in suggested and ("income" in suggested or "expense" in suggested):
			suggested = {k: v for k, v in suggested.items() if k not in ("amount", "direction")}
			result["suggested_mapping"] = suggested
		requested = suggested if mapping is None else mapping
		if isinstance(requested, dict) and "currency" in suggested:
			# Keep a declared transaction currency when the UI remaps other fields.
			requested = {**requested, "currency": suggested["currency"]}
		cols = _mapping(header, requested)
		if not _usable(cols):
			result["needs_mapping"] = True
			return result
		start = header_index + 1
		result["mapping_used"] = cols
	occurrences = Counter()
	seen_ids = set()
	for i, raw in enumerate(sheet[start:], start + 1):
		if not any(c not in (None, "") for c in raw):
			continue
		try:
			if original and not any(c not in ("", None) for j, c in enumerate(raw) if j != 11):
				if result["template"]["closing_row"] is not None:
					raise StatementError("发现重复的期末余额行", "balance")
				result["template"]["closing_row"] = _str(_at(raw, 11))
				continue
			if original and result["template"]["closing_row"] is not None:
				raise StatementError("期末余额行之后仍有流水", "date")
			row = _parse_row(raw, cols, original)
			_append_row(result, row, i, occurrences, seen_ids)
		except StatementError as exc:
			result["errors"].append({"row": i, "field": exc.field, "message": str(exc)})
	if not result["rows"] and not result["errors"]:
		result["errors"].append({"row": start + 1, "field": "date", "message": "文件未包含交易流水"})
	if not result["errors"]:
		_finish_rows(result)
		if original:
			carry_label = _str(_at(result["template"]["carry"], 8))
			period = re.fullmatch(r"承(\d{4})年(\d{1,2})月期末余额", carry_label.strip())
			if period:
				year, month = map(int, period.groups())
				if 1 <= month <= 12 and 1 <= year < 9999:
					candidate = date(year + int(month == 12), month % 12 + 1, 1).isoformat()
					if candidate <= result["opening_date"]:
						result["opening_date"] = candidate
		if original:
			last = result["rows"][-1].get("balance")
			end = result["template"]["closing_row"]
			if end is not None and _money(end, "balance") != last:
				result["errors"].append(
					{
						"row": start + len(result["rows"]) + 1,
						"field": "balance",
						"message": "期末余额与最后一笔流水不符",
					}
				)
	if len(result["rows"]) > MAX_MOVEMENTS:
		raise StatementError("单次最多导入 10,000 笔流水，请按月份拆分")
	return result


def _rmb(value):
	return "" if value is None else "￥" + format(Decimal(str(value)), ",.2f")


def _excel_text(value):
	text = _str(value)
	if text.lstrip().startswith(("=", "+", "-", "@")) or text.startswith(("\t", "\r", "\n")):
		return "'" + text
	return text


def _export_cell(row):
	day = date.fromisoformat(str(row["date"])[:10])
	cells = [str(day.year), str(day.month), str(day.day), "\r\n".join(row.get("refs") or [])] + [""] * 10
	for i, key in TEXT_COLS.items():
		cells[i] = _excel_text(row.get(key) or "")
	for i, key in enumerate(("income", "expense", "balance"), 9):
		cells[i] = _rmb(row.get(key))
	raw = row.get("raw_cells")
	if isinstance(raw, list) and len(raw) == 14:
		if [_str(raw[i]).strip() for i in range(3)] == cells[:3]:
			cells[:3] = raw[:3]
		if _references(raw[3]) == (row.get("refs") or []):
			cells[3] = raw[3]
		for i, key in TEXT_COLS.items():
			if _str(raw[i]).strip() == _str(row.get(key)).strip():
				cells[i] = _excel_text(raw[i])
		for i, key in enumerate(("income", "expense", "balance"), 9):
			actual = _money(row.get(key), key)
			previous = _money(raw[i], key)
			if previous == actual or (key != "balance" and actual is None and previous == "0.00"):
				cells[i] = raw[i]
	return cells


def export_journal(rows, *, opening, account_label, template=None, month=None):
	"""Emit the CSV layout used by Excel, preserving untouched raw cell representations."""
	template = template or {}
	rows = list(rows)
	opening = _money(opening if opening is not None else "0", "opening")
	end = _money(rows[-1]["balance"], "balance") if rows else opening
	if end is None:
		raise StatementError("导出流水缺少期末余额", "balance")
	title = (list(template.get("title") or []) + [""] * 14)[:14]
	if not title[0]:
		title[0] = _excel_text(account_label)
	else:
		title[0] = _excel_text(title[0])
	title[11] = title[11] or "余额"
	if template.get("header_total") != end:
		title[12] = end
	carry = (list(template.get("carry") or []) + [""] * 14)[:14]
	if month:
		prior = date.fromisoformat(str(month) + "-01") - timedelta(days=1)
		carry[8] = f"承{prior.year}年{prior.month}月期末余额"
	else:
		carry[8] = carry[8] or "期初余额"
	if _money(carry[11], "opening") != opening:
		carry[11] = _rmb(opening)
	title = [_excel_text(v) if i != 12 else v for i, v in enumerate(title)]
	carry = [_excel_text(v) if i != 11 else v for i, v in enumerate(carry)]
	footer = [""] * 14
	original_closing = template.get("closing_row")
	footer[11] = original_closing if _money(original_closing, "balance") == end else _rmb(end)
	width = max(14, min(MAX_COLS, int(template.get("width", 42))))
	saved_rows = max(0, min(MAX_ROWS, int(template.get("total_rows", 2225))))
	preamble = max(0, min(40, int(template.get("start_empty", 0))))
	buf = io.StringIO(newline="")
	writer = csv.writer(buf, lineterminator="\r\n")

	def write(line):
		writer.writerow(line + [""] * (width - len(line)))

	for _ in range(preamble):
		write([])
	write(title)
	write(list(HEADERS))
	write(carry)
	for row in rows:
		write(_export_cell(row))
	write(footer)
	for _ in range(max(0, saved_rows - (preamble + 4 + len(rows)))):
		write([])
	text = buf.getvalue()
	return codecs.BOM_UTF8 + (text if template.get("trailing_newline") else text[:-2]).encode("utf-8")
