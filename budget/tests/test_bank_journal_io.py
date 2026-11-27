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
