# budget 文档索引

> 阅读顺序：先看模块 README 的「结论先行」，再看 workflow 的时序图，最后才翻代码。
> 判断依据都在代码里，本文档只负责指路和记录「为什么这么做」。

## 模块

| 模块 | 文档 | 状态 |
| --- | --- | --- |
| 审批对接（approval connector） | [modules/approval-connector/README.md](modules/approval-connector/README.md) | 只读链路已实现 |
| 同上 · 时序与数据流 | [modules/approval-connector/workflow.md](modules/approval-connector/workflow.md) | — |

## 一句话定位

`budget` 是预算域的独立 app：**数据由上游 `yueweiit/dingtalk-oa` 拉取，本 app 只消费**，
保留预算台账与额度同步服务，并承载 CN 预算/运营/采购只读审批工作台。
新版 CN 复用 `deeplinkerp_branding` 的共享审批 HTTP 客户端、字段及分类工具，投影、权限与页面在 Budget 内实现；不复制 MX 会计口径。详见 [工作台入口与口径](../README.md#cn-只读审批工作台2026-11-20)。

## 开发约定

| 项 | 约定 |
| --- | --- |
| 分层 | `api/` 只做参数入口 + 权限；判定逻辑放 `services/`；IO 放 `integrations/` |
| 命名 | 只读接口 `get_` / `list_` 前缀；服务文件 `*_service.py` |
| 缩进 | tab，行宽 110（与 frappe 上游一致） |
| 格式化 | **不要对 `apps/` 跑 `ruff format`**：ruff 0.17.4 在 `target-version = "py314"` 下会把 `except (A, B):` 格式化成非法语法。只跑 `ruff check`，需要格式化时加 `--target-version py313` |
| 测试 | `PYTHONPATH=. pytest -q budget/tests`，全部靠注入替身，不连站点/库/网络 |
| 改表结构 | 写 patch 并登记 `budget/patches.txt`，禁止直接 `ALTER TABLE` |
