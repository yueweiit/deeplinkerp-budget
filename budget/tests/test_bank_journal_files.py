import base64
from types import SimpleNamespace
from unittest.mock import Mock

import frappe
import pytest

from budget.api import bank_journal_files as api


@pytest.fixture
def book(monkeypatch):
	doc = SimpleNamespace(
		name="book", company="Example", account="Cash", currency="CNY", revision=4, opening_date="2026-01-01"
	)
	store = SimpleNamespace(
		authorize_book=Mock(return_value=doc), apply_batch=Mock(return_value={"added": 1}),
		legacy_import_issues=Mock(return_value=[]),
		book_currency=lambda value: getattr(value, "currency", None) or "CNY",
	)
	monkeypatch.setattr(api, "_store", lambda: store)
	monkeypatch.setattr(api, "_secret", lambda: b"test secret")
	monkeypatch.setattr(frappe, "session", SimpleNamespace(user="finance"))
	monkeypatch.setattr(frappe, "throw", lambda msg, *a: (_ for _ in ()).throw(ValueError(msg)))
	encoded = base64.b64encode("日期,摘要,收入\r\n2026-11-16,示例,1.00".encode()).decode()
	return store, encoded


def test_bound_preview_reparses_bank_file_and_calls_native_writer(book):
	store, encoded = book
	info = api.preview_import("book", "示例.csv", encoded)
	assert info["row_count"] == 1 and info["income_total"] == "1.00" and info["version"] == 4
	assert "raw_cells" not in info["rows"][0]
	result = api.apply_import("book", "示例.csv", encoded, None, info["preview_id"], info["version"], "req-1")
	assert result == {"added": 1}
	assert store.apply_batch.call_args.args[1][0]["income"] == "1.00"
	store.apply_batch.assert_called_once()


def test_preview_content_user_and_revision_are_bound(book, monkeypatch):
	store, encoded = book
	receipt = api.preview_import("book", "示例.csv", encoded)["preview_id"]
	for user, version, content in [
		("finance", 5, encoded),
		("other", 4, encoded),
		("finance", 4, base64.b64encode(base64.b64decode(encoded) + b"\n").decode()),
	]:
		monkeypatch.setattr(frappe, "session", SimpleNamespace(user=user))
		with pytest.raises(ValueError):
			api.apply_import("book", "示例.csv", content, None, receipt, version, "req-1")
	store.apply_batch.assert_not_called()


def test_bad_rows_and_invalid_base64_cannot_be_applied(book):
	store, _ = book
	with pytest.raises(ValueError):
		api.preview_import("book", "流水.csv", "【非BASE64】")
	invalid = base64.b64encode("日期,收入\n2026-11-16,￥X".encode()).decode()
	preview = api.preview_import("book", "流水.csv", invalid)
	assert preview["errors"] and not preview["preview_id"]
	with pytest.raises(ValueError):
		api.apply_import("book", "流水.csv", invalid, None, "bad", 4, "req-2")
	store.apply_batch.assert_not_called()


def test_file_replacement_preview_binds_mode_dates_edits_and_same_book(book):
	store, _ = book
	store.authorize_replacement = Mock()
	store.replacement_impact = Mock(return_value=(
		{"start": "2026-11-05", "end": "2026-11-20", "old_count": 3, "new_count": 2}, [], []
	))
	encoded = base64.b64encode("日期,摘要,收入\n2026-11-05,甲,2\n2026-11-20,乙,5".encode()).decode()
	args = dict(book="book", filename="流水.csv", content_base64=encoded)
	append = api.preview_import(**args, mode="append")
	result = api.preview_import(**args, mode="replace_range")
	assert result["impact"]["old_count"] == 3 and result["preview_id"] != append["preview_id"]
	assert [r["date"] for r in store.replacement_impact.call_args.args[1]] == ["2026-11-05", "2026-11-20"]
	confirm = dict(**args, version=4, request_id="replace-file-test")
	for mode, token in (("replace_range", append["preview_id"]), ("append", result["preview_id"])):
		with pytest.raises(ValueError, match="预览已变化"):
			api.apply_import(**confirm, mode=mode, preview_id=token)
	with pytest.raises(ValueError, match="预览已变化"):
		api.apply_import(**confirm, mode="replace_range", preview_id=result["preview_id"], edits={"3":{"date":"2026-11-21"}})
	store.apply_batch.assert_not_called()
	api.apply_import(**confirm, mode="replace_range", preview_id=result["preview_id"])
	assert store.apply_batch.call_args.kwargs["replace_range"] is True
	assert store.authorize_replacement.called


