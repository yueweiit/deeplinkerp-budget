/* 预算台账列表：
 * - 生产 / 非生产用 tab 切页；
 * - 子公司 / 部门 / 状态 / 执行地区用下拉筛选（候选值 = 设置页 + 台账现有值 + 系统主数据）；
 * - 预算月份用只能选月份的日历弹层；
 * 文案一律英文原文 + translations/*.csv，跟随系统语言。
 */
frappe.listview_settings["Budget Ledger Entry"] = {
	onload(listview) {
		// 台账由同步任务写入，不允许手工新增：隐藏右上角「添加」按钮
		listview.page.clear_primary_action?.();

		// 「详情」「钉钉原单」由 subject 列注入，事件委托挂在 page.main 上（列表重绘不影响）
		listview.page.main.on("click", ".budget-detail-link", (event) => {
			event.preventDefault();
			event.stopPropagation();
			const name = $(event.currentTarget).attr("data-name");
			if (name) show_budget_detail(name);
		});
		listview.page.main.on("click", ".budget-dingtalk-link", (event) => {
			event.preventDefault();
			event.stopPropagation();
			const name = $(event.currentTarget).attr("data-name");
			if (name) open_budget_dingtalk(name);
		});

		// 列表首列是 title_field（form_no）的 Subject 列：v16 的 get_subject_text 会调用
		// settings.formatters，但返回值经 get_link_element 当纯文本插入，HTML 会被原样
		// 显示成标签。所以 formatter 只保留纯文本，操作入口改用 DOM 追加到 subject 元素上。
		const original_subject_element = listview.get_subject_element.bind(listview);
		listview.get_subject_element = function (doc, title) {
			const element = original_subject_element(doc, title);
			const name = String((doc && doc.name) || "");
			if (!name || $(element).find(".budget-row-actions").length) {
				return element;
			}
			const escaped = frappe.utils.escape_html(name);
			$(element).append(
				`<span class="budget-row-actions">
					<a class="budget-detail-link" href="#" data-name="${escaped}">${__("详情")}</a>
					<a class="budget-dingtalk-link" href="#" data-name="${escaped}">${__("钉钉原单")}</a>
				</span>`
			);
			return element;
		};

		const esc = (value) =>
			String(value === null || value === undefined ? "" : value)
				.replace(/&/g, "&amp;")
				.replace(/</g, "&lt;")
				.replace(/>/g, "&gt;")
				.replace(/"/g, "&quot;");

		const getFilters = () => (listview.filter_area ? listview.filter_area.get() || [] : []);
		const filterValue = (fieldname) => {
			const hit = getFilters().find((f) => f[1] === fieldname);
			return hit ? hit[3] : undefined;
		};

		// ---------- 生产 / 非生产 tab ----------
		const kindField = "budget_kind";
		const kinds = [
			{ value: "", label: __("All") },
			{ value: "production", label: __("Production Budget") },
			{ value: "non_production", label: __("Non Production Budget") },
		];

		// 用 button 而不是 <a href="#">：Desk 的 router 会接管所有 <a> 点击
		const $tabs = $(
			'<ul class="nav nav-tabs budget-kind-tabs" style="margin: 0 0 10px;"></ul>'
		);
		kinds.forEach((kind) => {
			$(
				`<li class="nav-item"><button type="button" class="nav-link" data-kind="${kind.value}">${kind.label}</button></li>`
			).appendTo($tabs);
		});

		listview.page.main.find(".budget-kind-tabs").remove();
		const $anchor = listview.$frappe_list?.length
			? listview.$frappe_list
			: listview.page.main.find(".frappe-list").first();
		if ($anchor.length) {
			$tabs.insertBefore($anchor);
		} else {
			$tabs.appendTo(listview.page.main);
		}

		// ---------- 通用筛选写入 ----------
		const applyEquals = (fieldname, value) => {
			const area = listview.filter_area;
			if (!area) return;
			const hasFilter = getFilters().some((f) => f[1] === fieldname);
			const after = value
				? () => area.add([[listview.doctype, fieldname, "=", value]])
				: () => listview.refresh();
			(hasFilter ? area.remove(fieldname) : Promise.resolve()).then(after).then(syncControls);
		};

		const applyKind = (value) => {
			if (!listview.filter_area || listview._budgetKindBusy) return;
			const kept = getFilters().filter((f) => f[1] !== kindField);
			const next = value ? [...kept, [listview.doctype, kindField, "=", value]] : kept;

			// filter_area.clear() 内部的 set_value("") 是异步的，必须等它跑完再写新值，
			// 否则新值会被收尾的清空操作覆盖（表现为点了 tab 没反应）。
			listview._budgetKindBusy = true;
			listview.filter_area
				.clear(false)
				.then(() => (next.length ? listview.filter_area.add(next, false) : null))
				.then(() => {
					listview._budgetKindBusy = false;
					listview.refresh();
					syncControls();
				})
				.catch(() => {
					listview._budgetKindBusy = false;
					syncControls();
				});
		};

		const markActive = () => {
			const active = String(filterValue(kindField) || "");
			$tabs.find("[data-kind]").each(function () {
				const on = (this.dataset.kind || "") === active;
				$(this).toggleClass("active", on).attr("aria-pressed", on ? "true" : "false");
			});
		};

		$tabs.on("click", "[data-kind]", function (event) {
			event.preventDefault();
			event.stopPropagation();
			applyKind(this.dataset.kind || "");
		});

		// ---------- 导出 XLSX（列与列表一致，跟随当前筛选） ----------
		// v16 的 Page 已移除 add_custom_button，用 add_inner_button 挂到工具栏；
		// 老版本没有 add_inner_button 时再退回 add_custom_button。
		const exportXlsx = () => {
			const filters = listview.get_filters_for_args?.() || [];
			const url =
				"/api/method/budget.api.budget_ledger.export_budget_entries_xlsx?filters=" +
				encodeURIComponent(JSON.stringify(filters));
			window.open(url, "_blank");
		};
		if (listview.page.add_inner_button) {
			listview.page.add_inner_button(__("Export XLSX"), exportXlsx);
		} else if (listview.page.add_custom_button) {
			listview.page.add_custom_button(__("Export XLSX"), exportXlsx);
		}

		// 「编号」(name) 与「表单编号」(title_field = form_no) 值相同，只留一个
		const $dupField = listview.page.fields_dict?.form_no?.$wrapper;
		$dupField?.hide();

		// ---------- 下拉筛选（子公司 / 部门 / 状态 / 执行地区） ----------
		const selectFields = [
			{ fieldname: "company", label: __("Company"), key: "company_options", width: 240 },
			{ fieldname: "department", label: __("Department"), key: "department_options", width: 300 },
			{ fieldname: "status", label: __("Status"), key: "status_options", width: 150 },
			{
				fieldname: "execution_region",
				label: __("Execution Region"),
				key: "execution_region_options",
				width: 140,
			},
		];

		const $selects = {};
		// 单独占一行，避免和 Desk 原生的「编号 / 过滤器 / 排序」挤成一团
		const $filterRow = $('<div class="budget-filters"></div>').appendTo(
			listview.page.page_form
		);
		selectFields.forEach((config) => {
			const $control = $(`<div class="budget-filter">
				<span class="budget-label">${config.label}</span>
				<select class="form-control input-xs" style="width:${config.width}px;"></select>
			</div>`);
			const $select = $control.find("select");
			$select.attr("title", "");
			$select.on("change", function () {
				const value = this.value || "";
				if (config.fieldname === "company") {
					applyEquals("company", value);
					renderDepartmentOptions(value);
					// 已选部门不属于新子公司时清掉，避免出现空结果
					const department = String(filterValue("department") || "").trim();
					const allowed = departmentsByCompany.get(String(value).trim());
					if (department && value && allowed && !allowed.has(department)) {
						applyEquals("department", "");
					}
				} else {
					applyEquals(config.fieldname, value);
				}
			});
			$control.appendTo($filterRow);
			$selects[config.fieldname] = $select;
		});

		const fillSelect = ($select, values) => {
			const current = $select.val() || "";
			$select.html(
				[`<option value="">${__("All")}</option>`]
					.concat(values.map((v) => `<option value="${esc(v)}">${esc(v)}</option>`))
					.join("")
			);
			if (current && !values.includes(current)) {
				$select.prepend(`<option value="${esc(current)}">${esc(current)}</option>`);
			}
			$select.val(current);
			$select.attr("title", current || "");
		};

		// 子公司 → 部门 的对应关系：优先取 OA 组织树，读不到时退回
		// 设置页配置 ∪ 台账里成对出现的值
		// （子公司 = OA 部门树第 3 层，部门 = 该节点的子部门）
		const departmentsByCompany = new Map();
		let allDepartmentValues = [];

		const addPair = (company, department) => {
			if (!department) return;
			const key = String(company || "").trim();
			if (!departmentsByCompany.has(key)) departmentsByCompany.set(key, new Set());
			departmentsByCompany.get(key).add(department);
		};

		const sortedValues = (set) =>
			[...set].sort((a, b) => String(a).localeCompare(String(b), "zh-CN"));

		const renderDepartmentOptions = (company) => {
			const $select = $selects.department;
			if (!$select) return;
			const current =
				company === undefined ? String(filterValue("company") || "").trim() : String(company || "").trim();
			// 选了子公司就只列它自己的部门（组织树里没有子节点时只剩「全部」），
			// 不再回退全量，避免跨公司筛出空结果。
			const values = current
				? sortedValues(departmentsByCompany.get(current) || new Set())
				: allDepartmentValues;
			fillSelect($select, values);
		};

		const loadSelectOptions = () => {
			const fields = selectFields.map((c) => c.fieldname);
			// 子公司/部门严格对齐 OA 组织树（dingtalk_oa.ding_department_tree）：
			// 树读得到时只用树里的公司/部门，不掺台账里的历史残留值；
			// 读不到（OA 库不通/未配置）才退回设置页配置 + 台账已有值。
			Promise.all([
				// Single DocType 的 name 就是它自己
				frappe.db.get_doc("Budget Settings", "Budget Settings").catch(() => ({})),
				frappe.db.get_list(listview.doctype, { fields, limit_page_length: 0 }).catch(() => []),
				frappe
					.call({ method: "budget.api.approval.list_organization_options" })
					.then((r) => r.message || {})
					.catch(() => null),
			]).then(([settings, rows, org]) => {
				// 按优先级合并：设置页 → 台账已有值 → 系统主数据；组内排序，组间保序
				const merge = (...lists) => {
					const out = [];
					lists.forEach((list) => {
						(list || [])
							.slice()
							.sort((a, b) => String(a).localeCompare(String(b), "zh-CN"))
							.forEach((value) => {
								if (value && !out.includes(value)) out.push(value);
							});
					});
					return out;
				};
				const fromDoc = (key) =>
					((settings || {})[key] || []).map((row) => String(row.value || "").trim());
				const fromRows = (fieldname) =>
					(rows || []).map((row) => String(row[fieldname] || "").trim());

				// 组织树：公司取第 3 层，部门取叶子节点，与台账 company/department 口径一致
				const orgCompanies = ((org || {}).companies || [])
					.map((row) => String(row.name || "").trim())
					.filter(Boolean);
				const orgPairs = ((org || {}).departments || []).map((row) => ({
					company: String(row.company_name || "").trim(),
					department: String(row.name || "").trim(),
				}));
				const useOrgTree = orgCompanies.length > 0;

				// 部门对应关系
				departmentsByCompany.clear();
				const pairSources = useOrgTree
					? [orgPairs]
					: [
							((settings || {}).department_options || []).map((row) => ({
								company: String(row.company || "").trim(),
								department: String(row.department || "").trim(),
							})),
							(rows || []).map((row) => ({
								company: String(row.company || "").trim(),
								department: String(row.department || "").trim(),
							})),
						];
				pairSources.forEach((pairs) =>
					pairs.forEach((pair) => addPair(pair.company, pair.department))
				);

				const departmentUnion = new Set();
				departmentsByCompany.forEach((set) => set.forEach((d) => departmentUnion.add(d)));
				allDepartmentValues = sortedValues(departmentUnion);

				const sources = {
					// 组织树可用时，公司候选不再包含台账里的历史残留值
					company: useOrgTree
						? [orgCompanies]
						: [fromDoc("company_options"), fromRows("company")],
					status: [fromDoc("status_options"), fromRows("status")],
					execution_region: [fromDoc("execution_region_options"), fromRows("execution_region")],
				};

				selectFields.forEach((config) => {
					if (config.fieldname === "department") return;
					fillSelect($selects[config.fieldname], merge(...sources[config.fieldname]));
				});
				syncControls();
			});
		};

		// ---------- 预算月份：只能选月份的日历 ----------
		const monthField = "budget_month";
		const $monthControl = $(`<div class="budget-filter budget-month-filter">
			<span class="budget-label">${__("Budget Month")}</span>
			<button type="button" class="btn btn-default btn-xs budget-month-trigger">
				<span class="budget-month-text">${__("All Months")}</span>
				<span class="budget-caret">▾</span>
			</button>
			<div class="budget-month-pop" hidden>
				<button type="button" class="btn btn-default btn-xs budget-month-clear">${__(
					"All Months"
				)}</button>
				<div class="budget-month-head">
					<button type="button" class="btn btn-default btn-xs" data-step="-1">‹</button>
					<span class="budget-month-year"></span>
					<button type="button" class="btn btn-default btn-xs" data-step="1">›</button>
				</div>
				<div class="budget-month-grid"></div>
			</div>
		</div>`);
		// 月份是最常用的筛选，放在这一行最前面
		$monthControl.prependTo($filterRow);

		const $monthTrigger = $monthControl.find(".budget-month-trigger");
		const $monthText = $monthControl.find(".budget-month-text");
		const $monthPop = $monthControl.find(".budget-month-pop");
		const $monthYear = $monthControl.find(".budget-month-year");
		const $monthGrid = $monthControl.find(".budget-month-grid");
		const monthState = { year: new Date().getFullYear() };

		const currentMonth = () => {
			const value = filterValue(monthField);
			const start = Array.isArray(value) ? value[0] : value;
			const text = String(start || "").slice(0, 7);
			return /^\d{4}-\d{2}$/.test(text) ? text : "";
		};

		const renderMonthPop = () => {
			const active = currentMonth();
			$monthYear.text(monthState.year);
			const cells = [];
			for (let month = 1; month <= 12; month++) {
				const value = `${monthState.year}-${String(month).padStart(2, "0")}`;
				cells.push(
					`<button type="button" class="btn btn-xs btn-default budget-month-cell${
						value === active ? " btn-primary active" : ""
					}" data-month="${value}">${month}</button>`
				);
			}
			$monthGrid.html(cells.join(""));
		};

		const openMonthPop = () => {
			const active = currentMonth();
			if (active) monthState.year = Number(active.slice(0, 4));
			$monthPop.prop("hidden", false);
			renderMonthPop();
		};

		const closeMonthPop = () => $monthPop.prop("hidden", true);

		const applyMonth = (value) => {
			const area = listview.filter_area;
			if (!area) return;
			const match = /^(\d{4})-(\d{2})$/.exec(String(value || ""));
			const range = match
				? [
						`${match[1]}-${match[2]}-01`,
						`${match[1]}-${match[2]}-${String(
							new Date(Number(match[1]), Number(match[2]), 0).getDate()
						).padStart(2, "0")}`,
					]
				: null;
			const hasFilter = getFilters().some((f) => f[1] === monthField);
			const after = range
				? () => area.add([[listview.doctype, monthField, "between", range]])
				: () => listview.refresh();
			(hasFilter ? area.remove(monthField) : Promise.resolve()).then(after).then(syncControls);
		};

		$monthTrigger.on("click", (event) => {
			event.preventDefault();
			event.stopPropagation();
			if ($monthPop.prop("hidden")) openMonthPop();
			else closeMonthPop();
		});
		$monthPop.on("click", (event) => {
			event.stopPropagation();
			const nav = event.target.closest("[data-step]");
			if (nav) {
				monthState.year += Number(nav.dataset.step);
				renderMonthPop();
				return;
			}
			const cell = event.target.closest("[data-month]");
			if (cell) {
				applyMonth(cell.dataset.month);
				closeMonthPop();
			}
		});
		$monthControl.find(".budget-month-clear").on("click", (event) => {
			event.stopPropagation();
			applyMonth("");
			closeMonthPop();
		});
		$(document).off("click.budgetMonth").on("click.budgetMonth", closeMonthPop);

		// ---------- 控件状态回填 ----------
		function syncControls() {
			markActive();
			selectFields.forEach((config) => {
				const value = String(filterValue(config.fieldname) || "");
				const $select = $selects[config.fieldname];
				if (!$select) return;
				if ($select.val() !== value) {
					const matched = $select.find("option").filter(function () {
						return this.value === value;
					}).length;
					if (value && !matched) {
						$select.append(`<option value="${esc(value)}">${esc(value)}</option>`);
					}
					$select.val(value);
				}
				// 长名称会被 select 截断，hover 时给完整值
				$select.attr("title", $select.val() || "");
			});
			// 部门下拉跟随子公司
			renderDepartmentOptions();
			const month = currentMonth();
			$monthText.text(month || __("All Months"));
			$monthTrigger.toggleClass("btn-primary", Boolean(month));
			if (month) monthState.year = Number(month.slice(0, 4));
			renderMonthPop();
		}

		// ---------- 样式 ----------
		if (!document.getElementById("budget-list-style")) {
			$(`
				<style id="budget-list-style">
					/* 单独一行的筛选条，跟 Desk 原生的「编号 / 过滤器 / 排序」分开 */
					.budget-filters {
						display: flex; align-items: center; flex-wrap: wrap; gap: 8px 14px;
						flex: 0 0 100%; width: 100%; padding: 6px 0 4px;
					}
					.budget-filter { position: relative; display: flex; align-items: center; gap: 6px; }
					.budget-filter .budget-label {
						font-size: 12px; color: #74808c; white-space: nowrap;
					}
					.budget-filter select.form-control {
						height: 28px; padding: 0 8px; font-size: 12px; border-radius: 6px;
					}
					.budget-month-trigger {
						height: 28px; padding: 0 10px; font-size: 12px; border-radius: 6px;
						display: inline-flex; align-items: center; gap: 6px;
					}
					.budget-caret { font-size: 10px; color: #8d99a6; }
					.budget-month-pop {
						position: absolute; top: calc(100% + 4px); left: 0; z-index: 1050;
						width: 232px; padding: 8px; background: #fff;
						border: 1px solid #d1d8dd; border-radius: 8px;
						box-shadow: 0 8px 24px rgba(23, 43, 66, 0.14);
					}
					.budget-month-pop .budget-month-clear { width: 100%; margin-bottom: 6px; }
					.budget-month-head {
						display: flex; align-items: center; justify-content: space-between;
						padding: 0 2px 6px; font-size: 12px; font-weight: 600;
					}
					.budget-month-grid {
						display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 4px;
					}
					.budget-month-cell { width: 100%; }
					/* 行内操作：表单编号 + 详情 / 钉钉原单 */
					.budget-row-actions {
						display: inline-flex; align-items: center; gap: 8px;
						margin-left: 10px; font-size: 12px;
					}
					.budget-detail-link, .budget-dingtalk-link { white-space: nowrap; }
				</style>
			`).appendTo(document.head);
		}

		// ---------- 启动 ----------
		syncControls();
		loadSelectOptions();
		listview._budgetKindTabs = { $tabs, markActive, syncControls };
	},

	refresh(listview) {
		listview._budgetKindTabs?.syncControls();
	},

	formatters: {
		// 首列是 title_field（form_no）的 Subject 列，formatter 只返回纯文本；
		// 「详情 / 钉钉原单」由 onload 里的 get_subject_element 包装注入 DOM，避免被转义。
		form_no(value, df, doc) {
			return String(value || (doc && doc.name) || "");
		},
		// 存储值是 production / non_production，展示跟随系统语言
		budget_kind(value) {
			if (value === "production") return __("Production Budget");
			if (value === "non_production") return __("Non Production Budget");
			return value || "";
		},
		// 存储是 Date（便于区间筛选），展示只到月份
		budget_month(value) {
			return value ? String(value).slice(0, 7) : "";
		},
	},
};

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

	mount_detail_chart(dialog, budget, expense);
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
				color: "#2490ef",
				barMaxWidth: 40,
				data: [budget.operation || 0, budget.hr || 0, 0, 0, budget.office || 0, 0],
				label: { show: true, position: "top", formatter: (params) => format_wan(params.value) },
			},
			{
				name: __("支出"),
				type: "bar",
				color: "#94a3b8",
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
