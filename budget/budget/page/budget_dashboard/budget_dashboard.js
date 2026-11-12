/* 预算数据看板直接使用 Deeplinkerp / Frappe Desk 的页面和报表样式。 */
frappe.pages["budget-dashboard"] = frappe.pages["budget-dashboard"] || {};

frappe.pages["budget-dashboard"].on_page_load = function (wrapper) {
	wrapper.budget_dashboard = new BudgetDashboard(wrapper);
};

frappe.pages["budget-dashboard"].on_page_show = function () {
	frappe.utils.set_title(__("预算数据看板"));
};

class BudgetDashboard {
	constructor(wrapper) {
		this.page = frappe.ui.make_app_page({
			parent: wrapper,
		title: __("预算数据看板"),
		single_column: true,
		});
		frappe.breadcrumbs.add("Budget");
		this.page_size = 10;
		this.page_number = 1;
		this.total = 0;
		this.list_request = 0;
		this.stats_request = 0;
		this.setup_filters();
		this.setup_content();
		this.page.set_primary_action(__("查询"), () => this.refresh(true), "search");
		this.page.set_secondary_action(__("重置"), () => this.reset());
		this.refresh(true);
	}

	current_month_start() {
		const today = frappe.datetime.get_today();
		return frappe.datetime.month_start(today);
	}

	setup_filters() {
		this.budget_date = this.page.add_field({
			fieldname: "budget_date", label: __("预算月份"), fieldtype: "Date",
			default: this.current_month_start(),
		});
		this.remove_filter_tooltip(this.budget_date);
		this.budget_date.$input.attr("aria-label", __("预算月份"));
		this.kind = this.page.add_field({
			fieldname: "budget_type", label: __("预算类型"), fieldtype: "Select",
			options: [__("全部"), __("生产预算"), __("非生产预算")].join("\n"),
			default: __("全部"),
		});
		this.remove_filter_tooltip(this.kind);
		this.kind.$input.attr("aria-label", __("预算类型"));
	}

	remove_filter_tooltip(field) {
		field.$wrapper.tooltip("dispose").removeAttr("title data-original-title");
	}

	setup_content() {
		this.$content = $(`
			<div class="budget-dashboard-page">
				<div class="budget-dashboard-message" role="alert" hidden></div>
				<div class="report-summary budget-dashboard-summary" aria-label="${__("预算统计")}">
					<div class="summary-item"><div class="summary-label">${__("今日生产预算")}</div><div class="summary-value" data-stat="production_today">—</div></div>
					<div class="summary-item"><div class="summary-label">${__("今日非生产预算")}</div><div class="summary-value" data-stat="non_production_today">—</div></div>
					<div class="summary-item"><div class="summary-label">${__("生产预算总数")}</div><div class="summary-value" data-stat="production_total">—</div></div>
					<div class="summary-item"><div class="summary-label">${__("非生产预算总数")}</div><div class="summary-value" data-stat="non_production_total">—</div></div>
				</div>
				<div class="report-wrapper budget-dashboard-report">
					<div class="budget-dashboard-heading">
						<strong>${__("预算明细")}</strong>
						<span class="text-muted budget-dashboard-count" aria-live="polite">${__("正在加载…")}</span>
					</div>
					<div class="budget-dashboard-table-scroll">
						<table class="table budget-dashboard-table">
							<thead><tr>
								<th scope="col">${__("表单编号")}</th>
								<th scope="col">${__("子公司")}</th>
								<th scope="col">${__("部门")}</th>
								<th scope="col">${__("预算类型")}</th>
								<th scope="col">${__("申请日期")}</th>
								<th scope="col">${__("预算月份")}</th>
								<th scope="col">${__("执行地区")}</th>
								<th scope="col">${__("状态")}</th>
								<th scope="col" class="text-right">${__("预算金额（元）")}</th>
							</tr></thead>
							<tbody><tr><td colspan="9" class="budget-dashboard-empty">${__("正在加载…")}</td></tr></tbody>
						</table>
					</div>
					<div class="budget-dashboard-footer">
						<span class="text-muted">${__("数据源：budget_system（只读）")}</span>
						<div class="budget-dashboard-pagination">
							<span class="text-muted budget-dashboard-page-info" aria-live="polite">—</span>
							<button type="button" class="btn btn-default btn-sm budget-dashboard-prev">${__("上一页")}</button>
							<button type="button" class="btn btn-default btn-sm budget-dashboard-next">${__("下一页")}</button>
						</div>
					</div>
				</div>
			</div>
		`).appendTo(this.page.main);
		this.$message = this.$content.find(".budget-dashboard-message");
		this.$body = this.$content.find("tbody");
		this.$count = this.$content.find(".budget-dashboard-count");
		this.$page_info = this.$content.find(".budget-dashboard-page-info");
		this.$prev = this.$content.find(".budget-dashboard-prev");
		this.$next = this.$content.find(".budget-dashboard-next");
		this.$prev.on("click", () => {
			if (this.page_number > 1) { this.page_number -= 1; this.load_list(); }
		});
		this.$next.on("click", () => {
			if (this.page_number * this.page_size < this.total) {
				this.page_number += 1;
				this.load_list();
			}
		});
		this.install_styles();
	}