def test_replacement_requires_valid_target_and_clean_whole_file(book):
	store, encoded = book
	store.authorize_replacement = Mock()
	store.replacement_impact = Mock(return_value=({}, [], [{"row":2,"field":"balance","message":"区间期初余额不符"}]))
	with pytest.raises(ValueError, match="已有账簿"):
		api.preview_import(filename="a.csv", company="A", content_base64=encoded, mode="replace_range")
	with pytest.raises(ValueError, match="导入方式"):
		api.preview_import("book", "a.csv", encoded, mode="replace_all")
	result = api.preview_import("book", "a.csv", encoded, mode="replace_range")
	assert not result["preview_id"] and result["error_count"] == 1
	store.authorize_replacement.side_effect = PermissionError("没有覆盖权限")
	with pytest.raises(PermissionError):
		api.preview_import("book", "a.csv", encoded, mode="replace_range")
	store.apply_batch.assert_not_called()


def test_file_review_binds_corrected_cells_and_preserves_bank_movement_identity(book):
	store, encoded = book
	base = api.preview_import("book", "流水.csv", encoded, review_offset=0)
	assert base["review_rows"][0]["source_row"] == 2
	edits = {"2": {"income": "1.75", "expense": "", "summary": "已核对"}}
	preview = api.preview_import("book", "流水.csv", encoded, edits=edits, review_offset=0)
	assert preview["review_rows"][0]["income"] == preview["income_total"] == "1.75"
	assert preview["review_rows"][0]["summary"] == "已核对"
	store.apply_batch.assert_not_called()

	def apply(info, changes):
		return api.apply_import(
			"book", "流水.csv", encoded, info["mapping_used"], info["preview_id"], 4, "review-test", edits=changes
		)

	with pytest.raises(ValueError, match="预览已变化"):
		apply(base, edits)
	with pytest.raises(ValueError, match="预览已变化"):
		apply(preview, {"2": {"income": "1.99", "expense": "", "summary": "已核对"}})
	apply(base, None)
	origin = store.apply_batch.call_args.args[1][0]["source_key"]
	apply(preview, edits)
	row = store.apply_batch.call_args.args[1][0]
	assert (row["source_key"], row["income"], row["summary"]) == (origin, "1.75", "已核对")
	assert row["file_review"]["original"]["income"] == "1.00"


def test_file_review_pages_include_invalid_rows_and_validate_entire_file(book):
	store, _ = book
	lines = ["日期,收入,余额", "2026-11-16,1,101", "错误日期,误识别,102", "2026-11-18,1,103"]
	encoded = base64.b64encode("\n".join(lines).encode()).decode()
	bad = api.preview_import("book", "流水.csv", encoded, review_offset=1)
	assert bad["review_total"] == 3 and not bad["preview_id"]
	assert bad["review_rows"][0]["date"] == "错误日期"
	assert bad["review_rows"][0]["income"] == "误识别"
	edits = {"3": {"date": "2026-11-17", "income": "1", "expense": ""}}
	receipts = set()
	for offset in (0, 2):
		preview = api.preview_import("book", "流水.csv", encoded, edits=edits, review_offset=offset)
		assert preview["preview_id"] and preview["income_total"] == "3.00"
		assert preview["review_rows"][0]["source_row"] == offset + 2
		receipts.add(preview["preview_id"])
	assert len(receipts) == 1
	bad_balance = api.preview_import("book", "流水.csv", encoded, edits={"3": {**edits["3"], "balance": "103"}})
	assert not bad_balance["preview_id"] and bad_balance["errors"][0]["field"] == "balance"
	for changes in ({"1": {"date": "2026-11-10"}}, {"4": {"bank_ref": "fake"}}):
		with pytest.raises(ValueError, match="修改"):
			api.preview_import("book", "流水.csv", encoded, edits=changes)
	store.apply_batch.assert_not_called()


def test_export_default_month_and_explicit_all(book, monkeypatch):
	store, _ = book
	store.export_rows = Mock(
		return_value={"rows": [], "opening": "0.00", "account": "示例", "company": "A", "template": None}
	)
	monkeypatch.setattr(api, "_current_month", lambda: "2026-11")
	for selector, expected in [(None, "2026-11"), ("all", None)]:
		output = api.export_journal("book", selector)
		assert output["filename"].endswith(".csv") and base64.b64decode(output["content_base64"]).startswith(
			b"\xef\xbb\xbf"
		)
		assert store.export_rows.call_args.args == ("book", expected)


def test_equivalent_suggested_mapping_from_ui_can_be_confirmed(book):
	_, encoded = book
	info = api.preview_import("book", "a.csv", encoded)
	result = api.apply_import(
		"book",
		"a.csv",
		encoded,
		{k: str(v) for k, v in info["suggested_mapping"].items()},
		info["preview_id"],
		info["version"],
		"req-1234",
	)
	assert result == {"added": 1}


