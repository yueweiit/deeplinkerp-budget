from __future__ import annotations

import pytest

from budget.integrations.budget_read_api_client import (
	BudgetReadAPIClient,
	BudgetReadAPIConfig,
	BudgetReadAPIError,
	BudgetReadRateLimited,
)

CONFIG = BudgetReadAPIConfig(
	base_url="https://oa.example", corp_id="ding-1", max_retries=2, retry_backoff_ms=10
)


class FakeResponse:
	def __init__(self, payload: dict, *, status_code: int = 200, headers: dict | None = None):
		self._payload = payload
		self.status_code = status_code
		self.headers = headers or {}

	def json(self):
		return self._payload


class FakeSession:
	def __init__(self, responses: list[FakeResponse]):
		self.responses = list(responses)
		self.calls: list[tuple] = []

	def request(self, method, url, params=None, timeout=None):
		self.calls.append((method, url, dict(params or {}), timeout))
		return self.responses.pop(0)


def build_client(responses: list[FakeResponse], *, sleeps: list[float] | None = None):
	session = FakeSession(responses)
	sleeps = sleeps if sleeps is not None else []
	return BudgetReadAPIClient(CONFIG, session=session, sleep=sleeps.append), session, sleeps


def test_entry_query_maps_snake_case_to_camel_case() -> None:
	client, session, _ = build_client(
		[FakeResponse({"success": True, "data": {"rows": [], "total": 0}})]
	)

	client.list_budget_entries(
		budget_type="production",
		start_month="2026-01",
		end_month="2026-12",
		keyword="采购",
		page=2,
		page_size=20,
	)

	method, url, params, _timeout = session.calls[0]
	assert method == "GET"
	assert url == "https://oa.example/api/v1/budget/entries"
	assert params["corpId"] == "ding-1"
	assert params["budgetType"] == "production"
	assert params["startMonth"] == "2026-01"
	assert params["endMonth"] == "2026-12"
	assert params["keyword"] == "采购"
	assert params["page"] == 2
	assert params["pageSize"] == 20


def test_empty_filters_are_dropped_from_query_string() -> None:
	client, session, _ = build_client([FakeResponse({"success": True, "data": []})])

	client.load_remarks([])

	assert "formNos" not in session.calls[0][2]


def test_form_nos_are_deduplicated_and_joined() -> None:
	client, session, _ = build_client([FakeResponse({"success": True, "data": {}})])

	client.load_remarks(["F-1", "F-1", "F-2", ""])

	assert session.calls[0][2]["formNos"] == "F-1,F-2"


def test_rate_limit_is_retried_with_retry_after() -> None:
	client, session, sleeps = build_client(
		[
			FakeResponse({"success": False, "code": "RATE_LIMITED"}, status_code=429, headers={"Retry-After": "2"}),
			FakeResponse({"success": True, "data": {"production_today": 1}}),
		]
	)

	data = client.get_budget_stats()

	assert data == {"production_today": 1}
	assert sleeps == [2.0]
	assert len(session.calls) == 2


def test_failed_response_raises_with_code() -> None:
	client, _, _ = build_client(
		[FakeResponse({"success": False, "code": "READ_UNAVAILABLE"}, status_code=503)]
	)

	with pytest.raises(BudgetReadAPIError) as excinfo:
		client.get_quota_options()

	assert excinfo.value.code == "READ_UNAVAILABLE"
	assert excinfo.value.status_code == 503


def test_rate_limit_raises_after_retries_exhausted() -> None:
	client, session, sleeps = build_client(
		[
			FakeResponse({"success": False, "code": "RATE_LIMITED"}, status_code=429),
			FakeResponse({"success": False, "code": "RATE_LIMITED"}, status_code=429),
			FakeResponse({"success": False, "code": "RATE_LIMITED"}, status_code=429),
		]
	)

	with pytest.raises(BudgetReadRateLimited):
		client.get_quota_options()

	assert len(session.calls) == 3
	assert len(sleeps) == 2


def test_department_snapshot_query_passes_corp_and_ids() -> None:
	client, session, _ = build_client([FakeResponse({"success": True, "data": []})])

	client.list_department_snapshot_rows(["d1", "d2"])

	assert session.calls[0][2] == {"corpId": "ding-1", "deptIds": "d1,d2"}


def test_missing_base_url_is_rejected_at_construction() -> None:
	with pytest.raises(BudgetReadAPIError):
		BudgetReadAPIClient(BudgetReadAPIConfig(base_url=""))
