"""增量水位计算（纯逻辑）。

上游契约（`dingtalk-oa/docs/modules/approval-read-api/README.md:150-157`）：

- 只有整轮成功后才把本轮 ``syncUntil`` 存为完成水位；
- 下一轮 ``updatedAfter`` 取该水位减去**至少 10 分钟**的重叠区间，按主键幂等覆盖；
- 不要只保存某一页的最大更新时间，也不要用 ``last_event_time`` 当水位。

本模块只做时间换算，不做 IO，可直接单测。
"""

from __future__ import annotations

import datetime
from typing import Any

DEFAULT_OVERLAP_MINUTES = 10


def parse_timestamp(value: Any) -> datetime.datetime | None:
	"""解析接口返回的 ISO 时间；无时区或格式非法返回 None。"""
	text = str(value or "").strip()
	if not text:
		return None
	normalized = text[:-1] + "+00:00" if text.endswith(("Z", "z")) else text
	try:
		parsed = datetime.datetime.fromisoformat(normalized)
	except ValueError:
		return None
	return parsed if parsed.tzinfo is not None else None


def compute_next_watermark(sync_until: Any, *, overlap_minutes: int = DEFAULT_OVERLAP_MINUTES) -> str | None:
	"""把上一轮 ``syncUntil`` 换算成下一轮的 ``updatedAfter``（回退重叠窗口）。

	``syncUntil`` 无效时返回 None，表示应从头重读（接口不传 ``updatedAfter``）。
	"""
	parsed = parse_timestamp(sync_until)
	if parsed is None:
		return None
	overlap = max(0, int(overlap_minutes))
	return (parsed - datetime.timedelta(minutes=overlap)).isoformat()


def is_valid_updated_after(value: Any) -> bool:
	"""``updatedAfter`` 必须带时区，否则接口在访问数据库前就返回 400。"""
	return parse_timestamp(value) is not None
