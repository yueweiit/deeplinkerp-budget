import csv
import io
from datetime import date

import pytest

from budget.services import bank_journal_io as journal


def csv_bytes(rows, width=None):
	out = io.StringIO(newline="")
	writer = csv.writer(out, lineterminator="\r\n")
	for row in rows:
		writer.writerow(row + [""] * max(0, (width or 0) - len(row)))
	return out.getvalue()[:-2].encode("utf-8-sig")


def sample_journal():
	return csv_bytes(
		[
			["示例银行"] + [""] * 10 + ["余额", "1003.300000000001"],
			list(journal.HEADERS),
			[""] * 8 + ["承2025年12月期末余额", "", "", "￥1,000.00"],
			["2026", "11", "1", "", "收入", "", "", "", "汇入", "￥10.00", "", "￥1,010.00"],
			[
				"2026",
				"11",
				"2",
				"200000000000000000001\r/200000000000000000002\r",
				"费用",
				"",
				"",
				"",
				"支付\r费用",
				"",
				"6.70",
				"￥1,003.30",
				"",
				"行政",
			],
			[""] * 11 + ["￥1,003.30"],
			[],
			[],
		],
		width=42,
	)


def test_exact_template_round_trip_and_changed_cells():
	raw = sample_journal()
	result = journal.parse_statement(raw, "银行.csv")
	assert result["errors"] == [] and result["needs_mapping"] is False
	rows = result["rows"]
	assert len(rows) == 2 and result["opening"] == "1000.00"
	assert rows[1]["refs"] == ["200000000000000000001", "200000000000000000002"]
	kwargs = dict(
		opening=result["opening"], account_label=result["account_label"], template=result["template"]
	)
	assert journal.export_journal(rows, **kwargs) == raw
	rows[1]["summary"] = "核准的新摘要"
	assert journal.export_journal(rows, **kwargs) == raw.replace(
		"支付\r费用".encode(), "核准的新摘要".encode()
	).replace(b'"' + "核准的新摘要".encode() + b'"', "核准的新摘要".encode())


def test_changed_amounts_and_monthly_carry_forward():
	data = journal.parse_statement(sample_journal(), "银行.csv")
	row = dict(data["rows"][1], expense="8.70", balance="1001.30")
	export = journal.export_journal(
		[row], opening="1010.00", account_label="示例银行", template=data["template"], month="2026-11"
	)
	read = list(csv.reader(io.StringIO(export.decode("utf-8-sig"), newline="")))
	assert read[0][12] == "1001.30"
	assert read[2][11] == "￥1,010.00" and "10月" in read[2][8]
	assert read[3][10:12] == ["￥8.70", "￥1,001.30"]
	assert len(read) == 8 and all(len(r) == 42 for r in read)


def test_native_template_repair_keeps_original_cell_evidence_and_exports_corrected_amount():
	raw = sample_journal().replace(b"6.70", b"invalid")
	bad = journal.parse_statement(raw, "银行.csv", review_offset=0)
	assert bad["review_rows"][1]["expense"] == "invalid"
	assert bad["errors"][0]["row"] == 5
	parsed = journal.parse_statement(raw, "银行.csv", edits={5: {"income": "", "expense": "6.70"}})
	assert not parsed["errors"]
	assert parsed["rows"][1]["raw_cells"][10] == "invalid"
	output = journal.export_journal(
		parsed["rows"], opening=parsed["opening"], account_label=parsed["account_label"], template=parsed["template"]
	)
	assert output == raw.replace(b"invalid", "￥6.70".encode())
	with pytest.raises(journal.StatementError, match="不是可修改的流水行"):
		journal.parse_statement(raw, "银行.csv", edits={6: {"balance": "2000"}})


def test_generic_overlapping_files_and_repeat_occurrences():
	header = ["交易日期", "摘要", "收入金额", "支出金额"]
	a = ["20261103", "示例", "0.00", "10.25"]
	result = journal.parse_statement(csv_bytes([header, a, a]), "流水.csv")
	assert not result["errors"] and not result["needs_mapping"]
	assert result["rows"][0]["date"] == "2026-11-03"
	assert result["rows"][0]["expense"] == "10.25" and result["rows"][0]["income"] is None
	keys = [row["source_key"] for row in result["rows"]]
	assert len(set(keys)) == 2
	assert [
		row["source_key"] for row in journal.parse_statement(csv_bytes([header, a, a]), "复制.csv")["rows"]
	] == keys