def test_structured_preview_apply_and_receipt_namespace(book):
	store, encoded = book
	scope = {"book": "book", "company": "Example", "account": "Cash", "currency": "CNY"}
	rows = [{"date": "2026-11-16", "income": "1.00", "summary": "示例"}]
	preview = api.preview_rows(**scope, rows=rows)
	assert preview["rows"][0]["source_row"] == 1
	assert preview["income_total"] == "1.00" and preview["version"] == 4
	assert not {"weak_file_proof", "source_key"} & preview["rows"][0].keys()
	params = {
		**scope,
		"rows": rows,
		"preview_id": preview["preview_id"],
		"version": 4,
		"request_id": "json-req-1234",
	}
	assert api.apply_rows(**params) == {"added": 1}
	assert store.apply_batch.call_args.args[2:] == ("Import", 4, "json-req-1234")
	for invalid in [
		{"preview_id": api.preview_import("book", "原表.csv", encoded)["preview_id"]},
		{"rows": [dict(rows[0], category="changed")]},
		{"currency": "USD"},
		{"account": "Other"},
		{"company": "Other"},
	]:
		with pytest.raises(ValueError):
			api.apply_rows(**{**params, **invalid})
	with pytest.raises(ValueError):
		api.apply_import("book", "原表.csv", encoded, None, preview["preview_id"], 4, "file-req-1234")
	assert store.apply_batch.call_count == 1


def test_structured_preview_errors_block_confirmation_and_limit_rows(book):
	scope = {"book": "book", "company": "Example", "account": "Cash", "currency": "CNY"}
	for rows in ([], "[]", [{"date": "2026-11-01", "expense": "1"}] * 10001):
		with pytest.raises(ValueError):
			api.preview_rows(**scope, rows=rows)
	rows = [{"date": "2026-11-03", "expense": "1.20"}] * 101
	pre = api.preview_rows(**scope, rows=rows)
	assert pre["row_count"] == 101 and len(pre["rows"]) == 100 and pre["expense_total"] == "121.20"
	bad = api.preview_rows(**scope, rows=[*rows, {"date": "yesterday"}])
	assert bad["error_count"] == 1 and bad["errors"][0]["row"] == 102 and bad["preview_id"] == ""
	with pytest.raises(ValueError):
		api.apply_rows(
			**scope,
			rows=[*rows, {"date": "yesterday"}],
			version=4,
			request_id="json-long-req",
			preview_id=pre["preview_id"],
		)


def test_structured_contract_scope_size_and_date(book, monkeypatch):
	store, _ = book
	scope = {"book": "book", "company": "Example", "account": "Cash", "currency": "CNY"}
	rows = [{"date": "2026-01-01", "expense": "2.00", "refs": ["00047"]}]
	base = api.preview_rows(**scope, rows=rows, cmd="budget.api.bank_journal_files.preview_rows")
	for change in (
		{"company": "Other"},
		{"account": "Other"},
		{"currency": "USD"},
		{"currency": None},
		{"book": {}},
		{"unknown": True},
	):
		with pytest.raises(ValueError):
			api.preview_rows(**{**scope, **change}, rows=rows)
	prior = api.preview_rows(**scope, rows=[dict(rows[0], date="2025-12-31")])
	assert prior["preview_id"] == "" and prior["errors"][0]["field"] == "date"
	check = {
		**scope,
		"preview_id": base["preview_id"],
		"version": 4,
		"request_id": "json-scope-check",
		"rows": rows,
	}
	for version in (True, "4", 5):
		with pytest.raises(ValueError):
			api.apply_rows(**{**check, "version": version})
	monkeypatch.setattr(frappe, "session", SimpleNamespace(user="other user"))
	with pytest.raises(ValueError):
		api.apply_rows(**check)
	store.apply_batch.assert_not_called()
	monkeypatch.setattr(api.formats, "MAX_BYTES", 25)
	with pytest.raises(ValueError, match="6 MiB"):
		api.preview_rows(**scope, rows=rows)


@pytest.mark.parametrize("extension", ["csv", "xlsx"])
def test_ai_layout_reads_original_values_and_binds_header_to_confirmation(book, monkeypatch, extension):
	from budget.services import bank_journal_ai as ai_layout

	store, _ = book
	raw = (
		"Example Private Account 001234567890\n"
		"过账日,资金流入额,资金流出额,账面余款,业务说明,Currency code\n"
		"2026-11-17,27.34,,127.34,Private memo 7788,CNY\n"
		"2026-11-18,,10.12,117.22,Private memo 9900,CNY"
	)
	payload = raw.encode()
	if extension == "xlsx":
		from io import BytesIO

		from openpyxl import Workbook

		workbook = Workbook()
		for row in raw.splitlines():
			workbook.active.append(row.split(","))
		output = BytesIO()
		workbook.save(output)
		payload = output.getvalue()
	filename = f"native.{extension}"
	encoded = base64.b64encode(payload).decode()

	def selected(candidates):
		safe_input = str(candidates)
		for value in ("001234567890", "2026-11-17", "Private", "27.34", "117.22", "7788"):
			assert value not in safe_input
		return {"header_row": 2, "columns": {
			"date": 0, "income": 1, "expense": 2, "balance": 3, "summary": 4, "currency": 5,
		}}

	model = Mock(side_effect=selected)
	monkeypatch.setattr(ai_layout, "request_layout", model)
	preview = api.preview_import("book", filename, encoded, use_ai=1)
	assert preview["mapping_source"] == "deepseek" and preview["header_row"] == 2
	assert preview["errors"] == [] and preview["preview_id"]
	assert [row["summary"] for row in preview["rows"]] == ["Private memo 7788", "Private memo 9900"]
	assert (preview["income_total"], preview["expense_total"]) == ("27.34", "10.12")
	store.apply_batch.assert_not_called()
	kwargs = dict(
		mapping=preview["mapping_used"], version=preview["version"],
		preview_id=preview["preview_id"], request_id="ai-import-1",
	)
	with pytest.raises(ValueError):
		api.apply_import("book", filename, encoded, header_row=1, **kwargs)
	assert api.apply_import("book", filename, encoded, header_row=2, **kwargs) == {"added": 1}
	model.assert_called_once()


