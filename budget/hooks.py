app_name = "budget"
app_title = "Budget"
app_publisher = "yuewei"
app_description = "预算与费用审批：钉钉开放平台接口 + OA 只读库查询"
app_email = "308642281@qq.com"
app_license = "mit"

boot_session = ["budget.setup.boot_session", "budget.expense_navigation.boot"]

app_include_js = [
	"/assets/budget/js/budget_navigation.js",
	"/assets/budget/js/cn_budget.js",
	"/assets/budget/js/budget_ledger_sync.js?v=0.0.1",
]

# 本 app 读钉钉 / OA 一侧的数据写入本地账本（Budget Ledger Entry / Detail），
# 不接管任何 DocType 事件，也不改上游 app。同步的触发方式见文件末尾：
# 站点开着 scheduler 时走 daily_long；站点 enable_scheduler=0 时由外部 cron 调
# budget.services.oa_budget_sync.scheduled_sync，两者链路完全一致（含每日整表对账）。

# 定制入口独立于 ERPNext 原生 Budget；同步 CN 兼容入口和统一支出管理入口。
# 统一入口按原生 Page 权限显示，不迁移或删除预算、审批业务数据。
after_install = ["budget.setup.apply_budget_nav", "budget.expense_navigation.install"]
after_migrate = ["budget.setup.apply_budget_nav", "budget.expense_navigation.install"]
before_uninstall = ["budget.expense_navigation.uninstall", "budget.setup.restore_budget_nav"]

# 只读 PG 连接按请求复用，响应返回后统一关闭，避免连接泄漏（见 integrations/pg.py）
after_request = ["budget.integrations.pg.close_all"]

# 每天从钉钉 OA 归档解析预算单并写入 Budget Ledger Entry / Budget Ledger Detail；
# 共用 OA 来源在同一事务内更新 Budget Quota / Budget Quota Subject。
# 需要立刻更新时分别调 budget.api.budget_ledger.sync_budget_entries 与
# budget.services.quota_sync.run_quota_sync。
scheduler_events = {
	"cron": {"*/15 * * * *": ["budget.services.cn_approval_sync.scheduled_sync"]},
	"daily_long": [
		"budget.services.budget_ledger_sync.sync_budget_entries",
	],
}

# 站点关闭 scheduler（enable_scheduler: 0）时 daily_long 不会触发，此时由外部定时器
# 调 scheduled_sync（部署侧的 ofelia 覆盖层见 frappe_docker 仓库
# overrides/compose.budget-sync-cron.yaml）。同步入口均检查配置和启用状态；
# 台账页面只读，不再因打开页面而自动入队。
# CN 审批工作台独立使用上述 cron；页面可异步提交过期快照更新。