def test_unknown_mapping_signed_bank_credit_debit_gb18030():
	data = "银行交易日,附言,有正负的金额,银行自编号\r\n2026/11/05,采购,-2.45,K4"
	result = journal.parse_statement(data.encode("gb18030"), "文件.csv")
	assert result["needs_mapping"] and len(result["headers"]) == 4
	mapped = journal.parse_statement(
		data.encode("gb18030"), "文件.csv", {"date": 0, "summary": 1, "amount": 2, "bank_ref": 3}
	)
	assert not mapped["errors"] and mapped["rows"][0]["expense"] == "2.45"
	assert mapped["rows"][0]["bank_ref"] == "K4"


def test_row_errors_do_not_misstate_money_or_dates():
	result = journal.parse_statement(
		csv_bytes(
			[
				["日期", "摘要", "交易金额"],
				["2026-02-30", "A", "10"],
				["2026-11-02", "B", "1,2"],
				["2026-11-03", "C", "0.003"],
			]
		),
		"流水.csv",
	)
	assert not result["needs_mapping"] and not result["rows"]
	assert [item["row"] for item in result["errors"]] == [2, 3, 4]


def test_descending_statement_and_explicit_opening():
	result = journal.parse_statement(
		csv_bytes(
			[
				["交易日期", "收入", "支出", "余额"],
				["2026-11-03", "3", "", "13"],
				["2026-11-02", "10", "", "10"],
			]
		),
		"流水.csv",
	)
	assert [row["balance"] for row in result["rows"]] == ["10.00", "13.00"]
	assert result["opening"] == "0.00"


def test_xlsx_real_dates_text_id_and_ambiguous_numeric_id():
	from openpyxl import Workbook

	w = Workbook()
	s = w.active
	s.append(["交易日期", "摘要", "支出", "钉钉单号"])
	s.append([date(2026, 11, 16), "测试", 12.25, "200000000000000000001"])
	s.append([date(2026, 11, 16), "测试", 3, 200000000000000000002])
	buf = io.BytesIO()
	w.save(buf)
	result = journal.parse_statement(buf.getvalue(), "流水.xlsx")
	assert result["rows"][0]["refs"] == ["200000000000000000001"]
	assert result["errors"][0]["field"] == "refs" and result["errors"][0]["row"] == 3


def test_export_finance_text_cannot_become_excel_formula():
	result = journal.parse_statement(sample_journal(), "模板.csv")
	row = dict(result["rows"][1], summary='=HYPERLINK("https://example.org")')
	output = journal.export_journal(
		[row], opening="1010.00", account_label="示例银行", template=result["template"]
	)
	fields = list(csv.reader(io.StringIO(output.decode("utf-8-sig"), newline="")))
	assert fields[3][8].startswith("'=HYPERLINK")


def test_duplicate_bank_id_and_bad_running_balance_are_visible():
	result = journal.parse_statement(
		csv_bytes(
			[
				["交易日期", "摘要", "收入", "余额", "交易流水号"],
				["20261101", "1", "1", "21", "001"],
				["20261101", "2", "1", "25", "001"],
			]
		),
		"流水.csv",
	)
	assert result["errors"]


def test_explicit_carry_date_and_generic_currency_gates():
	result = journal.parse_statement(sample_journal(), "模板.csv")
	assert result["opening_date"] == "2026-01-01"
	rows = journal.parse_statement(
		csv_bytes([["日期", "收入", "币种"], ["20261101", "3.00", "美元"]]), "bank.csv"
	)
	assert not rows["rows"] and rows["errors"][0]["field"] == "currency"


def test_explicit_text_refs_support_valid_non_numeric_instances_but_no_excel_scientific():
	rows = [["日期", "支出", "钉钉单号"], ["20261101", "3", "PAY-001"], ["20261102", "2", "2.00000E+20"]]
	data = journal.parse_statement(csv_bytes(rows), "a.csv")
	assert data["rows"][0]["refs"] == ["PAY-001"] and data["errors"][0]["field"] == "refs"


def test_template_opening_and_footer_do_not_propagate_formula_text():
	data = journal.parse_statement(sample_journal(), "example.csv")
	data["template"]["title"][3] = "=cmd"
	data["template"]["carry"][8] = "@demo"
	rows = list(
		csv.reader(
			io.StringIO(
				journal.export_journal(
					data["rows"],
					opening=data["opening"],
					account_label=data["account_label"],
					template=data["template"],
				).decode("utf-8-sig"),
				newline="",
			)
		)
	)
	assert rows[0][3] == "'=cmd" and rows[2][8] == "'@demo"


