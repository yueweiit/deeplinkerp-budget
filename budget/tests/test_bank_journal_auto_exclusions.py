import base64
from unittest.mock import Mock

import pytest

from budget.api import bank_journal_files as api
from budget.tests.test_bank_journal_files import book
from budget.tests.test_bank_journal_io import csv_bytes, split_journal


def file_args(rows):
	return dict(book="book", filename="bank.csv", content_base64=base64.b64encode(csv_bytes(rows)).decode())


def test_auto_exclusions_cover_all_pages_and_sign_the_effective_rows(book):
	store, _ = book
	rows = [["日期", "摘要", "收入", "支出", "余额"], ["20261101", "首笔", "1", "0", "1"],
		["20261101", "已人工核对的行", "", "", ""]]
	for balance in range(2, 104):
		rows.append(["20261102", "入账", "1", "", str(balance)])
	rows.extend([["20261103", "手续费", "", "", "103"], ["20261103", "备注", "", "", ""]])
	args = file_args(rows)
	auto = api.preview_import(**args, auto_exclude="1", excluded_rows=[3], review_offset=0)
	expected = [3, 106, 107]
	assert auto["excluded_source_rows"] == expected and auto["excluded_count"] == 3
	assert auto["row_count"] == 103 and auto["income_total"] == "103.00" and auto["review_total"] == 106
	assert auto["errors"] == [] and auto["preview_id"]
	explicit = api.preview_import(**args, excluded_rows=expected, review_offset=100)
	assert explicit["excluded_source_rows"] == expected and explicit["preview_id"] == auto["preview_id"]
	assert [r["source_row"] for r in explicit["review_rows"] if r["excluded"]] == expected[1:]
	assert not api.preview_import(**args)["preview_id"]
	restored = api.preview_import(**args, excluded_rows=[3, 106], review_offset=100)
	assert not restored["preview_id"] and [e["row"] for e in restored["errors"]] == [107]
	confirm = dict(version=4, preview_id=auto["preview_id"], request_id="audited-auto-selection")
	with pytest.raises(ValueError, match="重新校验"):
		api.apply_import(**args, **confirm, excluded_rows=[3, 106])
	store.apply_batch.assert_not_called()
	api.apply_import(**args, **confirm, excluded_rows=expected)
	assert store.apply_batch.call_args.kwargs["import_review"]["excluded_source_rows"] == expected
	assert len(store.apply_batch.call_args.args[1]) == 103


def test_checked_formula_and_edited_trailing_description(book):
	store, _ = book
	args = dict(book="book", filename="bank.xlsx", content_base64=base64.b64encode(split_journal()).decode())
	auto = api.preview_import(**args, auto_exclude=1, review_offset=0)
	assert auto["excluded_source_rows"] == [6, 7] and auto["preview_id"]
	assert auto["review_rows"][2]["balance"] == "10.31" and auto["review_rows"][2]["excludable"]
	edited = {"7": {"income": "", "expense": "0.31"}}
	ready = api.preview_import(**args, auto_exclude=1, edits=edited, review_offset=0)
	assert ready["excluded_source_rows"] == [6] and ready["expense_total"] == "2.31"
	assert ready["preview_id"] and not ready["review_rows"][3]["excluded"]
	api.apply_import(**args, preview_id=ready["preview_id"], version=4, request_id="repaired-amount",
		edits=edited, excluded_rows=[6])
	assert store.apply_batch.call_args.args[1][-1]["file_review"]["source_row"] == 7
	partial = api.preview_import(**args, auto_exclude=1, edits={"7": {"summary": "补录中"}}, review_offset=0)
	assert partial["excluded_source_rows"] == [6] and not partial["preview_id"]
	assert partial["review_rows"][3]["summary"] == "补录中"


@pytest.mark.parametrize(("cells", "field"), [
	(["20261103", "冲突", "", "", "12", "", "", "", ""], "amount"),
	(["20261133", "待核", "", "", "", "", "", "", ""], "date"),
	(["20261103", "待核", "", "", "10", "0001", "", "", ""], "amount"),
	(["20261103", "待核", "", "", "10", "", "OA-123", "", ""], "amount"),
	(["20261103", "待核", "", "", "10", "", "", "USD", ""], "currency"),
	(["20261103", "待核", "", "", "", "", "", "", "8.00"], "mapping"),
	(["20261103", "待核", "1,2", "", "10", "", "", "", ""], "income"),
	(["20261103", "待核", "0.00", "", "", "", "", "", ""], "amount"),
])
def test_source_conflicts_stay_visible(book, cells, field):
	args = file_args([["日期", "摘要", "收入", "支出", "余额", "银行流水号", "钉钉申请单号", "币种", "其他"],
		["20261101", "入账", "10", "0", "10", "", "", "CNY", ""], cells])
	report = api.preview_import(**args, auto_exclude=1, review_offset=0)
	assert report["excluded_source_rows"] == [] and report["row_count"] == 1
	assert [(e["row"], e["field"]) for e in report["errors"]] == [(3, field)]
	assert not report["preview_id"] and not report["review_rows"][1]["excluded"]


