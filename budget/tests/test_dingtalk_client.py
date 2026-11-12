from __future__ import annotations

import pytest
import requests

from budget.integrations.dingtalk_client import (
	CircuitOpenError,
	DingTalkAPIError,
	DingTalkClient,
	DingTalkConfig,
	ProcessTokenCache,
)


class FakeResponse:
	def __init__(self, payload: object, status_code: int = 200):
		self._payload = payload
		self.status_code = status_code
		self.text = "QpsLimitForApi" if status_code == 429 else ""

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


def build_client(responses: list, **overrides) -> tuple[DingTalkClient, FakeSession]:
	session = FakeSession(responses)
	config = DingTalkConfig(app_key="key", app_secret="secret", request_delay_ms=0, **overrides)
	return DingTalkClient(config, session=session, token_cache=ProcessTokenCache()), session


def test_access_token_is_cached_between_calls() -> None:
	client, session = build_client(
		[FakeResponse({"errcode": 0, "access_token": "tok-1", "expires_in": 7200})]
	)

	assert client.get_access_token() == "tok-1"
	assert client.get_access_token() == "tok-1"
	assert len(session.calls) == 1


def test_access_token_failure_raises() -> None:
	client, _ = build_client([FakeResponse({"errcode": 40001, "errmsg": "invalid credential"})])

	with pytest.raises(DingTalkAPIError):
		client.get_access_token()


def test_qps_limit_is_retried() -> None:
	client, session = build_client(
		[
			FakeResponse({"code": "Forbidden.AccessDenied.QpsLimitForApi"}, 429),
			FakeResponse({"errcode": 0, "access_token": "tok", "expires_in": 7200}),
		],
		max_retries=1,
	)

	assert client.get_access_token() == "tok"
	assert len(session.calls) == 2


def test_circuit_opens_after_repeated_network_failures() -> None:
	client, session = build_client(
		[requests.ConnectionError("boom") for _ in range(3)],
		max_retries=0,
	)

	for _ in range(3):
		with pytest.raises(DingTalkAPIError):
			client.get_access_token()

	assert client._circuits["token"].state == "OPEN"
	with pytest.raises(CircuitOpenError):
		client.get_access_token()
	assert len(session.calls) == 3


def test_circuit_can_be_disabled() -> None:
	client, session = build_client(
		[requests.ConnectionError("boom") for _ in range(4)],
		max_retries=0,
		circuit_enabled=False,
	)

	for _ in range(4):
		with pytest.raises(DingTalkAPIError):
			client.get_access_token()

	assert len(session.calls) == 4


def test_send_markdown_reports_partial_failure() -> None:
	client, session = build_client(
		[
			FakeResponse({"errcode": 0, "access_token": "robot-tok", "expires_in": 7200}),
			FakeResponse(
				{"processQueryKey": "q1", "invalidStaffIdList": ["u2"], "flowControlledStaffIdList": []}
			),
		],
		robot_app_key="robot-key",
		robot_app_secret="robot-secret",
		robot_code="robot-code",
	)

	result = client.send_markdown(["u1", "u2"], "标题", "正文")

	assert result.success is False
	assert result.invalid_staff_ids == ["u2"]
	assert session.calls[-1]["headers"]["x-acs-dingtalk-access-token"] == "robot-tok"
