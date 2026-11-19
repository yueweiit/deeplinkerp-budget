(function () {
/* 预算详情弹窗：对齐 dingtalk-budget 的 BudgetList 详情。
 * 基本信息 + 3 类预算卡 + 5 类支出卡 + 预算/支出对比图 + 总预算/总支出/剩余。
 */
/* 行内「钉钉原单」：原单链接由后端按 process_instance_id 拼接，列表接口不返回该字段，
 * 所以点一次取一次详情（详情接口只读且已缓存过一条单据，开销可接受）。
 */
function open_budget_dingtalk(name) {
	frappe.call({
		method: "budget.api.budget_ledger.get_budget_entry_detail",
		args: { form_no: name },
	}).then((response) => {
		const dingtalk = (response.message || {}).dingtalk || {};
		const url = dingtalk.client_url || dingtalk.pc_url;
		if (url) {
			window.open(url, "_blank");
		} else {
			frappe.msgprint(__("这张预算单没有关联的钉钉审批实例"));
		}
	});
}

function show_budget_detail(name) {
	frappe.call({
		method: "budget.api.budget_ledger.get_budget_entry_detail",
		args: { form_no: name },
		freeze: true,
		freeze_message: __("加载预算详情…"),
	}).then((response) => render_budget_detail(response.message || {}));
}

// 「支出明细」分区顺序与标题对齐源项目 expenseDetailSections.js
const EXPENSE_SECTION_DEFS = [
	["management", "运营/采购支出明细"],
	["salary", "工资/社保明细"],
	["bonus", "备用金明细"],
	["officeEquipment", "办公设备明细"],
	["tax", "个税明细"],
	["office", "办公场地明细"],
];

// echarts 只加载一次；用 var 避免与其他页面脚本的同名声明冲突
var bd_echarts_loading = null;

// 金额格式与源项目 fmtWan 一致：≥ 1 万折算成「万」，否则保留两位小数
function format_wan(value) {
	if (value === null) return "—";
	const num = Number(value || 0);
	return num >= 10000 ? `¥${(num / 10000).toFixed(2)}万` : `¥${num.toFixed(2)}`;
}

// 固定分区之外，行政/管理费按类别各自成区（源项目 administrative:<类别>）
function expense_section_defs(details) {
	const defs = EXPENSE_SECTION_DEFS.map(([key, title]) => ({ key, title }));
	const dynamic = new Map();
	(details || []).forEach((row) => {
		const key = String(row.section || "");
		if (!key.startsWith("administrative:") || dynamic.has(key)) return;
		const label = String(row.section_title || "").trim();
		dynamic.set(key, label || `${key.slice("administrative:".length)}明细`);
	});
	const extra = [...dynamic.entries()]
		.map(([key, title]) => ({ key, title }))
		.sort((left, right) => left.title.localeCompare(right.title, "zh-CN"));
	return defs.concat(extra);
}

function render_expense_sections(details, esc) {
	const blocks = expense_section_defs(details).map(({ key, title }) => {
		const rows = (details || []).filter((row) => row.section === key);
		if (!rows.length) return "";
		const total = rows.reduce((sum, row) => sum + Number(row.amount || 0), 0);
		const body = rows
			.map(
				(row) => `<tr>
					<td class="bd-nowrap">${esc(row.date || "-")}</td>
					<td class="bd-nowrap">${esc(row.business_id || "-")}</td>
					<td class="bd-type">${esc(row.expense_type || "-")}</td>
					<td class="bd-text">${esc(row.description || "-")}</td>
					<td class="bd-amount">${esc(format_wan(row.amount))}</td>
					<td class="bd-nowrap">${esc(row.payment_event || "-")}</td>
					<td class="bd-text bd-remark">${esc(row.remark || "-")}</td>
				</tr>`
			)
			.join("");
		return `<section class="bd-expense-section">
			<div class="bd-expense-head">
				<h4>${esc(__(title))}</h4>
				<span>${__("{0} 条", [rows.length])} / ${esc(format_wan(total))}</span>
			</div>
			<div class="bd-expense-table-wrap">
				<table class="bd-expense-table">
					<thead><tr>
						<th>${__("日期")}</th>
						<th>${__("业务编号")}</th>
						<th>${__("支出类型")}</th>
						<th>${__("事项/说明")}</th>
						<th class="bd-num">${__("金额")}</th>
						<th>${__("付款事件")}</th>
						<th>${__("备注")}</th>
					</tr></thead>
					<tbody>${body}</tbody>
				</table>
			</div>
		</section>`;
	}).join("");
	return blocks
		? `<div class="bd-expense-details"><h3 class="bd-expense-title">${__("支出明细")}</h3>${blocks}</div>`
		: "";
}

function render_budget_detail(data) {
	const entry = data.entry || {};
	const budget = data.budget || {};
	const expense = data.expense || {};
	const dingtalk = data.dingtalk || {};
	const money = (value) =>
		Number(value || 0).toLocaleString("zh-CN", {
			minimumFractionDigits: 2,
			maximumFractionDigits: 2,
		});
	const esc = (value) => frappe.utils.escape_html(String(value ?? ""));
	const row = (label, value) =>
		`<div class="bd-item"><div class="bd-item-label">${__(label)}</div><div class="bd-item-value">${esc(
			value || "-"
		)}</div></div>`;

	const dialog = new frappe.ui.Dialog({
		title: __("预算详情") + " · " + esc(entry.form_no || ""),
		size: "large",
	});

	dialog.$body.html(`
		<div class="bd-detail">
			${data.warning ? `<div class="bd-warning">${esc(data.warning)}</div>` : ""}
			<div class="bd-head">
				<span class="indicator-pill no-indicator-dot gray">${esc(entry.status || "-")}</span>
				<button type="button" class="btn btn-default btn-xs bd-dingtalk">${__("钉钉原单")}</button>
			</div>
			<div class="bd-info">
				${row("表单编号", entry.form_no)}
				${row("子公司", entry.company)}
				${row("部门", entry.department)}
				${row("部门 ID", entry.department_id)}
				${row("预算类型", entry.budget_kind === "production" ? __("Production Budget") : __("Non Production Budget"))}
				${row("状态", entry.status)}
				${row("申请日期", entry.application_date)}
				${row("预算月份", String(entry.budget_month || "").slice(0, 7))}
				${row("执行地区", entry.execution_region)}
				${row("预算金额", money(entry.amount))}
				${row("同步时间", entry.synced_at)}
				${row("备注", entry.remark)}
			</div>

			<div class="bd-section-title">${__("预算与支出明细")}</div>
			<div class="bd-cards bd-cards-3">
				<div class="bd-card bd-budget"><div class="bd-card-label">${__("管理预算明细")}</div><div class="bd-card-value">${esc(format_wan(budget.operation))}</div></div>
				<div class="bd-card bd-budget"><div class="bd-card-label">${__("人资预算")}</div><div class="bd-card-value">${esc(format_wan(budget.hr))}</div></div>
				<div class="bd-card bd-budget"><div class="bd-card-label">${__("办公场地预算")}</div><div class="bd-card-value">${esc(format_wan(budget.office))}</div></div>
			</div>
			<div class="bd-cards bd-cards-5">
				<div class="bd-card bd-expense"><div class="bd-card-label">${__("管理支出")}</div><div class="bd-card-value">${esc(format_wan(expense.management))}</div></div>
				<div class="bd-card bd-expense"><div class="bd-card-label">${__("工资/公积金支出")}</div><div class="bd-card-value">${esc(format_wan(expense.salary))}</div></div>
				<div class="bd-card bd-expense"><div class="bd-card-label">${__("备用金支出")}</div><div class="bd-card-value">${esc(format_wan(expense.bonus))}</div></div>
				<div class="bd-card bd-expense"><div class="bd-card-label">${__("办公场地支出")}</div><div class="bd-card-value">${esc(format_wan(expense.office))}</div></div>
				<div class="bd-card bd-expense"><div class="bd-card-label">${__("个税支出")}</div><div class="bd-card-value">${esc(format_wan(expense.tax))}</div></div>
			</div>

			<div class="bd-chart" data-detail-chart></div>

			<div class="bd-cards bd-cards-3 bd-totals">
				<div class="bd-card bd-total-budget"><div class="bd-card-label">${__("总预算")}</div><div class="bd-card-value">${esc(format_wan(budget.total))}</div></div>
				<div class="bd-card bd-total-expense"><div class="bd-card-label">${__("总支出")}</div><div class="bd-card-value">${esc(format_wan(expense.total))}</div></div>
				<div class="bd-card bd-total-remaining ${Number(data.remaining || 0) < 0 ? "bd-negative" : ""}"><div class="bd-card-label">${__("剩余")}</div><div class="bd-card-value">${esc(format_wan(data.remaining))}</div></div>
			</div>

			${render_expense_sections(data.expense_details || [], esc)}
		</div>
	`);

	dialog.$body.find(".bd-dingtalk").on("click", () => {
		const url = dingtalk.client_url || dingtalk.pc_url;
		if (url) window.open(url, "_blank");
		else frappe.msgprint(__("这张预算单没有关联的钉钉审批实例"));
	});

	dialog.show();
	dialog.$body.tooltip({ selector: "[title]", trigger: "hover" });

	// 每次打开都重建样式，避免已加载页面沿用过期的 CSS
	$("#budget-detail-style").remove();
	$(`<style id="budget-detail-style">
			.bd-detail { font-size: 13px; }
			.bd-warning { margin-bottom: 10px; padding: 8px 10px; border-radius: 6px; background: #fffbeb; color: #92400e; border: 1px solid #fde68a; }
			.bd-head { display: flex; align-items: center; gap: 10px; margin-bottom: 12px; }
			.bd-info { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 10px 16px; margin-bottom: 18px; }
			.bd-item-label { font-size: 12px; color: #8d99a6; margin-bottom: 2px; }
			.bd-item-value { font-size: 13px; color: #1f272e; word-break: break-all; }
			.bd-section-title { font-size: 16px; font-weight: 600; margin: 20px 0 10px; }
			.bd-cards { display: grid; gap: 10px; margin-bottom: 10px; }
			.bd-cards-3 { grid-template-columns: repeat(3, minmax(0, 1fr)); }
			.bd-cards-5 { grid-template-columns: repeat(5, minmax(0, 1fr)); margin-bottom: 16px; }
			.bd-card { padding: 12px; border-radius: 6px; text-align: center; }
			.bd-budget { background: #e6f0ff; }
			.bd-budget .bd-card-label { color: #2f54eb; }
			.bd-budget .bd-card-value { color: #2f54eb; }
			.bd-expense { background: #f0fff0; }
			.bd-expense .bd-card-label { color: #52c41a; }
			.bd-expense .bd-card-value { color: #52c41a; }
			.bd-card-label { font-size: 12px; margin-bottom: 4px; }
			.bd-card-value { font-size: 18px; font-weight: 700; font-variant-numeric: tabular-nums; }
			.bd-totals { margin-top: 16px; }
			.bd-totals .bd-card { padding: 14px; }
			.bd-totals .bd-card-value { font-size: 24px; }
			.bd-total-budget { background: #e6f0ff; }
			.bd-total-budget .bd-card-label, .bd-total-budget .bd-card-value { color: #2f54eb; }
			.bd-total-expense { background: #f0fff0; }
			.bd-total-expense .bd-card-label, .bd-total-expense .bd-card-value { color: #52c41a; }
			.bd-total-remaining { background: #fff7e6; }
			.bd-total-remaining .bd-card-label, .bd-total-remaining .bd-card-value { color: #d46b08; }
			.bd-total-remaining.bd-negative { background: #fff1f0; }
			.bd-total-remaining.bd-negative .bd-card-label, .bd-total-remaining.bd-negative .bd-card-value { color: #cf1322; }
			.bd-chart { height: 320px; margin: 8px 0 16px; }
			.bd-empty { display: flex; align-items: center; justify-content: center; height: 100%; color: #6b7280; font-size: 13px; }
			/* 支出明细：与源项目 expenseDetailSections 的表格样式逐项对齐 */
			.bd-expense-details { margin-top: 20px; display: grid; gap: 16px; }
			.bd-expense-title { margin: 0; font-size: 16px; font-weight: 600; color: #1f272e; }
			.bd-expense-section { border: 1px solid #e5e7eb; border-radius: 8px; overflow: hidden; background: #fff; }
			.bd-expense-head { display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: 8px; padding: 10px 12px; background: #f9fafb; border-bottom: 1px solid #e5e7eb; }
			.bd-expense-head h4 { margin: 0; font-size: 14px; font-weight: 700; color: #374151; }
			.bd-expense-head span { font-size: 13px; font-weight: 700; color: #16a34a; }
			.bd-expense-table-wrap { overflow-x: auto; }
			.bd-expense-table { width: 100%; min-width: 940px; margin: 0; border-collapse: collapse; font-size: 12px; }
			.bd-expense-table th { padding: 9px 10px; background: #fff; color: #6b7280; font-size: 12px; font-weight: 600; text-align: left; white-space: nowrap; border-bottom: 1px solid #f3f4f6; }
			.bd-expense-table td { padding: 10px; color: #374151; font-size: 12px; vertical-align: top; border-bottom: 1px solid #f3f4f6; }
			.bd-expense-table .bd-num, .bd-expense-table .bd-amount { text-align: right; white-space: nowrap; }
			.bd-expense-table .bd-amount { color: #16a34a; font-weight: 700; }
			.bd-expense-table .bd-type { min-width: 130px; }
			.bd-expense-table .bd-text { min-width: 280px; white-space: normal; word-break: break-word; line-height: 1.5; }
			.bd-expense-table .bd-remark { min-width: 150px; }
			.bd-nowrap { white-space: nowrap; }
			@media (max-width: 900px) { .bd-cards-3, .bd-cards-5 { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
			@media (max-width: 800px) { .bd-info { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
		</style>`).appendTo(document.head);

	if (data.expense_available === false) {
		dialog.$body.find("[data-detail-chart]").text(__("Expense data unavailable"));
	} else {
		mount_detail_chart(dialog, budget, expense);
	}
}

// 预算 vs 支出：与源项目 detail 图表的 ECharts 配置逐项对齐（柱顶金额标签、万单位、图例置顶）
function mount_detail_chart(dialog, budget, expense) {
	const option = {
		tooltip: { trigger: "axis", axisPointer: { type: "shadow" } },
		toolbox: { feature: { saveAsImage: { title: __("保存图片") } }, right: 10 },
		grid: { top: 60, bottom: 40, left: 60, right: 20 },
		legend: { data: [__("预算"), __("支出")], top: 10 },
		xAxis: {
			type: "category",
			data: [
				__("管理预算明细"),
				__("人资"),
				__("备用金"),
				__("办公设备"),
				__("办公场地"),
				__("个税"),
			],
		},
		yAxis: {
			type: "value",
			axisLabel: {
				formatter: (value) => (Math.abs(value) >= 10000 ? `${value / 10000}万` : value),
			},
		},
		series: [
			{
				name: __("预算"),
				type: "bar",
				color: "#2f54eb",
				barMaxWidth: 40,
				data: [budget.operation || 0, budget.hr || 0, 0, 0, budget.office || 0, 0],
				label: { show: true, position: "top", formatter: (params) => format_wan(params.value) },
			},
			{
				name: __("支出"),
				type: "bar",
				color: "#52c41a",
				barMaxWidth: 40,
				data: [
					expense.management || 0,
					expense.salary || 0,
					expense.bonus || 0,
					expense.officeEquipment || 0,
					expense.office || 0,
					expense.tax || 0,
				],
				label: { show: true, position: "top", formatter: (params) => format_wan(params.value) },
			},
		],
	};

	let chart = null;

	// dialog 里的容器要等弹窗排版完成后才有宽高，尺寸为 0 时重试，避免画出空图
	const draw = () => {
		const element = dialog.$body.find("[data-detail-chart]").get(0);
		if (!element || !window.echarts || !element.clientWidth || !element.clientHeight) {
			return false;
		}
		chart = window.echarts.getInstanceByDom(element) || window.echarts.init(element);
		chart.setOption(option, true);
		chart.resize();
		return true;
	};

	const ensure = (attempt = 0) => {
		if (draw()) return;
		if (attempt < 30) {
			setTimeout(() => ensure(attempt + 1), 100);
			return;
		}
		dialog.$body
			.find("[data-detail-chart]")
			.html(`<div class="bd-empty">${__("图表加载失败，请刷新页面重试")}</div>`);
	};

	const load = () => {
		if (window.echarts) {
			ensure();
			return;
		}
		// 与 dashboard 共用同一份 echarts；用 script 标签直接注入，加载失败也能兜底提示
		if (!bd_echarts_loading) {
			bd_echarts_loading = new Promise((resolve) => {
				const script = document.createElement("script");
				script.src = "/assets/budget/js/echarts.min.js";
				script.async = true;
				script.onload = resolve;
				script.onerror = resolve;
				document.head.appendChild(script);
			});
		}
		bd_echarts_loading.then(() => ensure());
	};

	load();
	$(window).on("resize.budget-detail-chart", () => chart && chart.resize());
	dialog.$wrapper.on("hidden.bs.modal", () => {
		$(window).off("resize.budget-detail-chart");
		if (chart) {
			chart.dispose();
			chart = null;
		}
	});
}

window.BudgetLedgerDetail = {open: show_budget_detail, openDingtalk: open_budget_dingtalk, formatAmount: format_wan};
})();
