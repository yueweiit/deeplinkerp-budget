app_name = "budget"
app_title = "Budget"
app_publisher = "yuewei"
app_description = "预算与费用审批：钉钉开放平台接口 + OA 只读库查询"
app_email = "308642281@qq.com"
app_license = "mit"

# 本 app 目前只读钉钉与 OA 一侧的数据，不接管任何 DocType 事件，也不改上游 app。
# 写入侧账本（DocType）与增量同步任务在第二阶段落地，届时在此登记 doc_events / scheduler_events。
# 声明未实现的函数会让 scheduler 每轮报错，所以先把 cron 留空。

# ERPNext 的 Workspace Sidebar 每次 migrate 都会按 json 重同步，
# 所以用 after_migrate 重放「预算台账 / 预算数据看板 / 预算配置」这三个入口。
# install-app 只装代码、不跑 migrate，因此 after_install 也要补一次，否则新装的站点
# 侧边栏里看不到任何入口（Page 记录也要等 migrate 才同步，点进去会是空白页）。
after_install = ["budget.setup.ensure_budget_sidebar_link"]
after_migrate = ["budget.setup.ensure_budget_sidebar_link"]

# 只读 PG 连接按请求复用，响应返回后统一关闭，避免连接泄漏（见 integrations/pg.py）
after_request = ["budget.integrations.pg.close_all"]

# 外部 budget_system 的预算单每天同步一次到 Budget Ledger Entry；
# 需要立刻更新时调 budget.api.budget_ledger.sync_budget_entries。
scheduler_events = {
	"daily_long": ["budget.services.budget_ledger_sync.sync_budget_entries"],
}

# scheduler_events 示例（第二阶段启用）：
# scheduler_events = {
# 	"cron": {
# 		"*/10 * * * *": ["budget.tasks.sync_approval_instances.sync_recent_approvals"],
# 	},
# }