def test_overlapping_weak_identical_movements_carry_file_proof_but_strong_rows_remain_portable():
	header = ["日期", "摘要", "支出", "余额", "交易流水号"]
	weak = ["2026-11-02", "银行手续费", "6.00", "", ""]
	strong = ["2026-11-03", "付款", "2.00", "", "C-100"]
	both = journal.parse_statement(csv_bytes([header, weak, strong]), "A.csv")["rows"]
	only = journal.parse_statement(csv_bytes([header, weak]), "B.csv")["rows"]
	repeated = journal.parse_statement(csv_bytes([header, strong, weak]), "重排.csv")["rows"]
	assert only[0]["source_key"] == both[0]["source_key"]
	assert only[0]["weak_file_proof"] != both[0]["weak_file_proof"]
	assert repeated[0]["weak_file_proof"] == both[0]["weak_file_proof"]
	assert "weak_file_proof" not in both[1]


def test_known_foreign_currency_is_still_checked_when_mapping_bank_columns():
	content = csv_bytes([["交易日期", "对方摘要", "支出", "交易币种"], ["20261101", "测试", "1", "USD"]])
	preview = journal.parse_statement(content, "bank.csv", {"date": 0, "summary": 1, "expense": 2})
	assert not preview["rows"] and preview["errors"][0]["field"] == "currency"


def test_excel_numeric_long_bank_reference_is_not_a_safe_identity():
	from openpyxl import Workbook

	wb = Workbook()
	ws = wb.active
	ws.append(["交易日期", "收入", "银行流水号"])
	ws.append(["20261101", 3, 123456789012345678901])
	stream = io.BytesIO()
	wb.save(stream)
	res = journal.parse_statement(stream.getvalue(), "流水.xlsx")
	assert not res["rows"] and res["errors"][0]["field"] == "bank_ref"


def test_structured_rows_share_bank_and_weak_identity_with_statement():
	fields = ["交易日期", "交易流水号", "收入", "摘要"]
	file_rows = journal.parse_statement(
		csv_bytes([fields, ["2026-11-01", "0019", "12.30", "进款"], ["2026-11-02", "", "1.50", "结息"]]),
		"示例.csv",
	)
	data = journal.parse_rows(
		[
			{"date": "2026-11-01", "bank_ref": "0019", "income": "12.30", "summary": "进款"},
			{"date": "2026-11-02", "income": "1.5", "summary": "结息"},
		]
	)
	assert data["errors"] == []
	for expected, actual in zip(file_rows["rows"], data["rows"], strict=True):
		assert {k: v for k, v in expected.items() if k != "source_row"} == {
			k: v for k, v in actual.items() if k != "source_row"
		}
	assert [r["source_row"] for r in data["rows"]] == [1, 2]


@pytest.mark.parametrize(
	("patch", "field"),
	[
		({"income": 1}, "income"),
		({"income": True}, "income"),
		({"income": "1.001"}, "income"),
		({"expense": "1"}, "amount"),
		({"income": "0.00"}, "income"),
		({"income": "1e2"}, "income"),
		({"balance": -2}, "balance"),
		({"date": "20261116"}, "date"),
		({"date": "2026-02-30"}, "date"),
		({"refs": "00123"}, "refs"),
		({"refs": [123]}, "refs"),
		({"refs": ["1E19"]}, "refs"),
		({"bank_ref": 123}, "bank_ref"),
		({"bank_ref": "100E5"}, "bank_ref"),
		({"links": {}}, "links"),
		({"summary": None}, "summary"),
	],
)
def test_structured_errors_identify_original_row_and_field(patch, field):
	valid = {"date": "2026-11-16", "income": "1.00"}
	data = journal.parse_rows([valid, {**valid, **patch}])
	assert len(data["errors"]) == 1
	assert (data["errors"][0]["row"], data["errors"][0]["field"]) == (2, field)


def test_structured_duplicates_and_balances():
	rows = [
		{"date": "2026-11-02", "expense": "1", "balance": "-1.50", "bank_ref": "b"},
		{"date": "2026-11-01", "expense": "2", "balance": "-0.50", "bank_ref": "a"},
	]
	valid = journal.parse_rows(rows)
	assert valid["errors"] == [] and valid["opening"] == "1.50" and valid["opening_date"] == "2026-11-01"
	assert [r["source_row"] for r in valid["rows"]] == [2, 1]
	assert journal.parse_rows([rows[0], rows[0]])["errors"][0]["field"] == "bank_ref"
	assert journal.parse_rows([dict(rows[0], balance="2.22"), rows[1]])["errors"][0]["row"] == 1


