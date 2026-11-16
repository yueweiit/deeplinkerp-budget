/* 预算配置：4 张统计卡 + 7 项筛选 +「组织额度汇总」四页签 +「预算额度明细」分页表。
 *
 * 视觉完全沿用 Desk 的样式变量与原生控件（form-control / btn / btn-group），
 * 不再自带页头与主题色，避免和系统其它页面脱节。
 * 口径见 budget/integrations/quota_source.py：只统计 status = 'effective'。
 */
frappe.pages["budget-config"] = frappe.pages["budget-config"] || {};

frappe.pages["budget-config"].on_page_load = function (wrapper) {
	wrapper.budget_config = new BudgetConfig(wrapper);
};

frappe.pages["budget-config"].on_page_show = function () {
	frappe.utils.set_title(__("预算配置"));
};

// key 与后端 rollups 的键名保持一致（group / company / department / subject）
const BC_SUMMARY_TABS = [
	{ key: "group", label: __("集团统计"), name: __("集团") },
	{ key: "company", label: __("子公司统计"), name: __("子公司") },
	{ key: "department", label: __("部门统计"), name: __("部门") },
	{ key: "subject", label: __("科目统计"), name: __("科目") },
];

function bc_money(value) {
	return Number(value || 0).toLocaleString("zh-CN", {
		minimumFractionDigits: 2,
		maximumFractionDigits: 2,
	});
}

function bc_budget_type_label(value) {
	if (value === "production") return __("生产预算");
	if (value === "non_production") return __("非生产预算");
	return __("待确认");
}

class BudgetConfig {
	constructor(wrapper) {
		this.page = frappe.ui.make_app_page({
			parent: wrapper,
			title: __("预算配置"),
			single_column: true,
		});
		this.page_size = 20;
		this.page_number = 1;
		this.active_tab = "group";
		this.total = 0;
		this.rollups = {};
		this.request_id = 0;
		this.setup_content();
		this.page.set_primary_action(__("查询"), () => this.refresh(true), "search");
		this.page.set_secondary_action(__("重置"), () => this.reset());
		// 筛选项与数据互不依赖，并行请求，少等一个来回
		this.load_options();
		this.refresh(true);
	}