def test_unusable_ai_mapping_keeps_manual_preview_and_cannot_skip_transactions(book, monkeypatch):
	from budget.services import bank_journal_ai as ai_layout

	store, good = book
	raw = (
		"过账日,资金流入额,资金流出额,账面余款,业务说明\n"
		"2026-11-17,27.34,,127.34,第一笔\n"
		"过账日,资金流入额,资金流出额,账面余款,业务说明\n"
		"2026-11-18,,10.12,117.22,第二笔"
	)
	encoded = base64.b64encode(raw.encode()).decode()
	model = Mock(return_value={"header_row": 3, "columns": {"date": 0, "income": 1, "expense": 2}})
	monkeypatch.setattr(ai_layout, "request_layout", model)
	assert api.preview_import("book", "local.csv", good, use_ai=1)["preview_id"]
	model.assert_not_called()
	assert api.preview_import("book", "local.csv", encoded, use_ai=1)["needs_mapping"]
	with pytest.raises(ValueError, match="表头前"):
		api.preview_import("book", "local.csv", encoded, header_row=3, mapping=model.return_value["columns"])
	store.apply_batch.assert_not_called()


@pytest.fixture
def new_target(book, monkeypatch):
	from budget.api import bank_journal

	store, _ = book
	store.authorize_book_creation = Mock()
	store.book_settings = bank_journal.book_settings
	monkeypatch.setattr(bank_journal, "supported_currencies", lambda: ["CNY", "MXN", "USD"])
	store.book_exists = Mock(return_value=False)
	payload = base64.b64encode("银行账户名称,测试专户\n日期,摘要,支出,余额\n2026-11-05,服务费,8.25,91.75".encode()).decode()
	return store, dict(company="示例公司", filename="银行.csv", content_base64=payload, currency="CNY")


def test_new_book_preview_bank_defaults_edit_and_bound_confirmation(new_target):
	store, args = new_target
	first = api.preview_import(**args, review_offset=0, use_ai=1)
	assert first["book_settings"] == {"account": "测试专户", "opening": "100.00", "opening_date": "2026-11-05", "currency": "CNY"}
	assert first["review_rows"][0]["expense"] == "8.25" and first["preview_id"]
	store.apply_batch.assert_not_called()
	store.authorize_book_creation.assert_called_once_with("示例公司")
	call = dict(version=0, preview_id=first["preview_id"], request_id="test-new-book")
	for changed in ({"account": "另一专户"}, {"opening": "99.00"}, {"opening_date": "2026-11-04"},
					{"company": "另一公司"}, {"edits": {"3": {"summary": "改过"}}}):
		with pytest.raises(ValueError, match="预览已变化"):
			api.apply_import(**{**args, **changed, **call})
	store.apply_batch.assert_not_called()
	result = api.apply_import(**args, **call)
	assert result["book"] and result["added"] == 1
	settings = store.apply_batch.call_args.kwargs["new_book"]
	assert settings.company == "示例公司" and settings.opening == "100.00"
	assert store.apply_batch.call_args.args[0] == result["book"]
	assert store.apply_batch.call_args.args[1][0]["expense"] == "8.25"


def test_incomplete_new_book_still_has_editable_rows_zero_is_explicit(new_target):
	store, args = new_target
	args["content_base64"] = base64.b64encode("对方账号,别人的账号\n日期,收入\n2026-11-05,8.25".encode()).decode()
	incomplete = api.preview_import(**args, review_offset=0)
	assert incomplete["preview_id"] == "" and len(incomplete["review_rows"]) == 1
	assert {p["field"] for p in incomplete["book_issues"]} == {"account", "opening"}
	good = dict(**args, account="手动账户", opening="0")
	fixed = api.preview_import(**good, review_offset=0)
	assert fixed["preview_id"] and fixed["book_settings"]["opening"] == "0.00"
	late = api.preview_import(**good, opening_date="2026-12-05")
	assert late["preview_id"] == "" and late["errors"][0]["row"] == 3
	store.book_exists.return_value = True
	existing = api.preview_import(**good)
	assert existing["preview_id"] == "" and "已有账簿" in existing["book_issues"][0]["message"]
	store.apply_batch.assert_not_called()