def split_journal(*, formula="=L3+J4-K4", cached=None):
	from zipfile import ZipFile

	from openpyxl import Workbook

	workbook = Workbook()
	sheet = workbook.active
	sheet.append(["Private Account 001234567890"])
	sheet.append([*journal.HEADERS[:13], "备注"])
	sheet.append([""] * 8 + ["期初余额", None, None, 0, None, "收款店铺名"])
	sheet.append([2026, 10, 5, "PAY-00001", "收入", "货款", "其他", "一", "第一笔", 12.31, None, formula, None, "独立备注"])
	sheet.append([2026, 10, 6, None, None, None, None, None, "第二笔", None, 2, "=L4+J5-K5"])
	sheet.append([2026, 10, 7, None, None, None, None, None, "待核一", None, None, "=L5+J6-K6"])
	sheet.append([2026, 10, 7, None, None, None, None, None, "待核二"])
	out = io.BytesIO()
	workbook.save(out)
	if cached is None:
		return out.getvalue()
	outbuffer = io.BytesIO()
	with ZipFile(out) as source, ZipFile(outbuffer, "w") as target:
		for name in source.namelist():
			data = source.read(name)
			if name == "xl/worksheets/sheet1.xml":
				data = data.replace(b"</f><v></v>", ("</f><v>" + cached + "</v>").encode(), 1)
				if cached.startswith("#"):
					data = data.replace(b'<c r="L4">', b'<c r="L4" t="e">')
			target.writestr(name, data)
	return outbuffer.getvalue()


def test_ai_split_mapping_keeps_independent_fields_and_replays_without_model(monkeypatch):
	from budget.services import bank_journal_ai

	calls = []
	def model(candidates):
		calls.append(candidates)
		assert [c["header_row"] for c in candidates] == [2]
		for value in ("001234567890", "PAY-00001", "独立备注", "第一笔", "12.31"):
			assert value not in str(candidates)
		return {"header_row": 2, "columns": {"year": 0, "month": 1, "day": 2, "income": 9,
			"expense": 10, "balance": 11, "dept": 13}}
	monkeypatch.setattr(bank_journal_ai, "request_layout", model)
	raw = split_journal()
	parsed = journal.parse_statement(raw, "bank.xlsx", currency="MXN", review_offset=0, suggest_layout=bank_journal_ai.infer_layout)
	assert len(calls) == 1 and parsed["mapping_source"] == "deepseek"
	assert parsed["header_row"] == 2 and parsed["mapping_used"]["remark"] == 13
	assert "dept" not in parsed["mapping_used"] and "date" not in parsed["mapping_used"]
	first = parsed["rows"][0]
	assert (first["date"], first["category"], first["level2"], first["level3"], first["project"]) == (
		"2026-10-05", "收入", "货款", "其他", "一")
	assert (first["summary"], first["dept"], first["remark"], first["refs"]) == ("第一笔", "", "独立备注", ["PAY-00001"])
	assert (parsed["opening"], parsed["opening_date"], parsed["suggested_currency"]) == ("0.00", "2026-10-05", None)
	assert parsed["review_total"] == 4 and [r["row"] for r in parsed["errors"]] == [6, 7]
	assert [r["excludable"] for r in parsed["review_rows"]] == [False, False, True, True]
	assert [r["balance"] for r in parsed["review_rows"]] == ["12.31", "10.31", "10.31", ""]
	fixed = journal.parse_statement(raw, "bank.xlsx", parsed["mapping_used"], header_row=2, currency="MXN",
		review_offset=0, edits={7: {"income": "", "expense": "0.31"}}, excluded_rows=[6])
	assert not fixed["errors"] and fixed["rows"][-1]["expense"] == "0.31"
	assert [r["excluded"] for r in fixed["review_rows"]] == [False, False, True, False]
	assert fixed["review_rows"][0]["remark"] == "独立备注" and fixed["review_total"] == 4


def test_unknown_split_labels_can_be_inferred_and_invalid_ai_falls_back(monkeypatch):
	from budget.services import bank_journal_ai

	file = csv_bytes([["accounting year", "posting month", "calendar day", "received funds", "paid funds"],
					  ["2026", "9", "1", "8", ""]])
	monkeypatch.setattr(bank_journal_ai, "request_layout", lambda c: {
		"header_row": 1, "columns": {"year": 0, "month": 1, "day": 2, "income": 3, "expense": 4}})
	parsed = journal.parse_statement(file, "bank.csv", suggest_layout=bank_journal_ai.infer_layout)
	assert parsed["rows"][0]["date"] == "2026-09-01" and parsed["mapping_source"] == "deepseek"
	monkeypatch.setattr(bank_journal_ai, "request_layout", lambda c: {"header_row": 2, "columns": {"year": 99}})
	fallback = journal.parse_statement(split_journal(), "bank.xlsx", suggest_layout=bank_journal_ai.infer_layout)
	assert len(fallback["rows"]) == 2 and fallback["mapping_source"] != "deepseek"


