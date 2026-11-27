from types import SimpleNamespace
from unittest.mock import patch

import pytest

from budget.api import bank_journal_actions as api
from budget.tests.test_bank_journal_oa import application


@pytest.fixture()
def action(monkeypatch):
	from frappe.utils import password

	doc = SimpleNamespace(name="book-a", account="account-a", company="company-a", revision=1)
	row = api.oa.candidate(application(), "2026-11-02")
	monkeypatch.setattr(api.frappe, "session", SimpleNamespace(user="journal-tester"))
	monkeypatch.setattr(api.frappe.utils, "today", lambda: "2026-11-26")
	monkeypatch.setattr(api.frappe, "throw", lambda msg, *a: (_ for _ in ()).throw(ValueError(msg)))
	monkeypatch.setattr(password, "get_encryption_key", lambda: b"synthetic")
	with (
		patch.object(api.store, "authorize_book", return_value=doc),
		patch.object(api.oa, "month_candidates", side_effect=lambda *_: [dict(row)]),
		patch.object(api.store, "book_ref_rows", return_value=[]) as existing,
		patch.object(api.store, "known_source_keys", return_value=set()),
		patch.object(api.store, "apply_batch", return_value={"created": 1}) as write,
		patch.object(api.store, "batch_replay", return_value=None) as replay,
	):
		yield doc, row, existing, write, replay


def test_month_snapshot_confirm_and_retry(action):
	doc, row, existing, write, replay = action
	view = api.preview_sync(doc.name)
	selection = [dict(source_key=row["source_key"], date="2026-11-04", expense="25.40")]
	args = (doc.name, view["month"], view["preview_id"], 1, "request-101", selection)
	assert api.apply_sync(*args)["created"] == 1
	assert write.call_args.args[1][0]["expense"] == "25.40"
	replay.return_value = {"created": 1}
	row["summary"] = "changed at source after successful import"
	assert api.apply_sync(*args)["created"] == 1
	write.assert_called_once()


def test_stale_changed_company_or_duplicated_reference_blocks(action):
	doc, row, existing, write, _ = action
	view = api.preview_sync(doc.name)
	selection = [dict(source_key=row["source_key"], date="2026-11-04", expense="25")]

	def apply():
		api.apply_sync(doc.name, view["month"], view["preview_id"], 1, "request-102", selection)

	doc.company = "company-b"
	with pytest.raises(ValueError):
		apply()
	doc.company = "company-a"
	existing.return_value = [{"refs": row["refs"]}]
	recorded = api.preview_sync(doc.name)["rows"][0]
	assert recorded["already_recorded"] and not recorded["selectable"]
	with pytest.raises(ValueError):
		apply()
	existing.return_value = []
	row["cashier_date"] = "2026-11-03"
	with pytest.raises(ValueError, match="原单或账簿已变化"):
		apply()
	row["cashier_date"] = None
	row["application_amount"] = "20.00"
	with pytest.raises(ValueError):
		apply()
	write.assert_not_called()


def test_foreign_book_cannot_sync_cny_approvals_even_by_replaying_a_receipt(action):
	doc, row, _, write, replay = action
	doc.currency = "MXN"
	replay.return_value = {"created": 1}
	with pytest.raises(ValueError, match="CNY"):
		api.preview_sync(doc.name)
	with pytest.raises(ValueError, match="CNY"):
		api.apply_sync(doc.name, "2026-11", "token", 1, "replay-import", [
			dict(source_key=row["source_key"], date="2026-11-04", expense="25.40")])
	api.oa.month_candidates.assert_not_called()
	write.assert_not_called()
	replay.assert_not_called()