	install_styles() {
		if (document.getElementById("budget-dashboard-desk-style")) return;
		$(`<style id="budget-dashboard-desk-style">
			.budget-dashboard-page { color: var(--text-color); padding-bottom: 24px; }
			.budget-dashboard-page .report-summary { margin: 12px 15px 18px; padding: 12px 8px; justify-content: flex-start; gap: 0; border: 1px solid var(--border-color); background: var(--fg-color); }
			.budget-dashboard-page .summary-item { flex: 1 1 160px; min-width: 140px; max-width: none; height: 54px; margin: 0; padding: 0 18px; align-items: flex-start; }
			.budget-dashboard-page .summary-item + .summary-item { border-left: 1px solid var(--border-color); }
			.budget-dashboard-page .summary-label { color: var(--text-muted); text-align: left; font-size: var(--text-sm); }
			.budget-dashboard-page .summary-value { margin-top: 5px; font-size: var(--text-xl); line-height: 1.2; }
			.budget-dashboard-message { margin: 12px 15px 0; padding: 8px 12px; border: 1px solid var(--red-200); border-radius: var(--border-radius); color: var(--red-600); background: var(--red-50); }
			.budget-dashboard-message[hidden] { display: none; }
			.budget-dashboard-report { padding-top: 0; }
			.budget-dashboard-heading { display: flex; align-items: baseline; gap: 12px; padding-bottom: 10px; }
			.budget-dashboard-count, .budget-dashboard-footer { font-size: var(--text-sm); }
			.budget-dashboard-table-scroll { overflow-x: auto; border: 1px solid var(--table-border-color); border-radius: var(--border-radius); }
			.budget-dashboard-table { width: 100%; min-width: 1080px; margin-bottom: 0; font-size: var(--text-sm); }
			.budget-dashboard-table th { background: var(--subtle-fg); color: var(--text-muted); font-weight: 500; white-space: nowrap; border-bottom: 1px solid var(--table-border-color); }
			.budget-dashboard-table th, .budget-dashboard-table td { padding: 8px 10px; vertical-align: middle; border-right: 1px solid var(--table-border-color); }
			.budget-dashboard-table th:last-child, .budget-dashboard-table td:last-child { border-right: 0; }
			.budget-dashboard-table tbody tr:hover { background: var(--highlight-color); }
			.budget-dashboard-table td { white-space: nowrap; border-top: 1px solid var(--table-border-color); }
			.budget-dashboard-table .budget-dashboard-ellipsis { display: block; max-width: 200px; overflow: hidden; text-overflow: ellipsis; }
			.budget-dashboard-table .budget-dashboard-amount { text-align: right; font-variant-numeric: tabular-nums; }
			.budget-dashboard-table .budget-dashboard-empty { padding: 36px 12px; text-align: center; color: var(--text-muted); }
			.budget-dashboard-footer { display: flex; align-items: center; justify-content: space-between; gap: 12px; flex-wrap: wrap; padding-top: 12px; }
			.budget-dashboard-pagination { display: flex; align-items: center; gap: 8px; }
			.budget-dashboard-page-info { margin-right: 4px; white-space: nowrap; }
			@media (max-width: 740px) {
				.budget-dashboard-page .report-summary { gap: 10px 0; }
				.budget-dashboard-page .summary-item { flex-basis: 50%; }
				.budget-dashboard-page .summary-item:nth-child(3) { border-left: 0; }
			}
		</style>`).appendTo(document.head);
	}

	filters() {
		const selected_date = String(this.budget_date.get_value() || "").trim();
		const selected = this.kind.get_value();
		const budget_type = selected === __("生产预算") ? "production" :
			selected === __("非生产预算") ? "non_production" : "";
		return { month: selected_date ? selected_date.slice(0, 7) : "", budget_type };
	}

	show_error(message) {
		this.$message.text(message || "").prop("hidden", !message);
	}

