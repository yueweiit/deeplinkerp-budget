"""Use a model to choose source columns from a bank statement's label structure."""

from __future__ import annotations

import json
import re

from . import bank_journal_io as formats

# Only these labels can leave ERP. A label containing a name, a value or an
# identifier contributes the recognized banking terms, never its source text.
TERMS = {
	term for aliases in formats.ALIASES.values() for term in aliases
} | {
	"过账", "记账", "记帐", "入账", "日期", "流水", "余款", "收入", "支出",
	"借", "贷", "转入", "转出", "流入", "流出", "支取", "存取", "收付", "收款", "付款",
	"借记", "贷记", "发生", "发生额", "正负", "净额", "往来", "账户", "金额", "业务", "说明", "币别",
	"余额", "结余", "月", "年", "日", "收", "付", "方向", "汇入", "汇出", "备注", "附言", "用途",
	"transaction", "posting", "booked", "entry", "date", "credit", "debit", "paid", "received", "withdrawal",
	"deposit", "signed", "amount", "balance", "description", "reference", "currency", "sequence",
	"fecha", "abono", "cargo", "debito", "débito", "credito", "crédito", "importe", "monto",
	"saldo", "referencia", "descripcion", "descripción", "concepto", "moneda", "retiro", "deposito", "depósito",
}
LABEL = re.compile("|".join(re.escape(term) for term in sorted(TERMS, key=lambda term: (-len(term), term))))

PROMPT = """你负责识别银行流水的表头。下面是候选行的列序号和银行术语，均为脱敏结构。
识别可能的表头和列对应，严格返回 JSON，例如
{"header_row":2,"columns":{"date":0,"income":1,"expense":2,"balance":3,"summary":4}}。
header_row 使用候选中的原行号，columns 的值为给定的零起始列序号。
可用字段为 date、income、expense、amount、direction、balance、summary、bank_ref、refs、currency。
银行账户的贷记是流入，借记是流出。分开的收入和支出使用 income/expense。
明确只有一列交易金额时使用 amount，有单独收支方向时给出 direction。
有明确余额、交易流水号或币种时保留其对应列。不能确定日期或金额列时返回空 JSON 对象。"""


def request_layout(candidates):
	try:
		from ai_assistant.ai_assistant import model_client
	except ImportError:
		raise formats.StatementError("请在 Deeplinkerp 设置中配置 DeepSeek 后重试。") from None
	try:
		return model_client.request_deepseek_json(
			model_client.get_deepseek_config(),
			[
				{"role": "system", "content": PROMPT},
				{"role": "user", "content": json.dumps(candidates, ensure_ascii=False)},
			],
		)
	except model_client.DeepSeekError as exc:
		raise formats.StatementError(str(exc)) from None


def infer_layout(sheet, first):
	candidates = []
	for index, row in enumerate(sheet[first : first + formats.HEADER_SCAN], first + 1):
		labels = {}
		for column, value in enumerate(row):
			if not isinstance(value, str) or isinstance(value, formats.Formula) or len(value) > 80:
				continue
			terms = list(dict.fromkeys(LABEL.findall(formats._normal(value))))[:6]
			if terms:
				labels[column] = terms
		if len(labels) >= 2:
			candidates.append({"header_row": index, "columns": labels})
	candidates = sorted(candidates, key=lambda item: -len(item["columns"]))[:6]
	if not candidates:
		return None

	result = request_layout(candidates)
	row = result.get("header_row")
	columns = result.get("columns")
	candidate = next((c for c in candidates if c["header_row"] == row), None)
	if (
		type(row) is not int
		or not candidate
		or not isinstance(columns, dict)
		or any(type(column) is not int or column not in candidate["columns"] for column in columns.values())
	):
		return None
	return row, columns