def test_invalid_source_formula_cannot_be_omitted(book):
	payload = split_journal(formula="=L3+J4", cached="12.31")
	info = api.preview_import("book", "bank.xlsx", base64.b64encode(payload).decode(), auto_exclude=1)
	assert not info["preview_id"] and 6 not in info["excluded_source_rows"]
	assert any(issue["field"] == "balance" and issue["row"] == 6 for issue in info["errors"])


def test_uncorroborated_interior_row_and_opening_history_need_review_even_in_replace_mode(book):
	store, _ = book
	store.authorize_replacement = Mock()
	store.replacement_impact = Mock(return_value=({}, [], []))
	args = file_args([["日期", "摘要", "收入", "支出", "余额"],
		["20261001", "期初待核", "", "", ""],
		["20261101", "收入", "10", "", "10"],
		["20261102", "中间一笔待核", "", "", ""],
		["20261103", "收入", "7", "", "17"],
		["20261104", "末尾备注", "", "", ""]])
	report = api.preview_import(**args, auto_exclude=1, mode="replace_range", review_offset=0)
	assert report["excluded_source_rows"] == [6]
	assert [e["row"] for e in report["errors"]] == [2, 4] and not report["preview_id"]
	store.replacement_impact.assert_not_called()
	store.apply_batch.assert_not_called()


@pytest.mark.parametrize(("candidate", "excluded"), [("18", True), ("23", False)])
def test_descending_file_compares_with_the_prior_posting_in_time(book, candidate, excluded):
	args = file_args([["日期", "摘要", "收入", "支出", "余额"],
		["20261105", "新", "5", "", "23"], ["20261104", "待核", "", "", candidate],
		["20261103", "早", "8", "", "18"], ["20261102", "最早", "10", "", "10"]])
	view = api.preview_import(**args, auto_exclude=1, review_offset=0)
	assert view["excluded_source_rows"] == ([3] if excluded else []) and bool(view["preview_id"]) is excluded
	if excluded:
		explicit = api.preview_import(**args, excluded_rows=[3])
		assert explicit["preview_id"] == view["preview_id"]


@pytest.mark.parametrize(("stated", "safe"), [("100", True), ("105", False)])
def test_editing_previous_page_cannot_change_the_original_exclusion_evidence(book, stated, safe):
	store, _ = book
	rows = [["日期", "摘要", "收入", "支出", "余额"]]
	for balance in range(1, 101):
		rows.append(["20261101", "入账", "1", "", str(balance)])
	rows.append(["20261103", "待核", "", "", stated])
	args = file_args(rows)
	edits = {"101": {"income": "6", "expense": "", "balance": "105"}}
	initial = api.preview_import(**args, auto_exclude=1, review_offset=0)
	preview = api.preview_import(**args, auto_exclude=1, edits=edits, review_offset=100)
	assert preview["review_rows"][0]["excludable"] is safe
	assert initial["excluded_source_rows"] == preview["excluded_source_rows"] == ([102] if safe else [])
	if safe:
		explicit = api.preview_import(**args, edits=edits, excluded_rows=[102], review_offset=0)
		assert bool(preview["preview_id"]) and explicit["preview_id"] == preview["preview_id"]
		restored = api.preview_import(**args, edits=edits, review_offset=100)
		assert not restored["preview_id"] and restored["review_rows"][0]["excludable"]
		api.apply_import(**args, edits=edits, excluded_rows=[102], preview_id=preview["preview_id"],
			version=4, request_id="original-balance-edit")
		assert store.apply_batch.call_args.args[1][-1]["balance"] == "105.00"
	else:
		assert not preview["preview_id"] and preview["review_errors"][0]["row"] == 102
		with pytest.raises(ValueError, match="不能排除"):
			api.preview_import(**args, edits=edits, excluded_rows=[102])
		store.apply_batch.assert_not_called()


def test_repaired_predecessor_cannot_create_missing_original_balance_evidence(book):
	args = file_args([["日期", "摘要", "收入", "支出", "余额"],
		["20261101", "原表缺金额", "", "", ""], ["20261102", "待核", "", "", "20"]])
	edits = {"2": {"income": "20", "expense": "", "balance": "20"}}
	view = api.preview_import(**args, edits=edits, auto_exclude=1, review_offset=0)
	assert not view["review_rows"][1]["excludable"] and not view["preview_id"]
	assert view["rows"][0]["source_row"] == 2 and view["excluded_source_rows"] == []
	with pytest.raises(ValueError, match="不能排除"):
		api.preview_import(**args, edits=edits, excluded_rows=[3])


@pytest.mark.parametrize(("opening", "balance", "expected"), [
	("0", "10", [4]), ("0", "15", []), (None, "10", []),
])
def test_same_day_single_posting_needs_source_evidence_for_its_order(book, opening, balance, expected):
	rows = [["日期", "摘要", "收入", "支出", "余额"]]
	if opening is not None:
		rows.append(["", "期初余额", "", "", opening])
	rows.extend([["20261101", "入账", "10", "", "10"], ["20261101", "待核", "", "", balance]])
	args = file_args(rows)
	view = api.preview_import(**args, auto_exclude=1, review_offset=0)
	assert view["excluded_source_rows"] == expected and bool(view["preview_id"]) == bool(expected)
	if expected:
		assert api.preview_import(**args, excluded_rows=expected)["preview_id"] == view["preview_id"]