	refresh(reset_page) {
		if (reset_page) this.page_number = 1;
		const filters = this.filters();
		if (!filters) return;
		this.active_filters = filters;
		this.show_error("");
		return Promise.allSettled([this.load_stats(), this.load_list(filters)]);
	}

	async reset() {
		await Promise.all([
			this.budget_date.set_value(this.current_month_start()),
			this.kind.set_value(__("全部")),
		]);
		return this.refresh(true);
	}

	async load_stats() {
		const request = ++this.stats_request;
		try {
			const response = await frappe.call({ method: "budget.api.budget_ledger.get_budget_stats" });
			if (request !== this.stats_request) return;
			if (!response.message) throw new Error("Missing budget statistics");
			for (const key of ["production_today", "non_production_today", "production_total", "non_production_total"]) {
				const value = Number(response.message[key]);
				if (!Number.isFinite(value)) throw new Error(`Invalid budget statistic: ${key}`);
				this.$content.find(`[data-stat="${key}"]`).text(value.toLocaleString("zh-CN"));
			}
		} catch (error) {
			if (request === this.stats_request) this.show_error(__("预算统计加载失败，请刷新后重试"));
		}
	}

	async load_list(filters = this.active_filters) {
		if (!filters) return;
		const request = ++this.list_request;
		this.$body.html(`<tr><td colspan="9" class="budget-dashboard-empty">${__("正在加载…")}</td></tr>`);
		this.$prev.prop("disabled", true);
		this.$next.prop("disabled", true);
		const args = { page: this.page_number, page_size: this.page_size };
		if (filters.month) {
			args.start_month = filters.month;
			args.end_month = filters.month;
		}
		if (filters.budget_type) args.budget_type = filters.budget_type;
		try {
			const response = await frappe.call({ method: "budget.api.budget_ledger.list_budget_entries", args });
			if (request !== this.list_request) return;
			const data = response.message || {};
			this.total = Number(data.total || 0);
			this.render_rows(data.rows || []);
			const pages = Math.max(Math.ceil(this.total / this.page_size), 1);
			this.$count.text(__("共 {0} 条", [this.total]));
			this.$page_info.text(__("第 {0} / {1} 页", [this.page_number, pages]));
			this.$prev.prop("disabled", this.page_number <= 1);
			this.$next.prop("disabled", this.page_number >= pages);
		} catch (error) {
			if (request !== this.list_request) return;
			this.show_error(__("预算明细查询失败，请检查权限或稍后重试"));
			this.$body.html(`<tr><td colspan="9" class="budget-dashboard-empty">${__("加载失败")}</td></tr>`);
			this.$count.text(__("加载失败"));
			this.$page_info.text("—");
		}
	}

	render_rows(rows) {
		if (!rows.length) {
			this.$body.html(`<tr><td colspan="9" class="budget-dashboard-empty">${__("当前条件下没有预算单")}</td></tr>`);
			return;
		}
		const escape = (value) => frappe.utils.escape_html(String(value ?? ""));
		const date = (value) => escape(String(value || "").slice(0, 10) || "-");
		const money = (value) => {
			const amount = Number(value ?? 0);
			return Number.isFinite(amount) ? amount.toLocaleString("zh-CN", { minimumFractionDigits: 2, maximumFractionDigits: 2 }) : "-";
		};
		this.$body.html(rows.map((row) => {
			const status = String(row.status || "-");
			const color = status.includes("通过") ? "green" : /待|审批|进行/.test(status) ? "orange" : "gray";
			const kind = row.budget_kind_label || (row.budget_kind === "production" ? __("生产预算") : __("非生产预算"));
			const amount = row.amount ?? row.total_amount ?? row.budget_amount;
			return `<tr>
				<td>${escape(row.form_no || "-")}</td>
				<td><span class="budget-dashboard-ellipsis" title="${escape(row.company_display || "-")}">${escape(row.company_display || "-")}</span></td>
				<td><span class="budget-dashboard-ellipsis" title="${escape(row.dept_name || row.department_display || "-")}">${escape(row.department_display || "-")}</span></td>
				<td>${escape(kind)}</td>
				<td>${date(row.application_date)}</td>
				<td>${escape(row.budget_month || row.declaration_month || "-")}</td>
				<td>${escape(row.execution_region || "-")}</td>
				<td><span class="indicator-pill no-indicator-dot ${color}">${escape(status)}</span></td>
				<td class="budget-dashboard-amount">${money(amount)}</td>
			</tr>`;
		}).join(""));
	}
}
