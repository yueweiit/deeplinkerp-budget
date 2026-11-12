from __future__ import annotations

from budget.services import approval_watermark_service as service


def test_next_watermark_rewinds_ten_minutes_by_default() -> None:
	assert service.compute_next_watermark("2026-10-29T02:00:00.654321Z") == "2026-10-29T01:50:00.654321+00:00"


def test_next_watermark_keeps_original_offset() -> None:
	assert service.compute_next_watermark("2026-10-29T10:00:00+08:00") == "2026-10-29T09:50:00+08:00"


def test_next_watermark_supports_custom_overlap() -> None:
	assert service.compute_next_watermark("2026-10-29T02:00:00Z", overlap_minutes=0) == (
		"2026-10-29T02:00:00+00:00"
	)


def test_next_watermark_returns_none_for_invalid_input() -> None:
	assert service.compute_next_watermark("not-a-time") is None
	assert service.compute_next_watermark(None) is None


def test_next_watermark_returns_none_without_timezone() -> None:
	assert service.compute_next_watermark("2026-10-29T02:00:00") is None


def test_updated_after_must_carry_timezone() -> None:
	assert service.is_valid_updated_after("2026-10-28T00:00:00+08:00") is True
	assert service.is_valid_updated_after("2026-10-28T00:00:00Z") is True
	assert service.is_valid_updated_after("2026-10-28T00:00:00") is False
	assert service.is_valid_updated_after("2026-10-28 00:00:00+08:00") is True