@pytest.mark.parametrize(("formula", "cached"), [
	("=L3+J4-K4", "99.00"), ("=L3+J4-K6", "12.31"), ("=SUM(J4)", "12.31"), ("=L3+J4-K4", "#VALUE!")
])
def test_unverified_excel_balance_blocks_even_if_review_edits_replace_it(formula, cached):
	file = split_journal(formula=formula, cached=cached)
	for edits in (None, {4: {"balance": "12.31"}}):
		result = journal.parse_statement(file, "bank.xlsx", currency="MXN", edits=edits, review_offset=0)
		assert any(x["row"] == 4 and x["field"] == "balance" for x in result["errors"])
	assert not result["review_rows"][0]["excludable"]


def test_balance_exclusions_require_blank_source_amounts_and_no_source_conflict():
	raw = csv_bytes([["日期", "收入", "支出", "余额", "币种", "交易流水号"],
		["20261101", "20", "", "20", "MXN"], ["20261102", "", "", "20", "MXN"],
		["20261102", "", "", "", "MXN"], ["20261102", "", "", "22", "MXN"],
		["20261102", "", "", "", "USD"], ["20261135", "", "", "", "MXN"],
		["20261103", "0", "", "", "MXN"], ["20261103", "", "", "", "MXN", "valid-bank-id"]])
	result = journal.parse_statement(raw, "bank.csv", currency="MXN", review_offset=0)
	assert [r["source_row"] for r in result["review_rows"] if r["excludable"]] == [3, 4]
	for invalid in (1, 2, 5, 6, 7, 8, 9, 45):
		with pytest.raises(journal.StatementError, match="排除"):
			journal.parse_statement(raw, "bank.csv", currency="MXN", excluded_rows=[invalid])
	with pytest.raises(journal.StatementError, match="排除"):
		journal.parse_statement(raw, "bank.csv", currency="MXN", excluded_rows=[3], edits={3: {"summary": "变更"}})
	assert journal.parse_statement(raw, "bank.csv", currency="MXN", edits={2: {"balance": ""}})["errors"][0]["field"] == "balance"


def test_currency_evidence_survives_remaps_and_edits_and_dollar_needs_a_choice():
	raw = csv_bytes([["币种", "MXN"], ["日期", "收入", "余额"], ["20261001", "MXN 2.00", "$ 2.00"]])
	data = journal.parse_statement(raw, "bank.csv", currency=None)
	assert data["suggested_currency"] == "MXN" and not data["errors"] and data["rows"][0]["balance"] == "2.00"
	assert journal.parse_statement(raw, "bank.csv", currency="USD")["errors"]
	raw = csv_bytes([["日期", "收入", "余额"], ["20261001", "$2", "$2"]])
	assert journal.parse_statement(raw, "bank.csv", currency=None)["suggested_currency"] is None
	assert not journal.parse_statement(raw, "bank.csv", currency="MXN")["errors"]
	assert journal.parse_statement(raw, "bank.csv", currency="CNY")["errors"]
	raw = csv_bytes([["日期", "收入（USD）", "支出", "币种", "余额"], ["20261001", "2.00", "", "USD", "2"]])
	data = journal.parse_statement(raw, "bank.csv", {"date": 0, "income": 1}, currency="MXN",
		edits={2: {"income": "2", "expense": ""}})
	assert data["suggested_currency"] == "USD" and not data["rows"] and data["errors"][0]["field"] == "currency"
	raw = csv_bytes([["日期", "收入", "币种"], ["20261001", "3", "美元"], ["20261002", "4", "墨西哥比索"]])
	data = journal.parse_statement(raw, "bank.csv", currency=None)
	assert data["suggested_currency"] is None and any(i["field"] == "currency" for i in data["errors"])


def test_non_cny_export_round_trip_preserves_currency_and_separate_notes():
	row = {"date": "2026-11-06", "income": "120.30", "balance": "120.30", "dept": "销售", "remark": "收款门店"}
	exported = journal.export_journal([row], opening="0", account_label="银行", currency="MXN")
	assert "￥".encode() not in exported and b"MXN 120.30" in exported
	parsed = journal.parse_statement(exported, "export.csv", currency="MXN")
	assert parsed["errors"] == [] and parsed["suggested_currency"] == "MXN" and parsed["opening"] == "0.00"
	assert (parsed["rows"][0]["dept"], parsed["rows"][0]["remark"]) == ("销售", "收款门店")
	assert journal.export_journal(parsed["rows"], opening=parsed["opening"], account_label=parsed["account_label"],
		currency="MXN", template=parsed["template"]) == exported


