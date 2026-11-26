"""Wire values for the RMB journal; all monetary arithmetic stays in decimal."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import date
from decimal import Decimal, InvalidOperation

TEXT = ("category", "level2", "level3", "project", "summary", "invoice", "dept")
SOURCE = {"oa": "DingTalk", "dingtalk": "DingTalk", "import": "Import"}
CENT = Decimal("0.01")


def digest(value):
	return hashlib.sha256(
		json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()
	).hexdigest()


def as_date(value):
	if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
		raise ValueError("请使用 YYYY-MM-DD 日期。")
	try:
		return date.fromisoformat(value)
	except ValueError:
		raise ValueError("日期无效。") from None


def month_bounds(month, today):
	month = str(today)[:7] if month is None or month == "" else month
	if not isinstance(month, str) or not re.fullmatch(r"\d{4}-(?:0[1-9]|1[012])", month):
		raise ValueError("月份无效。")
	start = as_date(month + "-01")
	end = date(start.year + int(start.month == 12), start.month % 12 + 1, 1)
	return month, start, end


def money(value, *, positive=False):
	if isinstance(value, bool) or not isinstance(value, (str, int, Decimal, float)):
		raise ValueError("请填写有效人民币金额。")
	try:
		n = Decimal(str(value))
		if not n.is_finite() or abs(n) >= Decimal("10000000000000") or n != n.quantize(CENT):
			raise ValueError
		if positive and n <= 0:
			raise ValueError
	except (InvalidOperation, ValueError) as exc:
		raise ValueError("金额须精确到分，流水金额必须大于零。") from exc
	return format(n, ".2f")


def references(value):
	if value is None or value == "":
		return []
	if isinstance(value, str):
		value = [r for r in re.split(r"[\s/、,，;；]+", value.strip()) if r]
	if not isinstance(value, list) or len(value) > 40:
		raise ValueError("钉钉单号格式不正确。")
	result = []
	for ref in value:
		if not isinstance(ref, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", ref.strip()):
			raise ValueError("钉钉单号应为完整的文本编号。")
		if ref.strip() not in result:
			result.append(ref.strip())
	return result


def clean_row(row):
	if not isinstance(row, dict):
		raise ValueError("流水格式无效。")
	out = {"date": as_date(row.get("date")).isoformat(), "refs": references(row.get("refs"))}
	for field in TEXT:
		value = row.get(field)
		value = "" if value is None else value
		if not isinstance(value, str) or len(value) > 12000 or "\x00" in value:
			raise ValueError("流水文本过长或包含无效字符。")
		out[field] = value
	for field in ("income", "expense"):
		out[field] = None if row.get(field) in (None, "") else money(row[field], positive=True)
	if (out["income"] is None) == (out["expense"] is None):
		raise ValueError("每行请选择一项收入或支出。")
	return out


def batch_digest(row):
	return digest({k: row.get(k) for k in ("date", "income", "expense", "summary", "refs", "bank_ref")})