def test_new_book_original_template_and_ai_layout_share_preview(new_target, monkeypatch):
	from budget.tests.test_bank_journal_io import sample_journal
	from budget.services import bank_journal_ai

	store, args = new_target
	template = api.preview_import(**{**args, "content_base64": base64.b64encode(sample_journal()).decode()}, review_offset=0)
	assert template["preview_id"] and template["book_settings"] == {
		"account": "示例银行", "opening": "1000.00", "opening_date": "2026-01-01", "currency": "CNY"
	}
	raw = "过账日,资金流入额,业务说明,账面余款\n2026-11-01,8.00,示例,18.00"
	model = Mock(return_value={"header_row": 1, "columns": {"date": 0, "income": 1, "summary": 2, "balance": 3}})
	monkeypatch.setattr(bank_journal_ai, "request_layout", model)
	args.update(content_base64=base64.b64encode(raw.encode()).decode(), account="测试银行")
	ai = api.preview_import(**args, review_offset=0, use_ai=1)
	assert ai["mapping_source"] == "deepseek" and ai["preview_id"] and ai["book_settings"]["opening"] == "10.00"
	args.update(mapping=ai["mapping_used"], header_row=ai["header_row"], edits={"2": {"summary": "已校对"}})
	edited = api.preview_import(**args, review_offset=0)
	assert edited["review_rows"][0]["summary"] == "已校对" and edited["preview_id"] != ai["preview_id"]
	model.assert_called_once()
	store.apply_batch.assert_not_called()


def test_new_book_authorizes_company_before_file_and_ai(new_target, monkeypatch):
	store, args = new_target
	reader = Mock()
	monkeypatch.setattr(api, "_read", reader)
	store.authorize_book_creation.side_effect = frappe.PermissionError()
	with pytest.raises(frappe.PermissionError):
		api.preview_import(**args, use_ai=1)
	with pytest.raises(frappe.PermissionError):
		api.apply_import(**args)
	reader.assert_not_called()


def test_new_book_uses_earliest_evidenced_balance_and_keeps_ambiguous_account_empty(new_target):
	_, args = new_target
	raw = ("开户银行,示例银行,本方账号,000123\n日期,摘要,收入,支出,余额\n"
		   "2026-11-01,收入,20,,\n2026-11-02,支出,,5,115")
	args["content_base64"] = base64.b64encode(raw.encode()).decode()
	good = api.preview_import(**args)
	assert good["preview_id"] and good["book_settings"]["opening"] == "100.00"
	assert good["book_settings"]["account"] == "示例银行 000123"
	args["content_base64"] = base64.b64encode(("本方账号,000456\n"+raw).encode()).decode()
	proposed = api.preview_import(**args)
	assert proposed["book_settings"]["account"] == "" and not proposed["preview_id"]


def test_new_file_book_requires_confirmed_currency_before_apply(new_target):
	store, args = new_target
	args = {**args, "currency": None}
	initial = api.preview_import(**args, review_offset=0)
	assert initial["row_count"] == 1 and not initial["preview_id"]
	assert "currency" in {issue["field"] for issue in initial["book_issues"]}
	mxn = api.preview_import(**{**args, "currency": "MXN"})
	assert mxn["book_settings"]["currency"] == "MXN" and mxn["preview_id"]
	assert api.preview_import(**{**args, "currency": " mxn "})["currency"] == "MXN"
	with pytest.raises(ValueError, match="币种"):
		api.preview_import(**{**args, "currency": ["MXN"]})
	confirm = dict(preview_id=mxn["preview_id"], version=0, request_id="peso-confirmation")
	with pytest.raises(ValueError, match="预览已变化"):
		api.apply_import(**{**args, "currency": "USD"}, **confirm)
	api.apply_import(**{**args, "currency": "MXN"}, **confirm)
	assert store.apply_batch.call_args.kwargs["new_book"].currency == "MXN"


def test_edited_opening_keeps_source_balance_and_previews_ledger_balance(new_target):
	store, args = new_target
	args = {**args, "opening": "120", "opening_date": "2026-10-01", "currency": "MXN"}
	info = api.preview_import(**args, review_offset=0)
	assert info["book_settings"]["opening"] == "120.00" and info["preview_id"]
	assert info["review_rows"][0]["balance"] == "91.75"
	assert info["opening_adjustment"] == {
		"file_opening": "100.00", "book_opening": "120.00", "difference": "20.00", "closing": "111.75",
	}
	api.apply_import(**args, preview_id=info["preview_id"], version=0, request_id="opening-edit")
	call = store.apply_batch.call_args
	assert (call.kwargs["opening"], call.kwargs["new_book"].opening, call.kwargs["opening_date"]) == ("100.00", "120.00", "2026-10-01")
	assert call.args[1][0]["balance"] == "91.75" and "ledger_balance" not in call.args[1][0]
	assert call.kwargs["import_review"]["file_opening"] == "100.00"