def test_remapping_cannot_hide_raw_amount_currency_or_let_a_payment_be_excluded():
	rows = [["日期", "收入", "支出（USD）", "其他"], ["20261101", "2", "", ""], ["20261102", "", "4", ""]]
	mapping = {"date": 0, "income": 1}
	raw = csv_bytes(rows)
	with pytest.raises(journal.StatementError, match="排除"):
		journal.parse_statement(raw, "bank.csv", mapping, currency="USD", excluded_rows=[3])
	rows[0][2] = "支出"
	rows[2][2] = "USD 4"
	data = journal.parse_statement(csv_bytes(rows), "bank.csv", mapping, currency="MXN",
		edits={3: {"income": "8", "expense": ""}})
	assert any(i["row"] == 3 and i["field"] == "currency" for i in data["errors"])
	rows[0] = ["日期", "收入", "币种", "币种"]
	rows[1] = ["20261101", "2", "MXN", "USD"]
	data = journal.parse_statement(csv_bytes(rows[:2]), "bank.csv", mapping, currency="MXN")
	assert data["errors"][0]["field"] == "currency"


def test_excel_currency_formats_keep_explicit_codes_distinct_from_locale_and_dollar():
	from openpyxl import Workbook

	def with_format(fmt):
		book = Workbook()
		book.active.append(["日期", "收入", "余额"])
		book.active.append(["20261101", 2, 2])
		book.active["B2"].number_format = fmt
		out = io.BytesIO()
		book.save(out)
		return out.getvalue()
	for fmt in ('[$MXN-80A]#,##0.00', '"MXN" #,##0.00'):
		raw = with_format(fmt)
		assert journal.parse_statement(raw, "bank.xlsx", currency=None)["suggested_currency"] == "MXN"
		assert journal.parse_statement(raw, "bank.xlsx", currency="USD")["errors"][0]["field"] == "currency"
	for fmt in ('[$$-409]#,##0.00', r'\$#,##0.00'):
		raw = with_format(fmt)
		assert journal.parse_statement(raw, "bank.xlsx", currency=None)["suggested_currency"] is None
		assert not journal.parse_statement(raw, "bank.xlsx", currency="MXN")["errors"]
		assert journal.parse_statement(raw, "bank.xlsx", currency="CNY")["errors"][0]["field"] == "currency"


def test_arbitrary_mapped_formula_cannot_be_hidden_by_an_edit():
	from openpyxl import Workbook

	book = Workbook()
	book.active.append(["日期", "收入"])
	book.active.append(["20261101", "=1+8"])
	out = io.BytesIO()
	book.save(out)
	data = journal.parse_statement(out.getvalue(), "bank.xlsx", edits={2: {"income": "9", "expense": ""}})
	assert not data["rows"] and data["errors"][0]["field"] == "income"


def test_foreign_export_identifies_currency_even_when_original_template_only_had_dollars():
	raw = sample_journal().replace("￥".encode(), b"$")
	first = journal.parse_statement(raw, "example.csv", currency="MXN")
	assert not first["errors"] and first["suggested_currency"] is None
	data = journal.export_journal(first["rows"], opening=first["opening"], account_label=first["account_label"],
		template=first["template"], currency="MXN")
	assert b"$" not in data and b"MXN 1,010.00" in data
	copy = journal.parse_statement(data, "export.csv", currency=None)
	assert not copy["errors"] and copy["suggested_currency"] == "MXN"


def test_native_looking_header_without_carry_row_keeps_first_transaction():
	header = list(journal.HEADERS)
	raw = csv_bytes([["银行流水"], header,
		["2026", "6", "5", "", "", "", "", "", "入账", "4.00", "", "4.00"],
		["2026", "6", "9", "", "", "", "", "", "支出", "", "1.50", "2.50"]])
	res = journal.parse_statement(raw, "bank.csv", currency="MXN")
	assert not res["errors"] and len(res["rows"]) == 2 and res["opening"] == "0.00"


def test_disclosed_money_in_a_native_looking_extra_column_is_checked():
	raw = csv_bytes([["银行流水"], [*journal.HEADERS, "交易币种"],
		[""] * 8 + ["期初余额", "", "", "0.00"],
		["2026", "6", "5", "", "", "", "", "", "入账", "4.00", "", "4.00", "", "", "MXN"]])
	res = journal.parse_statement(raw, "bank.csv", currency="MXN")
	assert not res["errors"] and res["suggested_currency"] == "MXN" and res["rows"][0]["income"] == "4.00"
	assert journal.parse_statement(raw, "bank.csv", currency="USD")["errors"][0]["field"] == "currency"


