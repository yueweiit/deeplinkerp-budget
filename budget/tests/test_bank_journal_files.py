import base64
from types import SimpleNamespace
from unittest.mock import Mock

import frappe
import pytest

from budget.api import bank_journal_files as api


@pytest.fixture
def book(monkeypatch):
	doc = SimpleNamespace(
		name="book", company="Example", account="Cash", revision=4, opening_date="2026-01-01"
	)
	store = SimpleNamespace(
		authorize_book=Mock(return_value=doc), apply_batch=Mock(return_value={"added": 1})
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