def test_existing_foreign_book_and_json_validate_same_currency(book):
	store, encoded = book
	store.authorize_book.return_value.currency = "MXN"
	args = dict(book="book", filename="b.csv", content_base64=encoded)
	assert api.preview_import(**args)["currency"] == "MXN"
	with pytest.raises(ValueError, match="币种"):
		api.preview_import(**args, currency="CNY")
	scope = dict(book="book", company="Example", account="Cash", currency="MXN")
	rows = [{"date": "2026-11-16", "income": "1.00"}]
	info = api.preview_rows(**scope, rows=rows)
	assert info["preview_id"] and info["currency"] == "MXN"
	api.apply_rows(**scope, rows=rows, preview_id=info["preview_id"], version=4, request_id="mxn-json")
	assert "MXN" in store.apply_batch.call_args.kwargs["request_facts"][0]


def test_explicit_exclusions_are_bound_to_file_and_audited(new_target):
	store, args = new_target
	content = "日期,摘要,收入,支出,余额\n2026-11-01,第一笔,5,,5\n2026-11-02,第二笔,,,\n2026-11-03,第三笔,,,"
	args = {**args, "content_base64": base64.b64encode(content.encode()).decode(), "account":"测试账户", "currency":"MXN"}
	info = api.preview_import(**args, excluded_rows=[3, 4], review_offset=0)
	assert info["excluded_count"] == 2 and info["row_count"] == 1 and info["preview_id"]
	assert [r["source_row"] for r in info["review_rows"] if r.get("excluded")] == [3, 4]
	confirm = dict(preview_id=info["preview_id"], version=0, request_id="explicit-exclusions")
	with pytest.raises(ValueError):
		api.apply_import(**args, excluded_rows=[3], **confirm)
	api.apply_import(**args, excluded_rows="[4,3]", **confirm)
	assert store.apply_batch.call_args.kwargs["import_review"]["excluded_source_rows"] == [3, 4]


def test_legacy_ambiguous_summary_receipt_blocks_duplicating_an_existing_import(book):
	from budget.services import bank_journal_io as fmt
	store, _ = book
	content = "日期,摘要,备注,收入\n2026-11-01,电商入账,订单123,100\n2026-11-01,另一收款,订单456,100"
	args = dict(book="book", filename="old.csv", content_base64=base64.b64encode(content.encode()).decode())
	keys = [fmt._record_key("bank-movement", "2026-11-01", "100.00", None, None, "", n) for n in (1, 2)]
	def existing(book_doc, rows):
		assert all(key in row["_legacy_source_keys"] for key, row in zip(keys, rows, strict=True))
		return [{"row": 2, "field": "summary", "message": "原行已按旧版摘要或余额导入"}]
	store.legacy_import_issues = Mock(side_effect=existing)
	report = api.preview_import(**args)
	assert not report["preview_id"] and report["errors"][0]["row"] == 2
	assert "_legacy_source_keys" not in report["rows"][0]
	store.legacy_import_issues.return_value = []
	store.legacy_import_issues.side_effect = None
	ready = api.preview_import(**args)
	assert ready["preview_id"]


def test_ledger_export_uses_site_marker_only_for_the_original_existing_book(book):
	store, _ = book
	marker = api._ledger_marker("book")
	assert api._marked_book(store.authorize_book.return_value, {"ledger_book": marker}) == "book"
	assert api._marked_book(store.authorize_book.return_value, {}) is None
	with pytest.raises(ValueError, match="其他账簿|对应的账簿"):
		api._marked_book(store.authorize_book.return_value, {"ledger_book": api._ledger_marker("elsewhere")})


@pytest.mark.parametrize(("content","prior_summary"), [
	('日期,收入,"银行流水号\nBank reference",摘要\n2026-11-01,100,AB-950,客户入账', "客户入账"),
	("日期,收入,摘要,备注\n2026-11-01,100,客户入账,订单123", "订单123"),
])
def test_reimport_checks_v1_reference_upgrade_and_manual_memo_mapping(book, content, prior_summary):
	from budget.services import bank_journal_io as fmt
	store, _ = book
	previous = fmt._record_key("bank-movement", "2026-11-01", "100.00", None, None, prior_summary, 1)
	def existing(_doc, rows):
		assert previous in rows[0]["_legacy_source_keys"]
		return [{"row": 2, "field": "summary", "message": "旧版来源已入账"}]
	store.legacy_import_issues = Mock(side_effect=existing)
	args = dict(book="book", filename="previous.csv", content_base64=base64.b64encode(content.encode()).decode())
	assert not api.preview_import(**args)["preview_id"]


@pytest.fixture
def file_first(new_target, monkeypatch):
	store, _ = new_target
	company = "拉丁购国际电子商务（东莞）有限公司"
	monkeypatch.setattr(frappe, "db", SimpleNamespace(
		get_value=lambda *a, **kw: frappe._dict(abbr="拉丁购", default_currency="CNY"),
		sql=Mock(return_value=[]),
	))
	monkeypatch.setattr(frappe, "conf", {"bank_journal_currency_defaults": {company: {"万里汇": "MXN"}}})
	return store, dict(company=company, filename="拉丁购万里汇日记账.csv", suggest_book_defaults=1)


