/* 预算配置：1:1 对齐 dingtalk-budget 的 BudgetConfiguration 页面。
 *
 * 结构：页头（DingTalk Budget / 标题 / 副标题 / 返回列表 / 退出）→ 4 张统计卡
 * → 7 项筛选 + 重置/查询 →「组织额度汇总」四个页签 →「预算额度明细」分页表。
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
		// 源页面自带页头，隐藏 Desk 默认的标题栏避免重复
		$(wrapper).addClass("bc-page-wrap");
		this.page_size = 20;
		this.page_number = 1;
		this.active_tab = "group";
		this.total = 0;
		this.rollups = {};
		this.request_id = 0;
		this.setup_content();
		this.load_options().then(() => this.refresh(true));
	}

	/* ---------------------------------------------------------- 骨架 */
	setup_content() {
		this.$content = $(`
			<div class="bc-page">
				<div class="bc-container">
					<header class="bc-header">
						<div>
							<p class="bc-eyebrow">DingTalk Budget</p>
							<h1 class="bc-title">${__("预算配置")}</h1>
							<p class="bc-subtitle">${__(
								"审批通过的预算申请会形成生效额度，按组织层级查看与汇总。"
							)}</p>
						</div>
						<div class="bc-header-actions">
							<button type="button" class="bc-button bc-back">${__("返回预算列表")}</button>
							<button type="button" class="bc-button bc-logout">${__("退出登录")}</button>
						</div>
					</header>

					<div class="bc-message" role="alert" hidden></div>

					<section class="bc-stats">
						${[
							["total_amount", "生效预算额度"],
							["quota_count", "生效预算单"],
							["department_count", "当前可见部门"],
							["detail_amount_mismatch_count", "明细金额待核对"],
						]
							.map(
								([key, label]) => `<div class="bc-stat-card">
									<div class="bc-stat-label">${__(label)}</div>
									<div class="bc-stat-value" data-stat="${key}">—</div>
								</div>`
							)
							.join("")}
					</section>

					<section class="bc-panel">
						<div class="bc-filter-grid"></div>
						<div class="bc-filter-actions">
							<button type="button" class="bc-button bc-reset">${__("重置")}</button>
							<button type="button" class="bc-button bc-primary bc-search">${__("查询")}</button>
						</div>
					</section>

					<section class="bc-panel">
						<div class="bc-section-head">
							<h2 class="bc-section-title">${__("组织额度汇总")}</h2>
							<div class="bc-tabs">
								${BC_SUMMARY_TABS.map(
									(tab, index) =>
										`<button type="button" class="bc-tab${
											index === 0 ? " active" : ""
										}" data-tab="${tab.key}">${tab.label}</button>`
								).join("")}
							</div>
						</div>
						<div class="bc-table-wrap">
							<table class="bc-table bc-table-rollup">
								<thead><tr>
									<th>${__("集团")}</th>
									<th class="bc-num">${__("生效预算单")}</th>
									<th class="bc-num">${__("预算额度（元）")}</th>
								</tr></thead>
								<tbody><tr><td colspan="3" class="bc-empty">${__("正在加载…")}</td></tr></tbody>
							</table>
						</div>
					</section>

					<section class="bc-panel">
						<div class="bc-section-head">
							<h2 class="bc-section-title">${__("预算额度明细")}</h2>
							<span class="bc-subtle bc-count"></span>
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
									<th class="bc-num">${__("预算金额（元）")}</th>
									<th class="bc-num">${__("科目金额（元）")}</th>
									<th>${__("负责人")}</th>
									<th>${__("预算类型")}</th>
									<th>${__("表单编号")}</th>
								</tr></thead>
								<tbody><tr><td colspan="11" class="bc-empty">${__("额度配置加载中…")}</td></tr></tbody>
							</table>
						</div>
						<div class="bc-pagination">
							<button type="button" class="bc-button bc-prev">${__("上一页")}</button>
							<span class="bc-page-info"></span>
							<button type="button" class="bc-button bc-next">${__("下一页")}</button>
						</div>
					</section>
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
		this.$content.find(".bc-tab").on("click", (event) => {
			event.preventDefault();
			this.active_tab = event.currentTarget.dataset.tab;
			this.$content.find(".bc-tab").removeClass("active");
			$(event.currentTarget).addClass("active");
			const tab = BC_SUMMARY_TABS.find((item) => item.key === this.active_tab);
			this.$rollup_head.text(tab ? tab.name : __("名称"));
			this.render_rollup();
		});
		this.$content.find(".bc-search").on("click", () => this.refresh(true));
		this.$content.find(".bc-reset").on("click", () => this.reset());
		this.$content.find(".bc-back").on("click", () => {
			frappe.set_route("List", "Budget Ledger Entry");
		});
		this.$content.find(".bc-logout").on("click", () => frappe.app.logout());

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
				<select class="bc-select"></select>
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
			.bc-page-wrap .page-head { display: none; }
			.bc-page-wrap .page-main { padding: 0; }
			.bc-page-wrap .layout-main-section { padding: 0; border: 0; box-shadow: none; background: transparent; }
			.bc-page-wrap .page-body { background: #f4f7fb; }
			.bc-page { min-height: 100%; width: 100%; overflow-x: hidden; background: #f4f7fb; color: #172033; }
			.bc-container { width: 100%; max-width: 1520px; margin: 0 auto; padding: 24px 24px 42px; box-sizing: border-box; }
			.bc-header { display: flex; justify-content: space-between; align-items: flex-start; gap: 24px; margin-bottom: 22px; }
			.bc-eyebrow { margin: 0 0 7px; color: #0f766e; font-size: 12px; font-weight: 700; text-transform: uppercase; }
			.bc-title { margin: 0; font-size: 26px; line-height: 1.25; font-weight: 700; }
			.bc-subtitle { margin: 8px 0 0; color: #657089; font-size: 14px; }
			.bc-header-actions { display: flex; gap: 10px; flex-shrink: 0; }
			.bc-button { height: 34px; padding: 0 13px; border: 1px solid #cbd5e1; border-radius: 5px; background: #fff; color: #344054; font-size: 13px; cursor: pointer; }
			.bc-button:disabled { cursor: default; opacity: 0.45; }
			.bc-primary { border-color: #0f766e; background: #0f766e; color: #fff; }
			.bc-message { margin-bottom: 16px; padding: 12px 16px; border: 1px solid #fecaca; border-radius: 6px; background: #fff1f2; color: #be123c; font-size: 13px; }
			.bc-stats { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 14px; margin-bottom: 16px; }
			.bc-stat-card { min-height: 98px; padding: 17px 18px; border: 1px solid #dde4ee; border-radius: 6px; background: #fff; box-sizing: border-box; }
			.bc-stat-label { color: #68748b; font-size: 13px; }
			.bc-stat-value { margin-top: 12px; color: #172033; font-size: 22px; line-height: 1; font-weight: 700; font-variant-numeric: tabular-nums; }
			.bc-panel { border: 1px solid #dde4ee; border-radius: 6px; background: #fff; margin-bottom: 16px; }
			.bc-filter-grid { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 14px 16px; padding: 20px; border-bottom: 1px solid #e7ecf3; }
			.bc-filter { display: grid; grid-template-columns: 76px minmax(0, 1fr); align-items: center; gap: 8px; margin: 0; }
			.bc-filter-label { color: #526078; font-size: 13px; text-align: right; font-weight: 400; }
			.bc-select { width: 100%; height: 34px; min-width: 0; padding: 0 9px; border: 1px solid #cbd5e1; border-radius: 4px; outline: none; background: #fff; color: #25324b; font-size: 13px; }
			.bc-filter-actions { display: flex; align-items: center; justify-content: flex-end; gap: 10px; padding: 14px 20px 18px; }
			.bc-section-head { display: flex; align-items: center; justify-content: space-between; gap: 16px; flex-wrap: wrap; padding: 15px 20px; border-bottom: 1px solid #e7ecf3; }
			.bc-section-title { margin: 0; font-size: 16px; font-weight: 700; }
			.bc-tabs { display: flex; gap: 4px; }
			.bc-tab { height: 32px; padding: 0 12px; border: 1px solid transparent; border-radius: 4px; background: transparent; color: #637087; font-size: 13px; cursor: pointer; }
			.bc-tab.active { border-color: #b6ded8; background: #e9f7f4; color: #0f766e; font-weight: 700; }
			.bc-table-wrap { width: 100%; overflow-x: auto; }
			.bc-table { width: 100%; min-width: 1180px; border-collapse: collapse; font-size: 13px; }
			.bc-table th { padding: 11px 12px; border-bottom: 1px solid #dfe6ef; background: #f8fafc; color: #516078; text-align: left; font-size: 12px; font-weight: 700; white-space: nowrap; }
			.bc-table td { padding: 12px; border-bottom: 1px solid #edf1f5; color: #26334b; vertical-align: top; }
			.bc-table .bc-num { text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; }
			.bc-table .bc-empty { padding: 34px 20px; color: #7a879b; text-align: center; font-size: 14px; }
			.bc-table-rollup { min-width: 480px; }
			.bc-subject-list { display: flex; flex-direction: column; gap: 4px; min-width: 180px; }
			.bc-subject { display: flex; align-items: center; justify-content: space-between; gap: 12px; color: #46536c; white-space: nowrap; }
			.bc-subtle { color: #7a879b; }
			.bc-type-badge { display: inline-flex; padding: 3px 7px; border-radius: 4px; background: #eef2ff; color: #4f46e5; font-size: 12px; white-space: nowrap; }
			.bc-warning-badge { display: inline-flex; margin-top: 5px; padding: 2px 6px; border-radius: 4px; background: #fff7e6; color: #b45309; font-size: 11px; }
			.bc-pagination { display: flex; align-items: center; justify-content: flex-end; gap: 10px; padding: 14px 20px; color: #68748b; font-size: 13px; }
			@media (max-width: 1200px) {
				.bc-container { padding: 16px 16px 32px; }
				.bc-stats { grid-template-columns: repeat(2, minmax(0, 1fr)); }
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
						<td class="bc-num">${Number(row.quota_count || 0).toLocaleString("zh-CN")}</td>
						<td class="bc-num">${bc_money(row.total_amount)}</td>
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
										)}</span><span class="bc-subtle">${bc_money(
											item.amount
										)}</span></span>`
								)
								.join("")}</div>`
						: `<span class="bc-subtle">${__("未填写明细科目")}</span>`;
					const department = row.department_is_company
						? __("子公司本级")
						: esc(row.department_name || __("待确认"));
					const mismatch = row.detail_amount_mismatch
						? `<span class="bc-warning-badge">${__("明细与总额不一致")}</span>`
						: "";
					return `<tr>
						<td>${esc(row.budget_year || "-")}</td>
						<td>${esc(row.budget_month || "-")}</td>
						<td>${esc(row.group_name || __("待确认"))}</td>
						<td>${esc(row.company_name || __("待确认"))}</td>
						<td>${department}</td>
						<td>${subjects}</td>
						<td class="bc-num">${bc_money(row.total_amount)}${mismatch}</td>
						<td class="bc-num">${bc_money(row.subject_amount)}</td>
						<td>${esc(row.owner_name || row.owner_user_id || __("待确认"))}</td>
						<td><span class="bc-type-badge">${esc(
							bc_budget_type_label(row.budget_type)
						)}</span></td>
						<td>${esc(row.form_no || "-")}</td>
					</tr>`;
				})
				.join("")
		);
	}
}