	/* ---------------------------------------------------------- 骨架 */
	setup_content() {
		this.$content = $(`
			<div class="bc-page">
				<div class="bc-message" role="alert" hidden></div>

				<div class="bc-stats">
					${[
						["total_amount", "生效预算额度"],
						["quota_count", "生效预算单"],
						["department_count", "当前可见部门"],
						["detail_amount_mismatch_count", "明细金额待核对"],
					]
						.map(
							([key, label]) => `<div class="bc-card">
								<div class="bc-card-label">${__(label)}</div>
								<div class="bc-card-value" data-stat="${key}">—</div>
							</div>`
						)
						.join("")}
				</div>

				<div class="bc-panel">
					<div class="bc-filter-grid"></div>
				</div>

				<div class="bc-panel">
					<div class="bc-panel-head">
						<h3 class="bc-panel-title">${__("组织额度汇总")}</h3>
						<div class="btn-group btn-group-sm bc-tabs" role="group">
							${BC_SUMMARY_TABS.map(
								(tab, index) =>
									`<button type="button" class="btn btn-default${
										index === 0 ? " active" : ""
									}" data-tab="${tab.key}">${tab.label}</button>`
							).join("")}
						</div>
					</div>
					<div class="bc-table-wrap">
						<table class="bc-table bc-table-rollup">
							<thead><tr>
								<th>${__("集团")}</th>
								<th class="text-right">${__("生效预算单")}</th>
								<th class="text-right">${__("预算额度（元）")}</th>
							</tr></thead>
							<tbody><tr><td colspan="3" class="bc-empty">${__("正在加载…")}</td></tr></tbody>
						</table>
					</div>
				</div>

				<div class="bc-panel">
					<div class="bc-panel-head">
						<h3 class="bc-panel-title">${__("预算额度明细")}</h3>
						<span class="text-muted small bc-count"></span>
					</div>
					<div class="bc-table-wrap">
						<table class="bc-table bc-table-detail">
							<thead><tr>
								<th>${__("年度")}</th>
								<th>${__("月份")}</th>
								<th>${__("集团")}</th>
								<th>${__("子公司")}</th>
								<th>${__("部门")}</th>
								<th>${__("预算科目")}</th>
								<th class="text-right">${__("预算金额（元）")}</th>
								<th class="text-right">${__("科目金额（元）")}</th>
								<th>${__("负责人")}</th>
								<th>${__("预算类型")}</th>
								<th>${__("表单编号")}</th>
							</tr></thead>
							<tbody><tr><td colspan="11" class="bc-empty">${__("额度配置加载中…")}</td></tr></tbody>
						</table>
					</div>
					<div class="bc-pagination">
						<button type="button" class="btn btn-default btn-xs bc-prev">${__("上一页")}</button>
						<span class="text-muted bc-page-info"></span>
						<button type="button" class="btn btn-default btn-xs bc-next">${__("下一页")}</button>
					</div>
				</div>
			</div>
		`).appendTo(this.page.main);

		this.$message = this.$content.find(".bc-message");
		this.$filters = this.$content.find(".bc-filter-grid");
		this.$rollup = this.$content.find(".bc-table-rollup tbody");
		this.$rollup_head = this.$content.find(".bc-table-rollup thead th").first();
		this.$detail = this.$content.find(".bc-table-detail tbody");
		this.$count = this.$content.find(".bc-count");
		this.$page_info = this.$content.find(".bc-page-info");
		this.$prev = this.$content.find(".bc-prev");
		this.$next = this.$content.find(".bc-next");

		this.$prev.on("click", () => {
			if (this.page_number <= 1) return;
			this.page_number -= 1;
			this.load_list();
		});
		this.$next.on("click", () => {
			if (this.page_number * this.page_size >= this.total) return;
			this.page_number += 1;
			this.load_list();
		});
		this.$content.find(".bc-tabs .btn").on("click", (event) => {
			event.preventDefault();
			this.active_tab = event.currentTarget.dataset.tab;
			this.$content.find(".bc-tabs .btn").removeClass("active");
			$(event.currentTarget).addClass("active");
			const tab = BC_SUMMARY_TABS.find((item) => item.key === this.active_tab);
			this.$rollup_head.text(tab ? tab.name : __("名称"));
			this.render_rollup();
		});

		this.build_filters();
		this.install_styles();
	}

	build_filters() {
		const configs = [
			{ fieldname: "budget_year", label: __("年度"), formatter: (value) => `${value}年` },
			{ fieldname: "budget_month", label: __("月份") },
			{ fieldname: "group_dept_id", label: __("集团") },
			{ fieldname: "company_dept_id", label: __("子公司") },
			{ fieldname: "department_id", label: __("部门") },
			{ fieldname: "subject_name", label: __("预算科目") },
			{
				fieldname: "budget_type",
				label: __("预算类型"),
				formatter: (value) => bc_budget_type_label(value),
			},
		];
		this.filters_config = configs;
		this.selects = {};
		this.$filters.empty();
		configs.forEach((config) => {
			const $filter = $(`<label class="bc-filter">
				<span class="bc-filter-label">${config.label}</span>
				<select class="form-control input-sm"></select>
			</label>`);
			const $select = $filter.find("select");
			$select.on("change", () => {
				if (config.fieldname === "group_dept_id") this.narrow_companies();
				if (config.fieldname === "company_dept_id") this.narrow_departments();
			});
			$filter.appendTo(this.$filters);
			this.selects[config.fieldname] = $select;
		});
	}

	install_styles() {
		// 每次进入都重建，避免旧的样式残留
		$("#budget-config-style").remove();
		$(`<style id="budget-config-style">
			.bc-page { padding: 12px 15px 30px; }
			.bc-message { margin-bottom: 12px; padding: 8px 12px; border: 1px solid var(--red-200); border-radius: var(--border-radius); color: var(--red-600); background: var(--red-50); }
			.bc-stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 12px; margin-bottom: 16px; }
			.bc-card { padding: 14px 16px; border: 1px solid var(--border-color); border-radius: var(--border-radius-md, 8px); background: var(--card-bg, var(--fg-color)); }
			.bc-card-label { font-size: 12px; color: var(--text-muted); }
			.bc-card-value { margin-top: 6px; font-size: 20px; font-weight: 600; line-height: 1.2; font-variant-numeric: tabular-nums; }
			.bc-panel { margin-bottom: 16px; border: 1px solid var(--border-color); border-radius: var(--border-radius-md, 8px); background: var(--card-bg, var(--fg-color)); }
			.bc-panel-head { display: flex; align-items: center; justify-content: space-between; gap: 12px; flex-wrap: wrap; padding: 10px 15px; border-bottom: 1px solid var(--border-color); }
			.bc-panel-title { margin: 0; font-size: 14px; font-weight: 600; color: var(--text-color); }
			.bc-filter-grid { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 10px 16px; padding: 15px; }
			.bc-filter { display: grid; grid-template-columns: 76px minmax(0, 1fr); align-items: center; gap: 8px; margin: 0; font-weight: 400; }
			.bc-filter-label { font-size: 12px; color: var(--text-muted); text-align: right; }
			.bc-table-wrap { overflow-x: auto; }
			.bc-table { width: 100%; margin: 0; font-size: 12px; border-collapse: collapse; }
			.bc-table thead th { padding: 8px 12px; background: var(--subtle-fg); color: var(--text-muted); font-size: 12px; font-weight: 500; text-align: left; white-space: nowrap; border-top: 0; border-bottom: 1px solid var(--border-color); }
			.bc-table tbody td { padding: 8px 12px; color: var(--text-color); vertical-align: top; border-top: 0; border-bottom: 1px solid var(--border-color); }
			.bc-table tbody tr:last-child td { border-bottom: 0; }
			.bc-table .text-right { white-space: nowrap; font-variant-numeric: tabular-nums; }
			.bc-table-detail { min-width: 1080px; }
			.bc-table .bc-empty { padding: 28px 12px; text-align: center; color: var(--text-muted); font-size: 13px; }
			.bc-subject-list { display: flex; flex-direction: column; gap: 2px; min-width: 160px; }
			.bc-subject { display: flex; align-items: center; justify-content: space-between; gap: 12px; white-space: nowrap; }
			.bc-subject .text-muted { font-variant-numeric: tabular-nums; }
			.bc-badge { display: inline-block; padding: 1px 6px; border-radius: var(--border-radius-sm, 4px); background: var(--bg-blue-100, #e8f1fd); color: var(--text-on-light-blue, #2b6cb0); font-size: 11px; white-space: nowrap; }
			.bc-badge-warn { display: inline-block; margin-top: 4px; padding: 1px 6px; border-radius: var(--border-radius-sm, 4px); background: var(--bg-orange-100, #fdf1e0); color: var(--text-on-light-orange, #a45a09); font-size: 11px; white-space: nowrap; }
			.bc-pagination { display: flex; align-items: center; justify-content: flex-end; gap: 8px; padding: 10px 15px; border-top: 1px solid var(--border-color); }
			@media (max-width: 1200px) {
				.bc-filter-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
			}
		</style>`).appendTo(document.head);
	}

	/* ---------------------------------------------------------- 数据 */
	load_options() {
		return frappe
			.call({ method: "budget.api.quota.get_quota_options" })
			.then((response) => {
				const options = response.message || {};
				const fill = (fieldname, values) => {
					const config = this.filters_config.find((item) => item.fieldname === fieldname) || {};
					const $select = this.selects[fieldname];
					if (!$select) return;
					const current = $select.val() || "";
					const html = (values || []).map((value) => {
						const item = value && typeof value === "object" ? value : { id: value, name: value };
						const raw = String(item.id ?? "");
						const label = config.formatter
							? config.formatter(String(item.name ?? ""))
							: String(item.name ?? "");
						return `<option value="${frappe.utils.escape_html(raw)}">${frappe.utils.escape_html(
							label
						)}</option>`;
					});
					$select.html(
						[`<option value="">${__("全部")}</option>`].concat(html).join("")
					);
					if (current) $select.val(current);
				};

				fill("budget_year", options.years);
				fill("budget_month", options.months);
				fill("group_dept_id", options.groups);
				fill("budget_type", options.budget_types);
				fill("subject_name", options.subjects);
				this.option_source = options;
				this.narrow_companies();
				return options;
			})
			.catch(() => {
				this.show_error(__("筛选项加载失败，请检查数据库配置"));
				return {};
			});
	}

	narrow_companies() {
		const options = this.option_source || {};
		const companies = options.companies || [];
		const $select = this.selects.company_dept_id;
		const current = $select.val();
		$select.html(
			[`<option value="">${__("全部")}</option>`]
				.concat(
					companies.map(
						(row) =>
							`<option value="${frappe.utils.escape_html(row.id)}">${frappe.utils.escape_html(
								row.name
							)}</option>`
					)
				)
				.join("")
		);
		if (current && companies.some((row) => row.id === current)) $select.val(current);
		this.narrow_departments();
	}

	narrow_departments() {
		const options = this.option_source || {};
		const departments = options.departments || [];
		const $select = this.selects.department_id;
		const current = $select.val();
		$select.html(
			[`<option value="">${__("全部")}</option>`]
				.concat(
					departments.map(
						(row) =>
							`<option value="${frappe.utils.escape_html(row.id)}">${frappe.utils.escape_html(
								row.name
							)}</option>`
					)
				)
				.join("")
		);
		if (current && departments.some((row) => row.id === current)) $select.val(current);
	}

	current_filters() {
		const filters = {};
		Object.entries(this.selects).forEach(([fieldname, $select]) => {
			const value = $select.val();
			if (value) filters[fieldname] = value;
		});
		return filters;
	}

	show_error(message) {
		this.$message.text(message || "").prop("hidden", !message);
	}

	refresh(reset_page) {
		if (reset_page) this.page_number = 1;
		this.show_error("");
		this.active_filters = this.current_filters();
		return Promise.allSettled([this.load_summary(), this.load_list()]);
	}

	reset() {
		Object.values(this.selects).forEach(($select) => $select.val(""));
		return this.refresh(true);
	}

	load_summary() {
		return frappe
			.call({
				method: "budget.api.quota.get_quota_summary",
				args: this.active_filters || {},
			})
			.then((response) => {
				const data = response.message || {};
				const totals = data.totals || {};
				this.$content.find('[data-stat="total_amount"]').text(bc_money(totals.total_amount));
				this.$content
					.find('[data-stat="quota_count"]')
					.text(Number(totals.quota_count || 0).toLocaleString("zh-CN"));
				this.$content
					.find('[data-stat="department_count"]')
					.text(Number(totals.department_count || 0).toLocaleString("zh-CN"));
				this.$content
					.find('[data-stat="detail_amount_mismatch_count"]')
					.text(Number(totals.detail_amount_mismatch_count || 0).toLocaleString("zh-CN"));
				this.rollups = data.rollups || {};
				this.render_rollup();
			})
			.catch(() => this.show_error(__("额度汇总加载失败，请检查数据库配置")));
	}

	render_rollup() {
		const rows = (this.rollups || {})[this.active_tab] || [];
		if (!rows.length) {
			this.$rollup.html(
				`<tr><td colspan="3" class="bc-empty">${__("暂无汇总数据")}</td></tr>`
			);
			return;
		}
		this.$rollup.html(
			rows
				.map(
					(row) => `<tr>
						<td>${frappe.utils.escape_html(String(row.name || __("待确认")))}</td>
						<td class="text-right">${Number(row.quota_count || 0).toLocaleString("zh-CN")}</td>
						<td class="text-right">${bc_money(row.total_amount)}</td>
					</tr>`
				)
				.join("")
		);
	}

	load_list() {
		const request = ++this.request_id;
		this.$detail.html(
			`<tr><td colspan="11" class="bc-empty">${__("额度配置加载中…")}</td></tr>`
		);
		this.$prev.prop("disabled", true);
		this.$next.prop("disabled", true);
		return frappe
			.call({
				method: "budget.api.quota.list_quotas",
				args: {
					...(this.active_filters || {}),
					page: this.page_number,
					page_size: this.page_size,
				},
			})
			.then((response) => {
				if (request !== this.request_id) return;
				const data = response.message || {};
				this.total = Number(data.total || 0);
				this.render_rows(data.rows || []);
				const pages = Math.max(Math.ceil(this.total / this.page_size), 1);
				this.$count.text(__("共 {0} 条", [this.total.toLocaleString("zh-CN")]));
				this.$page_info.text(__("第 {0} / {1} 页", [this.page_number, pages]));
				this.$prev.prop("disabled", this.page_number <= 1);
				this.$next.prop("disabled", this.page_number >= pages);
			})
			.catch(() => {
				if (request !== this.request_id) return;
				this.show_error(__("额度明细加载失败，请检查数据库配置"));
				this.$detail.html(
					`<tr><td colspan="11" class="bc-empty">${__("加载失败")}</td></tr>`
				);
			});
	}

	render_rows(rows) {
		if (!rows.length) {
			this.$detail.html(
				`<tr><td colspan="11" class="bc-empty">${__(
					"暂无审批通过后生效的预算额度"
				)}</td></tr>`
			);
			return;
		}
		const esc = (value) => frappe.utils.escape_html(String(value ?? ""));
		this.$detail.html(
			rows
				.map((row) => {
					const subjects = (row.subjects || []).length
						? `<div class="bc-subject-list">${row.subjects
								.map(
									(item) =>
										`<span class="bc-subject"><span>${esc(
											item.name || __("未分类预算科目")
										)}</span><span class="text-muted">${bc_money(
											item.amount
										)}</span></span>`
								)
								.join("")}</div>`
						: `<span class="text-muted">${__("未填写明细科目")}</span>`;
					const department = row.department_is_company
						? __("子公司本级")
						: esc(row.department_name || __("待确认"));
					const mismatch = row.detail_amount_mismatch
						? `<div><span class="bc-badge-warn">${__("明细与总额不一致")}</span></div>`
						: "";
					return `<tr>
						<td>${esc(row.budget_year || "-")}</td>
						<td>${esc(row.budget_month || "-")}</td>
						<td>${esc(row.group_name || __("待确认"))}</td>
						<td>${esc(row.company_name || __("待确认"))}</td>
						<td>${department}</td>
						<td>${subjects}</td>
						<td class="text-right">${bc_money(row.total_amount)}${mismatch}</td>
						<td class="text-right">${bc_money(row.subject_amount)}</td>
						<td>${esc(row.owner_name || row.owner_user_id || __("待确认"))}</td>
						<td><span class="bc-badge">${esc(
							bc_budget_type_label(row.budget_type)
						)}</span></td>
						<td>${esc(row.form_no || "-")}</td>
					</tr>`;
				})
				.join("")
		);
	}
}
