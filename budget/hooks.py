app_name = "budget"
app_title = "Budget"
app_publisher = "yuewei"
app_description = "预算与费用审批：钉钉开放平台接口 + OA 只读库查询"
app_email = "308642281@qq.com"
app_license = "mit"

# 本 app 读钉钉 / OA 一侧的数据写入本地账本（Budget Ledger Entry / Detail），
# 不接管任何 DocType 事件，也不改上游 app。同步的触发方式见文件末尾：
# 站点开着 scheduler 时走 daily_long；站点 enable_scheduler=0 时由外部 cron 调
# budget.services.oa_budget_sync.scheduled_sync，两者链路完全一致（含每日整表对账）。

# ERPNext 的 Workspace Sidebar / Desktop Icon 每次 migrate 都会按 json 重同步，
# 所以用 after_migrate 重放：5 个原生入口收进「自定义」分组、追加本 app 的 3 个入口、
# 并把 Desktop Icon「Budget」提到顶级。
# install-app 只装代码、不跑 migrate，因此 after_install 也要补一次，否则新装的站点
# 侧边栏里看不到任何入口（Page 记录也要等 migrate 才同步，点进去会是空白页）。
# 卸载时把导航还原成 ERPNext 原生形态，避免留下指向已删 DocType/Page 的死链接。
after_install = ["budget.setup.apply_budget_nav"]
after_migrate = ["budget.setup.apply_budget_nav"]
before_uninstall = ["budget.setup.restore_budget_nav"]

# 只读 PG 连接按请求复用，响应返回后统一关闭，避免连接泄漏（见 integrations/pg.py）
after_request = ["budget.integrations.pg.close_all"]

# 每天从钉钉 OA 归档解析预算单并写入 Budget Ledger Entry / Budget Ledger Detail；
# 同时把外部 budget_system 的预算额度同步成本地 Budget Quota / Budget Quota Subject。
# 需要立刻更新时分别调 budget.api.budget_ledger.sync_budget_entries 与
# budget.services.quota_sync.run_quota_sync。
scheduler_events = {
	"daily_long": [
		"budget.services.budget_ledger_sync.sync_budget_entries",
		"budget.services.quota_sync.sync_quotas",
	],
}

# 站点关闭 scheduler（enable_scheduler: 0）时 daily_long 不会触发，此时由外部定时器
# 调 scheduled_sync（部署侧的 ofelia 覆盖层见 frappe_docker 仓库
# overrides/compose.budget-sync-cron.yaml）。台账列表页打开时还会调
# ensure_ledger_fresh 按需补一轮，所以定时间隔只需当兜底。