@pytest.mark.parametrize("label", ["Deposit", "Depósitos", "Credit", "存款金额"])
def test_common_receipt_label_next_to_withdrawals_is_imported_and_cannot_be_hidden(label):
	raw = csv_bytes([["日期", "支出", label, "备注"], ["20261101", "3", "", "1"],
		["20261102", "", "10.50", "2"]])
	good = journal.parse_statement(raw, "example.csv")
	assert not good["errors"] and good["rows"][1]["income"] == "10.50"
	for mapping in ({"date": 0, "expense": 1}, {"date": 0, "expense": 1, "summary": 2},
					{"date": 0, "expense": 1, "balance": 2}):
		view = journal.parse_statement(raw, "example.csv", mapping, review_offset=0, edits={3: {"income": "1", "expense": ""}})
		assert any(x["row"] == 3 and x["field"] == "mapping" for x in view["errors"])
		assert not view["review_rows"][1]["excludable"]
		with pytest.raises(journal.StatementError, match="排除"):
			journal.parse_statement(raw, "example.csv", mapping, excluded_rows=[3])


@pytest.mark.parametrize("unknown", ["172.61", "$ 9,117.60", "0", "EUR 12.6", "12.00 USD", "=SUM(A2:A3)"])
def test_amount_like_unknown_cells_require_mapping_with_or_without_another_amount(unknown):
	raw = csv_bytes([["日期", "支出", "other", "备注"], ["20261101", "", unknown, "1"], ["20261102", "2", unknown, "2"],
					 ["20261103", "", "", "123.54"]])
	for mapping in (None, {"date": 0, "expense": 1, "remark": 2}):
		result = journal.parse_statement(raw, "example.csv", mapping, review_offset=0, currency="USD")
		assert [(x["row"], x["field"]) for x in result["errors"]][:2] == [(2, "mapping"), (3, "mapping")]
		assert [r["excludable"] for r in result["review_rows"]] == [False, False, True]
		with pytest.raises(journal.StatementError, match="排除"):
			journal.parse_statement(raw, "example.csv", mapping, currency="USD", excluded_rows=[2])


def test_numeric_unknown_column_can_be_resolved_as_money_and_explicit_nonfinancial_fields_are_safe():
	raw = csv_bytes([["日期", "支出", "opaque", "备注", "银行流水号"], ["20261101", "", "14", "123.00", "000123"]])
	result = journal.parse_statement(raw, "example.csv", {"date": 0, "expense": 1, "income": 2, "remark": 3, "bank_ref": 4})
	assert not result["errors"] and result["rows"][0]["income"] == "14.00" and result["rows"][0]["bank_ref"] == "000123"


def test_foreign_currency_in_separate_header_qualifier_is_mandatory():
	raw = csv_bytes([["日期", "ABONOS USD", "CARGOS USD", "SALDO [USD]"], ["20261101", "2", "", "2"]])
	data = journal.parse_statement(raw, "example.csv", currency=None)
	assert not data["needs_mapping"] and not data["errors"] and data["suggested_currency"] == "USD"
	foreign = journal.parse_statement(raw, "example.csv", {"date": 0, "income": 1}, currency="MXN")
	assert any(x["field"] == "currency" for x in foreign["errors"])


def test_marked_native_export_identifies_ledger_balance_and_only_trusts_the_template_slot():
	rows = [{"date": "2026-11-02", "income": "11", "balance": "21", "remark": "原表备注"}]
	kwargs = {"opening": "10", "account_label": "测试账簿", "currency": "MXN"}
	plain = journal.export_journal(rows, **kwargs)
	marker = "a8" * 32
	encoded = journal.export_journal(rows, **kwargs, ledger_book=marker)
	first = journal.parse_statement(encoded, "bank.csv", currency="MXN")
	assert first["ledger_book"] == marker
	assert journal.parse_statement(plain, "bank.csv", currency="MXN")["ledger_book"] is None
	with pytest.raises(journal.StatementError):
		journal.export_journal(rows, **kwargs, ledger_book="not-a-digest")
	# A previous export template must not copy a stale book identifier to a different export.
	assert journal.parse_statement(journal.export_journal(rows, **kwargs, template=first["template"]), "bank.csv", currency="MXN")["ledger_book"] is None


@pytest.mark.parametrize("early", [
	[["交易日期", "收入", "摘要"], ["20261101", "7.00", "earlier"]],
	[["Month", "Deposit", "Year", "Day"], ["11", "7.00", "2026", "1"]],
])
def test_an_ai_or_explicit_header_cannot_skip_a_statement_with_a_different_date_column(early):
	raw = csv_bytes([*early, ["支出", "附言", "日期", "other"], ["2", "later", "20261102", ""]])
	mapping = {"expense": 0, "summary": 1, "date": 2}
	with pytest.raises(journal.StatementError, match="表头前"):
		journal.parse_statement(raw, "example.csv", mapping, header_row=3)
	parsed = journal.parse_statement(raw, "example.csv", suggest_layout=lambda *_: (3, mapping))
	assert parsed["errors"] or parsed["needs_mapping"]


