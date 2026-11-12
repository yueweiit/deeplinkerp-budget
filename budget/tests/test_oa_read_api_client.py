from __future__ import annotations

import pytest

from budget.integrations.oa_read_api_client import (
	OAInvalidCursor,
	OARateLimited,
	OAReadAPIClient,
	OAReadAPIConfig,
	OAReadAPIError,
)


class FakeResponse:
	def __init__(self, payload: object, status_code: int = 200, headers: dict | None = None):
		self._payload = payload
		self.status_code = status_code
		self.headers = headers or {}

	def json(self) -> object:
		if isinstance(self._payload, Exception):
			raise self._payload
		return self._payload


class FakeSession:
	def __init__(self, responses: list):
		self.responses = list(responses)
		self.calls: list[dict] = []

	def request(self, method: str, url: str, **kwargs) -> FakeResponse:
		self.calls.append({"method": method, "url": url, **kwargs})
		item = self.responses.pop(0)
		if isinstance(item, Exception):
			raise item
		return item


def build_client(responses: list, **overrides) -> tuple[OAReadAPIClient, FakeSession, list[float]]:
	session = FakeSession(responses)
	sleeps: list[float] = []
	config = OAReadAPIConfig(base_url="https://oa.example", **overrides)
	client = OAReadAPIClient(config, session=session, sleep=sleeps.append)
	return client, session, sleeps


def approval_payload(rows: list[dict], *, has_more: bool = False, cursor: str | None = None) -> dict:
	return {
		"success": True,
		"data": rows,
		"pagination": {
			"limit": 50,
			"hasMore": has_more,
			"nextCursor": cursor,
			"syncUntil": "2026-10-29T02:00:00.654321Z",
		},
	}


def test_list_approvals_parses_pagination_and_filters() -> None:
	client, session, _ = build_client([FakeResponse(approval_payload([{"processInstanceId": "inst-1"}]))])

	page = client.list_approvals("corp-1", process_code="PROC-1", updated_after="2026-10-28T00:00:00+08:00")

	assert page.rows == [{"processInstanceId": "inst-1"}]
	assert page.sync_until == "2026-10-29T02:00:00.654321Z"
	assert page.has_more is False
	params = session.calls[0]["params"]
	assert params == {
		"corpId": "corp-1",
		"limit": 50,
		"processCode": "PROC-1",
		"updatedAfter": "2026-10-28T00:00:00+08:00",
	}


def test_list_approvals_clamps_limit_and_omits_empty_filters() -> None:
	client, session, _ = build_client([FakeResponse(approval_payload([]))])

	client.list_approvals("corp-1", process_code="", limit=500)

	params = session.calls[0]["params"]
	assert params == {"corpId": "corp-1", "limit": 200}


def test_rate_limit_is_retried_after_retry_after_header() -> None:
	client, session, sleeps = build_client(
		[
			FakeResponse({"success": False, "code": "RATE_LIMITED"}, 429, {"Retry-After": "2"}),
			FakeResponse(approval_payload([])),
		],
		max_retries=1,
	)

	client.list_approvals("corp-1")

	assert len(session.calls) == 2
	assert sleeps == [2.0]


def test_rate_limit_raises_when_retries_exhausted() -> None:
	client, _, _ = build_client(
		[FakeResponse({"success": False, "code": "RATE_LIMITED"}, 429, {"Retry-After": "3"})] * 2,
		max_retries=1,
	)

	with pytest.raises(OARateLimited) as exc_info:
		client.list_approvals("corp-1")

	assert exc_info.value.retry_after == 3.0


def test_invalid_cursor_is_distinguished_for_full_rescan() -> None:
	client, _, _ = build_client([FakeResponse({"success": False, "code": "INVALID_CURSOR"}, 400)])

	with pytest.raises(OAInvalidCursor):
		client.list_approvals("corp-1", cursor="stale")


def test_other_errors_keep_stable_code() -> None:
	client, _, _ = build_client([FakeResponse({"success": False, "code": "READ_UNAVAILABLE"}, 503)])

	with pytest.raises(OAReadAPIError) as exc_info:
		client.list_approvals("corp-1")

	assert exc_info.value.code == "READ_UNAVAILABLE"
	assert exc_info.value.status_code == 503


def test_iter_pages_follows_cursor_until_last_page() -> None:
	client, session, _ = build_client(
		[
			FakeResponse(approval_payload([{"processInstanceId": "a"}], has_more=True, cursor="c1")),
			FakeResponse(approval_payload([{"processInstanceId": "b"}])),
		]
	)

	pages = list(client.iter_approval_pages("corp-1", limit=1))

	assert [row["processInstanceId"] for page in pages for row in page.rows] == ["a", "b"]
	assert session.calls[0]["params"].get("cursor") is None
	assert session.calls[1]["params"]["cursor"] == "c1"


def test_get_approval_returns_none_when_filters_match_nothing() -> None:
	client, session, _ = build_client([FakeResponse(approval_payload([]))])

	assert client.get_approval("corp-1", "inst-1") is None
	assert session.calls[0]["params"]["processInstanceId"] == "inst-1"
	assert session.calls[0]["params"]["limit"] == 1


def test_missing_base_url_fails_fast() -> None:
	with pytest.raises(OAReadAPIError):
		OAReadAPIClient(OAReadAPIConfig(base_url=""), session=FakeSession([]))