def statement(text):
	return base64.b64encode(text.encode()).decode()


def test_file_first_defaults_scoped_account_and_peso_and_signs_displayed_values(file_first):
	store, args = file_first
	args["content_base64"] = statement("年,月,日,摘要,收入,支出,余额\n,,,期初余额,,,0\n2026,7,23,收款,$12.50,,$12.50")
	preview = api.preview_import(**args, review_offset=0)
	assert preview["preview_id"] and not preview["book_issues"]
	assert preview["book_settings"] == {"account": "万里汇", "currency": "MXN", "opening": "0.00", "opening_date": "2026-07-23"}
	assert preview["currency"] == "MXN" and preview["income_total"] == "12.50"
	write = {key: value for key, value in args.items() if key != "suggest_book_defaults"}
	write.update(preview["book_settings"], mapping=preview["mapping_used"], header_row=preview["header_row"])
	api.apply_import(**write, version=0, request_id="file-first", preview_id=preview["preview_id"])
	assert store.apply_batch.call_args.kwargs["new_book"].account == "万里汇"
	with pytest.raises(ValueError, match="预览已变化"):
		api.apply_import(**{**write, "currency": "USD"}, version=0, request_id="changed", preview_id=preview["preview_id"])


def test_file_first_respects_explicit_source_and_manual_defaults(file_first):
	_, args = file_first
	args["content_base64"] = statement("银行账户名称,外汇主账户\n日期,摘要,收入,余额,币种\n2026-07-23,收款,10,40,USD")
	source = api.preview_import(**args)
	assert source["book_settings"] == {"account": "外汇主账户", "currency": "USD", "opening": "30.00", "opening_date": "2026-07-23"}
	manual = api.preview_import(**args, account="我的账本", opening="20", opening_date="2026-07-01")
	assert manual["preview_id"] and manual["book_settings"] == {"account": "我的账本", "currency": "USD", "opening": "20.00", "opening_date": "2026-07-01"}
	assert manual["opening_adjustment"]["closing"] == "30.00"
	conflict = api.preview_import(**args, currency="MXN")
	assert conflict["error_count"] and not conflict["preview_id"]


def test_file_first_no_cross_company_dollar_default_and_no_false_source_opening(file_first):
	_, args = file_first
	args["content_base64"] = statement("日期,摘要,收入\n2026-07-23,收款,$10")
	other = api.preview_import(**{**args, "company": "另一家公司"})
	assert not other["book_settings"]["currency"] and not other["preview_id"]
	plain = api.preview_import(**{**args, "filename": "转账专户日记账.csv",
		"content_base64": statement("日期,摘要,收入\n2026-07-23,收款,10")})
	assert plain["book_settings"] == {"account": "转账专户", "currency": "CNY", "opening": "0.00", "opening_date": "2026-07-23"}
	assert plain["opening"] is None and plain["preview_id"]


def test_file_first_conflicting_own_account_or_currencies_require_correction(file_first):
	_, args = file_first
	args["content_base64"] = statement("本方账号,0001\n本方账号,0002\n日期,摘要,收入,余额\n2026-07-23,收款,10,10")
	conflict = api.preview_import(**args)
	assert not conflict["book_settings"]["account"] and not conflict["preview_id"]
	args["content_base64"] = statement("日期,摘要,收入,余额,币种\n2026-07-23,收款,10,10,USD\n2026-07-24,收款,15,25,MXN")
	mixed = api.preview_import(**args)
	assert mixed["error_count"] and not mixed["preview_id"]


def test_company_with_three_account_currencies_uses_its_own_bank_ledgers(file_first):
	_, args = file_first
	accounts = {"国内结算": "CNY", "比索结算": "MXN", "美元结算": "USD"}
	def bank_currencies(_query, params):
		return [(accounts[params["account"]], None)] if params["company"] == args["company"] else []
	frappe.db.sql.side_effect = bank_currencies
	for account, code in accounts.items():
		amount = "50" if code == "CNY" else "$50"
		preview = api.preview_import(**args, account=account,
			content_base64=statement(f"日期,收入,余额\n2026-07-23,{amount},{amount}"))
		assert preview["book_settings"]["currency"] == code and preview["preview_id"]
		assert preview["income_total"] == "50.00"
	other = api.preview_import(**{**args, "company": "另一家公司"}, account="比索结算",
		content_base64=statement("日期,收入\n2026-07-23,$50"))
	assert not other["book_settings"]["currency"] and not other["preview_id"]


def test_ambiguous_native_bank_name_leaves_currency_editable(file_first):
	_, args = file_first
	frappe.db.sql.return_value = [("USD", None), ("MXN", None)]
	args.update(account="多币种结算", content_base64=statement("日期,收入,余额\n2026-07-23,10,10"))
	preview = api.preview_import(**args)
	assert not preview["book_settings"]["currency"] and not preview["preview_id"]
	manual = api.preview_import(**args, currency="USD")
	assert manual["book_settings"]["currency"] == "USD" and manual["preview_id"]