def test_duplicate_source_balance_cannot_hide_a_change_on_an_amountless_row():
	raw = csv_bytes([["日期", "支出", "余额", "余额"], ["20261101", "2", "8", "8"], ["20261102", "", "8", "19"]])
	for mapping in (None, {"date": 0, "expense": 1, "balance": 2}):
		view = journal.parse_statement(raw, "bank.csv", mapping, review_offset=0)
		assert not view["review_rows"][1]["excludable"] and any(e["row"] == 3 for e in view["errors"])
		with pytest.raises(journal.StatementError, match="排除"):
			journal.parse_statement(raw, "bank.csv", mapping, excluded_rows=[3])
	fixed = raw.replace(b",8,19", b",8,8")
	assert not journal.parse_statement(fixed, "bank.csv", {"date": 0, "expense": 1, "balance": 2}, excluded_rows=[3])["errors"]


@pytest.mark.parametrize("header", ["USD ABONOS", "ABONOS usd", "uSd/ABONOS", "(usd) ABONOS", "ABONOS [uSd]", "（USD）收入"])
def test_currency_qualifier_position_case_and_brackets_do_not_change_account_checks(header):
	raw = csv_bytes([["日期", header, "CARGOS"], ["20261101", "4.00", ""]])
	match = journal.parse_statement(raw, "bank.csv", currency="USD")
	assert not match["errors"] and match["suggested_currency"] == "USD" and match["rows"][0]["income"] == "4.00"
	conflict = journal.parse_statement(raw, "bank.csv", {"date": 0, "income": 1}, currency="MXN")
	assert any(e["field"] == "currency" for e in conflict["errors"])


def test_explicit_account_numbers_and_row_numbers_are_not_additional_amounts():
	raw = csv_bytes([["日期", "支出", "对方账号", "账号", "行序号", "Counterparty Account Number", "Row No."],
		["20261101", "3.25", "0012345", "0056789", "1", "00011", "01"]])
	result = journal.parse_statement(raw, "example.csv", currency="MXN")
	assert not result["errors"] and result["rows"][0]["expense"] == "3.25"
	for label in ("交易数量", "交易行数", "原账号金额", "其他数字", "Deposit"):
		result = journal.parse_statement(raw.replace("对方账号".encode(), label.encode()), "example.csv", currency="MXN")
		assert result["errors"] and not result["rows"]


@pytest.mark.parametrize("early", [
	[["日期", "到账资金"], ["20261101", "1000"]],
	[["日", "到账资金", "月", "年"], ["1", "1000", "11", "2026"]],
])
def test_later_header_cannot_skip_dated_money_under_an_unrecognized_prior_amount_header(early):
	raw = csv_bytes([*early, ["Deposit", "description", "Date"], ["2", "later", "20261102"]])
	mapping = {"income": 0, "summary": 1, "date": 2}
	with pytest.raises(journal.StatementError, match="表头前"):
		journal.parse_statement(raw, "example.csv", mapping, header_row=3)
	# A report date alone is normal bank metadata above the header.
	preamble = csv_bytes([["日期", "对方账号"], ["20261101", "0000187"],
		["Deposit", "description", "Date"], ["2", "later", "20261102"]])
	safe = journal.parse_statement(preamble, "example.csv", mapping, header_row=3)
	assert not safe["errors"] and len(safe["rows"]) == 1


def test_reasoning_model_can_finish_the_layout_json_after_reasoning_tokens(monkeypatch):
	import sys
	from types import ModuleType, SimpleNamespace
	from budget.services import bank_journal_ai
	columns = {"header_row": 2, "columns": {"year": 0, "month": 1, "day": 2, "income": 3}}
	def request(config, messages, *, max_tokens=700):
		# Observed on the split-date workbook: 825 reasoning tokens before the JSON response.
		if max_tokens < 1000:
			raise ValueError("truncated response")
		assert max_tokens <= 4096
		return columns
	module = ModuleType("ai_assistant.ai_assistant")
	module.model_client = SimpleNamespace(get_deepseek_config=lambda: {}, request_deepseek_json=request, DeepSeekError=ValueError)
	monkeypatch.setitem(sys.modules, "ai_assistant.ai_assistant", module)
	assert bank_journal_ai.request_layout([{"header_row": 2, "columns": {"0": ["year"]}}]) == columns
