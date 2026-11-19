(function () {
const ECHARTS_URL = "/assets/budget/js/echarts.min.js";
const CHART_COLORS = {
	production: "#2563eb",
	nonProduction: "#0f766e",
	total: "#b45309",
	expense: "#7c3aed",
	regionBudget: "#5470c6",
	regionExpense: "#b7d92d",
	executed: "#52c41a",
	inProgress: "#faad14",
	unexecuted: "#d9d9d9",
	rateDanger: "#dc2626",
	rateWarning: "#b45309",
	rateOk: "#0f766e",
};
const AXIS_LABEL = { fontSize: 12, color: "#6b7280" };
const LEGEND = { bottom: 0, itemWidth: 12, itemHeight: 12, textStyle: { fontSize: 12, color: "#374151" } };

let echarts_promise = null;
function load_echarts() {
	if (window.echarts) return Promise.resolve(window.echarts);
	if (!echarts_promise) {
		echarts_promise = new Promise((resolve, reject) => {
			frappe.require([ECHARTS_URL], () => {
				window.echarts ? resolve(window.echarts) : reject(new Error("ECharts 未加载"));
			});
		});
	}
	return echarts_promise;
}

function format_currency(value) {
	const num = Number(value || 0);
	if (Math.abs(num) >= 10000) return `${(num / 10000).toFixed(1)}万`;
	return num.toLocaleString("zh-CN");
}

function truncate_label(label, max = 10) {
	const text = String(label || "");
	return text.length > max ? `${text.slice(0, max)}...` : text;
}

const TOOLTIP = {
	backgroundColor: "#fff",
	borderColor: "#e5e7eb",
	borderWidth: 1,
	textStyle: { color: "#374151", fontSize: 13 },
	extraCssText: "box-shadow: 0 4px 6px rgba(15, 23, 42, 0.1); border-radius: 6px;",
};

function bar_series(name, data, color, options = {}) {
	return {
		name,
		type: "bar",
		data,
		barSize: options.barSize,
		stack: options.stack,
		itemStyle: { color, borderRadius: options.borderRadius || 0 },
		emphasis: { focus: "series" },
	};
}

function value_axis(formatter = format_currency, grid_color = "#f3f4f6") {
	return {
		type: "value",
		axisLabel: { ...AXIS_LABEL, formatter },
		axisLine: { show: false },
		axisTick: { show: false },
		splitLine: { lineStyle: { type: "dashed", color: grid_color } },
	};
}

class BudgetDashboard {
	constructor(wrapper, embedded = null) {
		this.embedded = embedded;
		this.page = embedded ? {main: $(embedded.parent)} : frappe.ui.make_app_page({
			parent: wrapper,
			title: __("Budget Execution Analysis"),
			single_column: true,
		});
		this.charts = [];
		this.request_id = 0;
		if (!embedded) this.setup_filters();
		this.setup_content();
		this.install_styles();
		if (!embedded) this.page.set_primary_action(__("刷新数据"), () => this.load(), "refresh");
		$(window).on("resize.budget-dashboard", () => this.resize_charts());
		if (!embedded) this.load();
	}

	setup_filters() {
		const today = frappe.datetime.get_today();
		this.start_date = this.page.add_field({
			fieldname: "start_date",
			label: __("开始日期"),
			fieldtype: "Date",
			default: frappe.datetime.month_start(today),
			onchange: () => this.load(),
		});
		this.end_date = this.page.add_field({
			fieldname: "end_date",
			label: __("结束日期"),
			fieldtype: "Date",
			default: frappe.datetime.month_end(today),
			onchange: () => this.load(),
		});
	}

	setup_content() {
		this.$content = $(`
			<div class="budget-report">
				<div class="br-message" role="alert" hidden></div>
				<div class="br-scope" hidden></div>

				<div class="br-stats">
					${[
						["productionCount", "生产预算明细数"],
						["operationCount", "非生产预算明细数"],
						["productionTotal", "生产预算总金额"],
						["nonProductionTotal", "非生产预算总金额"],
						["approvedTotal", "实际支出合计"],
						["budgetSubmittedApprovedTotal", "有提交预算部门支出合计"],
						["overallRate", "整体执行率"],
					]
						.map(
							([key, label]) => `<div class="br-card">
								<div class="br-value" data-stat="${key}">—</div>
								<div class="br-label" data-stat-label="${key}">${__(label)}</div>
							</div>`
						)
						.join("")}
				</div>

				<div class="br-block br-approval">
					<div class="br-block-head">
						<strong>${__("审批效率")}</strong>
						<span class="text-muted br-timeout"></span>
					</div>
					<div class="br-stats br-stats-sm">
						${[
							["totalInstances", "审批实例数"],
							["averageCompletionHours", "平均完成时长（小时）"],
							["runningInstances", "审批中"],
							["taskOverdueRate", "节点超时率"],
							["averageTaskHours", "平均节点耗时（小时）"],
						]
							.map(
								([key, label]) => `<div class="br-card br-card-sm">
									<div class="br-value" data-approval="${key}">—</div>
									<div class="br-label">${__(label)}</div>
								</div>`
							)
							.join("")}
					</div>
					<div class="br-table-wrap">
						<table class="br-table br-approval-table">
							<thead><tr>
								<th scope="col">${__("流程")}</th>
								<th scope="col" class="num">${__("实例数")}</th>
								<th scope="col" class="num">${__("已完成")}</th>
								<th scope="col" class="num">${__("审批中")}</th>
								<th scope="col" class="num">${__("平均完成时长")}</th>
								<th scope="col" class="num">${__("平均节点耗时")}</th>
								<th scope="col" class="num">${__("超时节点")}</th>
								<th scope="col" class="num">${__("节点超时率")}</th>
							</tr></thead>
							<tbody><tr><td colspan="8" class="br-empty">${__("正在加载…")}</td></tr></tbody>
						</table>
					</div>
				</div>

				<div class="br-charts">
					<div class="br-chart-card">
						<h3 class="br-chart-title">${__("各部门预算分布（Top 12）")}</h3>
						<div class="br-chart" data-chart="dept-dist" style="height: 360px;"></div>
					</div>
					<div class="br-chart-card">
						<h3 class="br-chart-title">${__("地区预算分布")}</h3>
						<div class="br-chart" data-chart="region-dist" style="height: 300px;"></div>
					</div>
					<div class="br-chart-card">
						<h3 class="br-chart-title">${__("预算类型占比")}</h3>
						<div class="br-chart" data-chart="type-share" style="height: 360px;"></div>
					</div>
					<div class="br-chart-card">
						<h3 class="br-chart-title">${__("预算执行状态分布（Top 10）")}</h3>
						<div class="br-chart" data-chart="exec-status" style="height: 360px;"></div>
					</div>
					<div class="br-chart-card br-chart-wide">
						<h3 class="br-chart-title br-trend-title">${__("月度预算趋势")}</h3>
						<div class="br-chart" data-chart="trend" style="height: 360px;"></div>
					</div>
					<div class="br-chart-card">
						<h3 class="br-chart-title">${__("各部门执行率（Top 10）")}</h3>
						<div class="br-chart" data-chart="rate" style="height: 360px;"></div>
					</div>
					<div class="br-chart-card">
						<h3 class="br-chart-title" data-expense-chart-title>${__("部门预算 vs 实际支出（Top 10）")}</h3>
						<div class="br-chart" data-chart="compare" style="height: 360px;"></div>
					</div>
				</div>
			</div>
		`).appendTo(this.page.main);

		this.$message = this.$content.find(".br-message");
		this.$stats = this.$content.find(".br-stats").first();
		this.$approval_body = this.$content.find(".br-approval-table tbody");
	}

	install_styles() {
		if (document.getElementById("budget-report-style")) return;
		$(`<style id="budget-report-style">
			.budget-report { padding: 12px 15px 30px; }
			.br-message { margin-bottom: 12px; padding: 8px 12px; border: 1px solid var(--red-200); border-radius: var(--border-radius); color: var(--red-600); background: var(--red-50); }
			.br-message.br-warning { border-color: var(--orange-200); color: var(--orange-700); background: var(--orange-50); }
			.br-scope { margin-bottom: 12px; font-size: 12px; color: var(--text-muted); }
			.br-stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 12px; margin-bottom: 16px; }
			.br-card { padding: 14px 16px; border: 1px solid var(--border-color); border-radius: var(--border-radius-md, 8px); background: var(--card-bg, var(--fg-color)); }
			.br-card-sm { padding: 10px 14px; }
			.br-value { font-size: 22px; font-weight: 600; line-height: 1.2; color: var(--text-color); font-variant-numeric: tabular-nums; }
			.br-card-sm .br-value { font-size: 18px; }
			.br-label { margin-top: 6px; font-size: 12px; color: var(--text-muted); }
			.br-block { margin-bottom: 16px; }
			.br-block-head { display: flex; align-items: baseline; gap: 12px; margin-bottom: 10px; }
			.br-table-wrap { overflow-x: auto; border: 1px solid var(--border-color); border-radius: var(--border-radius-md, 8px); background: var(--card-bg, var(--fg-color)); }
			.br-table { width: 100%; margin-bottom: 0; font-size: 12px; border-collapse: collapse; }
			.br-table th { background: var(--subtle-fg); color: var(--text-muted); font-weight: 500; white-space: nowrap; }
			.br-table th, .br-table td { padding: 8px 12px; border-bottom: 1px solid var(--border-color); }
			.br-table td { border-top: 0; white-space: nowrap; }
			.br-table .num { text-align: right; font-variant-numeric: tabular-nums; }
			.br-empty { padding: 28px 12px; text-align: center; color: var(--text-muted); font-size: 13px; }
			/* 固定两列：除趋势卡全宽外，其余图一律两图一行 */
			.br-charts { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 16px; }
			.br-chart-card { padding: 15px; border: 1px solid var(--border-color); border-radius: var(--border-radius-md, 8px); background: var(--card-bg, var(--fg-color)); }
			.br-chart-wide { grid-column: 1 / -1; }
			.br-chart-title { margin: 0 0 12px; font-size: 14px; font-weight: 600; color: var(--text-color); }
			.br-chart { width: 100%; }
			@media (max-width: 900px) { .br-charts { grid-template-columns: 1fr; } }
            .cn-expenses .budget-report { padding: 0; }
            .cn-expenses .br-card, .cn-expenses .br-chart-card, .cn-expenses .br-table-wrap { border-color: var(--mx-line); border-radius: 10px; }
            .cn-expenses .br-value { color: var(--mx-ink); }
            .cn-expenses .br-label { color: var(--mx-muted); }
            .cn-expenses .br-stats { grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 16px; }
            .cn-expenses .br-stats-sm { grid-template-columns: repeat(5, minmax(0, 1fr)); }
            @media (max-width: 900px) { .cn-expenses .br-stats { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
            @media (max-width: 480px) { .cn-expenses .br-stats { grid-template-columns: 1fr; } }
		</style>`).appendTo(document.head);
	}

	dispose() {
		this.disposed = true;
		this.request_id++;
		window.DeepLinkERPMexicoExpenseInsights?.dispose?.(this.$content[0]);
		this.destroy_charts();
		$(window).off("resize.budget-dashboard");
	}

	filters() {
		if (this.embedded) return this.embedded.filters();
		return {
			start_date: this.start_date.get_value() || "",
			end_date: this.end_date.get_value() || "",
		};
	}

	show_error(message) {
		this.$message.removeClass("br-warning").text(message || "").prop("hidden", !message);
	}

	show_warning(message) {
		this.$message.addClass("br-warning").text(message || "").prop("hidden", !message);
	}

	load() {
		const request = ++this.request_id;
		this.show_error("");
		this.$approval_body.html(
			`<tr><td colspan="8" class="br-empty">${__("正在加载…")}</td></tr>`
		);
		return frappe
			.call({
				method: "budget.api.report.get_report_data",
				args: this.filters(),
			})
			.then((response) => {
				if (request !== this.request_id) return;
				const data = response.message || {};
				this.render_scope(data);
				this.render_stats(data.stats || {});
				this.render_approval(data.approval || {}, data.availability?.approval !== false);
				this.render_charts(data);
				if ((data.warnings || []).length) {
					this.show_warning(data.warnings.join("；"));
				}
			})
			.catch(() => {
				if (request !== this.request_id) return;
				this.show_error(__("报表加载失败，请检查数据库配置或稍后重试"));
				this.$approval_body.html(
					`<tr><td colspan="8" class="br-empty">${__("加载失败")}</td></tr>`
				);
			});
	}

	render_scope(data) {
		const approved = data.expense_basis === "approved";
		this.$content.find('[data-stat-label="approvedTotal"]').text(approved ? __("Approved Spending Total") : __("实际支出合计"));
		this.$content.find('[data-stat-label="budgetSubmittedApprovedTotal"]').text(approved ? __("Approved Spending of Budgeted Departments") : __("有提交预算部门支出合计"));
		this.$content.find("[data-expense-chart-title]").text(approved ? __("Department Budget vs Approved Spending (Top 10)") : __("部门预算 vs 实际支出（Top 10）"));
		const notice = data.scope_notice || "";
		const $scope = this.$content.find(".br-scope").prop("hidden", !notice);
		const tips = window.DeepLinkERPMexicoExpenseInsights;
		tips?.dispose?.($scope[0]);
		$scope.html(notice ? `${__("Statistical Scope")} ${tips?.help ? tips.help(notice) : `<button type="button" class="btn btn-xs" title="${frappe.utils.escape_html(notice)}" aria-label="${frappe.utils.escape_html(notice)}">?</button>`}` : "");
		tips?.mount?.(this.$content[0]);
	}

	money(value) {
		if (value === null) return "—";
		return Number(value || 0).toLocaleString("zh-CN", {
			minimumFractionDigits: 2,
			maximumFractionDigits: 2,
		});
	}

	render_stats(stats) {
		const moneyKeys = [
			"productionTotal",
			"nonProductionTotal",
			"approvedTotal",
			"budgetSubmittedApprovedTotal",
		];
		const countKeys = ["productionCount", "operationCount"];
		Object.entries(stats).forEach(([key, value]) => {
			const text = moneyKeys.includes(key)
				? this.money(value)
				: countKeys.includes(key)
				? Number(value || 0).toLocaleString("zh-CN")
				: String(value ?? "—");
			// 数值统一使用正文色，不再按指标上色
			this.$content.find(`[data-stat="${key}"]`).text(text);
		});
	}

	render_approval(approval, available = true) {
		this.$content.find("[data-approval]").text("—");
		const summary = approval.summary || {};
		this.$content
			.find(".br-timeout")
			.text(__("超时标准：{0} 小时", [approval.timeoutHours || 24]));
		Object.entries(summary).forEach(([key, value]) => {
			const $target = this.$content.find(`[data-approval="${key}"]`);
			if (!$target.length) return;
			$target.text(
				typeof value === "number"
					? key.endsWith("Rate")
						? `${value}%`
						: Number(value).toLocaleString("zh-CN")
					: String(value ?? "—")
			);
		});

		const rows = approval.byProcess || [];
		if (!rows.length) {
			this.$approval_body.html(
				`<tr><td colspan="8" class="br-empty">${available ? __("当前期间没有审批实例") : __("Approval data unavailable")}</td></tr>`
			);
			return;
		}
		this.$approval_body.html(
			rows
				.map(
					(row) => `<tr>
						<td>${frappe.utils.escape_html(String(row.processName || "-"))}</td>
						<td class="num">${Number(row.totalInstances || 0)}</td>
						<td class="num">${Number(row.completedInstances || 0)}</td>
						<td class="num">${Number(row.runningInstances || 0)}</td>
						<td class="num">${row.averageCompletionHours ?? 0}</td>
						<td class="num">${row.averageTaskHours ?? 0}</td>
						<td class="num">${Number(row.overdueTasks || 0)}</td>
						<td class="num">${row.taskOverdueRate ?? 0}%</td>
					</tr>`
				)
				.join("")
		);
	}

	destroy_charts() {
		this.charts.forEach((chart) => {
			try {
				chart.dispose();
			} catch (error) {
				// 容器被替换后 dispose 会抛错，忽略
			}
		});
		this.charts = [];
	}

	resize_charts() {
		this.charts.forEach((chart) => chart.resize());
	}

	empty_chart(name, message = __("没有数据")) {
		this.$content.find(`[data-chart="${name}"]`).html(`<div class="br-empty">${frappe.utils.escape_html(message)}</div>`);
	}

	mount(echarts, name, option, height) {
		const element = this.$content.find(`[data-chart="${name}"]`).get(0);
		if (!element) return;
		if (height) element.style.height = `${height}px`;
		$(element).empty();
		const chart = echarts.init(element);
		chart.setOption(option);
		this.charts.push(chart);
		return chart;
	}

	/** 部门维度的图：点柱子跳到台账列表，按该部门 + 看板当前日期区间筛选。 */
	bind_department_click(chart, rows) {
		if (!chart) return;
		const element = chart.getDom();
		element.style.cursor = "pointer";
		element.setAttribute("title", __("点击查看该部门的预算单据"));
		chart.on("click", (params) => {
			const row = rows[params.dataIndex];
			if (row) this.open_ledger_entries(row);
		});
	}

	open_ledger_entries(row) {
		if (this.embedded) return this.embedded.openDetails(row);
		const deptId = String(row.deptId || "").trim();
		const deptName = String(row.deptName || "").trim();
		if (!deptId && !deptName) return;

		// ListView.before_refresh 会把 route_options 转成筛选：
		// 单值 = 等值；[[操作符, 值], ...] 展开成多条（见 frappe list_view.js）
		const filters = {};
		if (deptId) {
			filters.department_id = deptId;
		} else {
			filters.department = ["like", `%${deptName}%`];
		}
		const start = this.start_date && this.start_date.get_value();
		const end = this.end_date && this.end_date.get_value();
		if (start && end) {
			filters.budget_month = [["between", [start, end]]];
		} else if (start) {
			filters.budget_month = [[">=", start]];
		} else if (end) {
			filters.budget_month = [["<=", end]];
		}
		frappe.route_options = filters;
		frappe.set_route("List", "Budget Ledger Entry", "List");
	}

	async render_charts(data) {
		const renderId = this.renderId = (this.renderId || 0) + 1;
		this.destroy_charts();
		const expenseAvailable = data.availability?.expenses !== false;
		const trendExpenseAvailable = (data.availability?.trend_expenses ?? expenseAvailable) !== false;
		const expenseLabel = data.expense_basis === "approved" ? __("Approved Spending") : __("实际支出");
		const executedLabel = data.expense_basis === "approved" ? __("Approved Spending") : __("已执行");
		let echarts;
		try {
			echarts = await load_echarts();
			if (this.disposed || renderId !== this.renderId) return;
		} catch (error) {
			["dept-dist", "region-dist", "type-share", "exec-status", "trend", "rate", "compare"].forEach(
				(name) => this.empty_chart(name)
			);
			return;
		}

		const category_axis = (labels, extra = {}) => ({
			type: "category",
			data: labels,
			axisTick: { show: false },
			axisLine: { lineStyle: { color: "#e5e7eb" } },
			...extra,
			// 合并默认字号/颜色，避免 extra 里的 axisLabel 覆盖掉它们
			axisLabel: { ...AXIS_LABEL, ...(extra.axisLabel || {}) },
		});

		// 1) 各部门预算分布（Top 12）：纵向双柱
		const dept_dist = data.dept_distribution || [];
		if (dept_dist.length) {
			const dept_dist_chart = this.mount(echarts, "dept-dist", {
				tooltip: { ...TOOLTIP, trigger: "axis", axisPointer: { type: "shadow" }, valueFormatter: format_currency },
				legend: { ...LEGEND, data: [__("生产预算"), __("非生产预算")] },
				grid: { left: 8, right: 24, top: 10, bottom: 70, containLabel: true },
				xAxis: category_axis(
					dept_dist.map((row) => truncate_label(row.shortName || row.deptName, 10)),
					{ axisLabel: { rotate: -35, interval: 0, height: 70 } }
				),
				yAxis: value_axis(),
				series: [
					bar_series("生产预算", dept_dist.map((row) => row.production || 0), CHART_COLORS.production, {
						borderRadius: [4, 4, 0, 0],
					}),
					bar_series(
						"非生产预算",
						dept_dist.map((row) => row.nonProduction || 0),
						CHART_COLORS.nonProduction,
						{ borderRadius: [4, 4, 0, 0] }
					),
				],
			});
			this.bind_department_click(dept_dist_chart, dept_dist);
		} else {
			this.empty_chart("dept-dist");
		}

		// 2) 地区预算分布：横向柱（高度随条目数，对齐源页面）
		const region_dist = data.region_distribution || [];
		if (region_dist.length) {
			this.$content
				.find('[data-chart="region-dist"]')
				.css("height", `${Math.max(260, region_dist.length * 72)}px`);
			this.mount(echarts, "region-dist", {
				tooltip: { ...TOOLTIP, trigger: "axis", axisPointer: { type: "shadow" }, valueFormatter: format_currency },
				legend: { ...LEGEND, data: expenseAvailable ? [__("预算"), expenseLabel] : [__("预算")] },
				grid: { left: 8, right: 40, top: 10, bottom: 40, containLabel: true },
				xAxis: value_axis(format_currency, "#e5e7eb"),
				yAxis: category_axis(
					region_dist.map((row) => row.region),
					{ axisLabel: { fontSize: 13, color: "#374151" }, inverse: true }
				),
				series: [
					bar_series("预算", region_dist.map((row) => row.budget || 0), CHART_COLORS.regionBudget, {
						barSize: 16,
						borderRadius: [0, 2, 2, 0],
					}),
					...(expenseAvailable ? [bar_series(expenseLabel, region_dist.map((row) => row.expense || 0), CHART_COLORS.regionExpense, {
						barSize: 16,
						borderRadius: [0, 2, 2, 0],
					})] : []),
				],
			});
		} else {
			this.empty_chart("region-dist");
		}

		// 3) 预算类型占比：环形图
		const type_share = (data.type_distribution || []).filter((row) => Number(row.value) > 0);
		if (type_share.length) {
			const palette = [CHART_COLORS.production, CHART_COLORS.nonProduction];
			this.mount(echarts, "type-share", {
				tooltip: { ...TOOLTIP, trigger: "item", formatter: "{b}: {d}%" },
				legend: { ...LEGEND },
				series: [
					{
						type: "pie",
						// 内/外半径与源页面 innerRadius 80 / outerRadius 130（图高 360）等比换算
						radius: ["44%", "72%"],
						center: ["50%", "50%"],
						avoidLabelOverlap: true,
						itemStyle: { borderColor: "#fff", borderWidth: 3 },
						label: {
							color: "#374151",
							fontSize: 12,
							formatter: (params) => `${params.name} ${Number(params.percent || 0).toFixed(1)}%`,
						},
						data: type_share.map((row, index) => ({
							name: row.kind === "production" ? __("生产预算") : __("非生产预算"),
							value: row.value || 0,
							itemStyle: { color: palette[index % palette.length] },
						})),
					},
				],
			});
		} else {
			this.empty_chart("type-share");
		}

		// 4) 预算执行状态分布：横向堆叠柱（高度随条目数，对齐源页面）
		const exec_status = data.execution_status || [];
		if (exec_status.length) {
			this.$content
				.find('[data-chart="exec-status"]')
				.css("height", `${Math.max(280, exec_status.length * 48)}px`);
			this.mount(echarts, "exec-status", {
				tooltip: { ...TOOLTIP, trigger: "axis", axisPointer: { type: "shadow" }, valueFormatter: format_currency },
				// 图例顺序对齐源页面「审批中 / 已执行 / 未执行」，堆叠顺序仍是已执行→审批中→未执行
				legend: { ...LEGEND, data: [__("审批中"), executedLabel, __("未执行")] },
				grid: { left: 8, right: 24, top: 10, bottom: 40, containLabel: true },
				xAxis: value_axis(),
				yAxis: category_axis(
					exec_status.map((row) => truncate_label(row.shortName || row.deptName, 10)),
					{ axisLabel: { fontSize: 12, color: "#374151" }, inverse: true }
				),
				series: [
					bar_series(executedLabel, exec_status.map((row) => row.executed || 0), CHART_COLORS.executed, {
						barSize: 20,
						stack: "a",
					}),
					bar_series("审批中", exec_status.map((row) => row.inProgress || 0), CHART_COLORS.inProgress, {
						barSize: 20,
						stack: "a",
					}),
					bar_series("未执行", exec_status.map((row) => row.unexecuted || 0), CHART_COLORS.unexecuted, {
						barSize: 20,
						stack: "a",
						borderRadius: [0, 4, 4, 0],
					}),
				],
			});
		} else {
			this.empty_chart("exec-status");
		}

		// 5) 月度预算趋势（全年）：平滑折线 + 空心圆点
		const trend = data.trend || [];
		if (trend.length) {
			const same_year =
				new Set(trend.map((row) => String(row.month || "").slice(0, 4))).size === 1;
			const year = String((trend[0] || {}).month || "").slice(0, 4);
			this.$content.find(".br-trend-title").text(__("月度预算趋势（{0}）", [year || __("不限")]));
			const line = (name, values, color, width) => ({
				name,
				type: "line",
				smooth: true,
				symbol: "emptyCircle",
				symbolSize: width > 2 ? 8 : 7,
				lineStyle: { width, color },
				itemStyle: { color, borderWidth: 2 },
				emphasis: { focus: "series" },
				data: values,
			});
			this.mount(echarts, "trend", {
				tooltip: { ...TOOLTIP, trigger: "axis", valueFormatter: format_currency },
				legend: {
					...LEGEND,
					data: [...(trendExpenseAvailable ? [expenseLabel] : []), __("生产预算"), __("非生产预算"), __("预算合计")],
				},
				grid: { left: 8, right: 24, top: 10, bottom: 40, containLabel: true },
				xAxis: category_axis(
					trend.map((row) =>
						same_year ? `${Number(String(row.month || "").slice(5, 7))}月` : row.month
					),
					{ axisLabel: { fontSize: 12, color: "#6b7280" } }
				),
				yAxis: value_axis(),
				series: [
					line(
						"预算合计",
						trend.map((row) => Number(row.production || 0) + Number(row.nonProduction || 0)),
						CHART_COLORS.total,
						3
					),
					line("生产预算", trend.map((row) => row.production || 0), CHART_COLORS.production, 2),
					line("非生产预算", trend.map((row) => row.nonProduction || 0), CHART_COLORS.nonProduction, 2),
					...(trendExpenseAvailable ? [line(expenseLabel, trend.map((row) => row.actualExpense || 0), CHART_COLORS.expense, 2)] : []),
				],
			});
		} else {
			this.empty_chart("trend");
		}

		// 6) 各部门执行率（Top 10）：横向柱，按阈值配色
		const rate = data.execution || [];
		if (rate.length) {
			const rate_chart = this.mount(echarts, "rate", {
				tooltip: { ...TOOLTIP, trigger: "axis", valueFormatter: (value) => `${value}%` },
				grid: { left: 8, right: 24, top: 10, bottom: 20, containLabel: true },
				xAxis: { ...value_axis((value) => `${value}%`), axisLabel: { ...AXIS_LABEL, formatter: "{value}%" } },
				yAxis: category_axis(
					rate.map((row) => truncate_label(row.shortName || row.deptName, 10)),
					{ axisLabel: { fontSize: 12, color: "#6b7280" }, inverse: true }
				),
				series: [
					{
						name: __("执行率"),
						type: "bar",
						barSize: 16,
						data: rate.map((row) => {
							const value = Number(row.executionRate || 0);
							const color =
								value >= 100
									? CHART_COLORS.rateDanger
									: value >= 80
									? CHART_COLORS.rateWarning
									: CHART_COLORS.rateOk;
							return { value, itemStyle: { color, borderRadius: [0, 4, 4, 0] } };
						}),
					},
				],
			});
			this.bind_department_click(rate_chart, rate);
		} else {
			this.empty_chart("rate");
		}

		// 7) 部门预算 vs 实际支出（Top 10）：纵向双柱，标签竖排
		const compare = data.dept_compare || [];
		if (compare.length) {
			const compare_chart = this.mount(echarts, "compare", {
				tooltip: { ...TOOLTIP, trigger: "axis", axisPointer: { type: "shadow" }, valueFormatter: format_currency },
				legend: { ...LEGEND },
				grid: { left: 8, right: 24, top: 10, bottom: 100, containLabel: true },
				xAxis: category_axis(
					compare.map((row) => truncate_label(row.shortName || row.deptName, 10)),
					{ axisLabel: { rotate: -90, interval: 0, height: 100 } }
				),
				yAxis: value_axis(),
				series: [
					bar_series("预算金额", compare.map((row) => row.budget || 0), CHART_COLORS.production, {
						borderRadius: [4, 4, 0, 0],
					}),
					bar_series(expenseLabel, compare.map((row) => row.approved || 0), CHART_COLORS.total, {
						borderRadius: [4, 4, 0, 0],
					}),
				],
			});
			this.bind_department_click(compare_chart, compare);
		} else {
			this.empty_chart("compare");
		}

		if (!expenseAvailable) {
			["rate", "compare"].forEach(name => this.empty_chart(name, __("Expense data unavailable")));
		}
		if (!expenseAvailable || data.availability?.pending_expenses === false) {
			this.empty_chart("exec-status", __("Expense data unavailable"));
		}
		this.resize_charts();
		// 侧边栏/容器宽度在加载完成后还会变一次，延迟再量一遍避免图表按旧宽度绘制
		setTimeout(() => this.resize_charts(), 300);
	}
}

window.BudgetReport = BudgetDashboard;
})();