def test_own_account_number_resolves_bank_currency_until_target_is_changed(file_first):
	_, args = file_first
	frappe.db.sql.side_effect = lambda query, params: [("USD", "000123")] if params["own_number"] == "000123" else []
	args["content_base64"] = statement("开户行,某银行\n本方账号,000123\n日期,收入\n2026-07-23,10")
	bank = api.preview_import(**args)
	assert bank["book_settings"]["account"] == "某银行 000123"
	assert bank["book_settings"]["currency"] == "USD" and bank["preview_id"]
	renamed = api.preview_import(**args, account="另一专户")
	assert renamed["book_settings"]["currency"] == "CNY" and renamed["preview_id"]


def test_bank_master_disagrees_with_scoped_currency_convention(file_first):
	_, args = file_first
	frappe.db.sql.return_value = [("USD", None)]
	args["content_base64"] = statement("日期,收入\n2026-07-23,$10")
	preview = api.preview_import(**args)
	assert not preview["preview_id"] and not preview["book_settings"]["currency"]
	selected = api.preview_import(**args, currency="MXN")
	assert selected["preview_id"] and selected["currency"] == "MXN"


@pytest.mark.parametrize(("master_number", "expected"), [("", "USD"), ("000999", "")])
def test_named_foreign_account_with_optional_or_conflicting_account_number(file_first, master_number, expected):
	_, args = file_first
	frappe.db.sql.side_effect = lambda query, params: [("USD", master_number)] if params["account"] == "出口户" else []
	args["content_base64"] = statement("银行账户名称,出口户\n本方账号,000123\n日期,收入\n2026-07-23,10")
	result = api.preview_import(**args)
	assert result["book_settings"]["currency"] == expected and bool(result["preview_id"]) == bool(expected)


@pytest.mark.parametrize("label", ["拉丁购万里汇", "拉丁购国际电子商务（东莞）有限公司 - 万里汇"])
def test_confirmed_currency_accepts_own_company_prefix_without_renaming_book(file_first, label):
	store, args = file_first
	args.update(account=label, content_base64=statement("日期,收入,余额\n2026-07-23,$10,$10"))
	result = api.preview_import(**args)
	assert result["book_settings"]["account"] == label
	assert result["currency"] == "MXN" and result["preview_id"]
	store.book_exists.return_value = True
	existing = api.preview_import(**args)
	assert existing["currency"] == "MXN" and not existing["preview_id"]
	assert [issue["field"] for issue in existing["book_issues"]] == ["account"]


def test_currency_preference_preserves_exact_named_account_and_company_boundaries(file_first):
	_, args = file_first
	prefs = frappe.conf["bank_journal_currency_defaults"][args["company"]]
	prefs["拉丁购万里汇"] = "USD"
	args["content_base64"] = statement("日期,收入\n2026-07-23,$10")
	assert api.preview_import(**args, account="拉丁购万里汇")["currency"] == "USD"
	for target in ({"account":"拉丁购万里汇二号"}, {"account":"其他公司万里汇"},
				   {"account":"拉丁购万里汇", "company":"另一家公司"}):
		unknown = api.preview_import(**{**args, **target})
		assert not unknown["preview_id"] and not unknown["book_settings"]["currency"]
		assert unknown["book_issues"] == [{"field":"currency", "message":"请选择账簿币种。"}]


def test_prefixed_currency_preference_checks_the_bank_master_for_the_short_name(file_first):
	_, args = file_first
	frappe.db.sql.side_effect = lambda _query, params: [("USD", None)] if params["account"] == "万里汇" else []
	args.update(account="拉丁购万里汇", content_base64=statement("日期,收入\n2026-07-23,$10"))
	conflict = api.preview_import(**args)
	assert not conflict["preview_id"] and not conflict["book_settings"]["currency"]
	assert conflict["book_issues"] == [{"field":"currency", "message":"请选择账簿币种。"}]


def test_prefixed_account_from_file_keeps_its_bank_number_when_checking_the_short_name(file_first):
	_, args = file_first
	frappe.db.sql.side_effect = lambda _query, params: [("MXN", "000999")] if params["account"] == "万里汇" else []
	args["content_base64"] = statement("银行账户名称,拉丁购万里汇\n本方账号,000123\n日期,收入\n2026-07-23,$10")
	preview = api.preview_import(**args)
	assert not preview["preview_id"] and not preview["book_settings"]["currency"]


def test_cleared_currency_stays_pending_until_operator_selects_a_currency(file_first):
	_, args = file_first
	args["content_base64"] = statement("日期,收入\n2026-07-23,$10")
	assert api.preview_import(**args)["currency"] == "MXN"
	missing = api.preview_import(**args, currency="")
	assert not missing["preview_id"] and not missing["book_settings"]["currency"]
	assert missing["book_issues"] == [{"field":"currency", "message":"请选择账簿币种。"}]
	selected = api.preview_import(**args, currency="USD")
	assert selected["currency"] == "USD" and selected["preview_id"]
