const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

function dashboard() {
	const context = vm.createContext({
		frappe: {pages: {}, utils: {escape_html: value => String(value)}},
		window: {echarts: {}}, __: value => value, setTimeout() {},
	});
	vm.runInContext(fs.readFileSync(path.join(__dirname, "../budget/page/budget_dashboard/budget_dashboard.js"), "utf8"), context);
	const instance = vm.runInContext("Object.create(BudgetDashboard.prototype)", context);
	const rendered = new Map(), charts = new Map();
	instance.$content = {find(selector) {
		return {length: 1, text(value) {rendered.set(selector, value); return this;},
			html(value) {rendered.set(selector, value); return this;}, css() {return this;}};
	}};
	instance.$approval_body = {html(value) {rendered.set("approval-table", value);}};
	instance.destroy_charts = () => charts.clear();
	instance.resize_charts = () => {};
	instance.mount = (echarts, name, options) => charts.set(name, options);
	instance.bind_department_click = () => {};
	return {instance, rendered, charts};
}

test("unknown expense amounts render as unavailable while real zero stays zero", () => {
	const {instance, rendered} = dashboard();
	instance.render_stats({approvedTotal: null, budgetSubmittedApprovedTotal: null, productionTotal: 0});
	assert.equal(rendered.get('[data-stat="approvedTotal"]'), "—");
	assert.equal(rendered.get('[data-stat="budgetSubmittedApprovedTotal"]'), "—");
	assert.equal(rendered.get('[data-stat="productionTotal"]'), "0.00");
	const context = vm.createContext({frappe: {listview_settings: {}}});
	vm.runInContext(fs.readFileSync(path.join(__dirname, "../budget/doctype/budget_ledger_entry/budget_ledger_entry_list.js"), "utf8"), context);
	assert.equal(vm.runInContext("format_wan(null)", context), "—");
	assert.equal(vm.runInContext("format_wan(0)", context), "¥0.00");
});

test("unavailable approval response clears prior metrics and explains missing data", () => {
	const {instance, rendered} = dashboard();
	instance.render_approval({summary: {total: 20}, byProcess: []});
	instance.render_approval({summary: {}, byProcess: []}, false);
	assert.equal(rendered.get("[data-approval]"), "—");
	assert.match(rendered.get("approval-table"), /Approval data unavailable/);
	assert.doesNotMatch(rendered.get("approval-table"), /当前期间没有审批实例/);
});

test("missing expense source hides expense series and dependent charts but preserves budgets", async () => {
	const {instance, rendered, charts} = dashboard();
	const data = {
		availability: {expenses: false, pending_expenses: false},
		trend: [{month: "2026-11", production: 100, nonProduction: 200, actualExpense: null}],
		region_distribution: [{region: "CN", budget: 300, expense: null}],
	};
	await instance.render_charts(data);
	assert.equal(charts.get("region-dist").series.length, 1);
	assert.equal(charts.get("region-dist").series[0].data[0], 300);
	assert.ok(!charts.get("trend").series.some(row => row.name === "实际支出"));
	for (const name of ["rate", "compare", "exec-status"]) {
		assert.match(rendered.get(`[data-chart="${name}"]`), /Expense data unavailable/);
	}
	data.availability.expenses = true;
	data.availability.pending_expenses = true;
	data.trend[0].actualExpense = 0;
	data.region_distribution[0].expense = 0;
	await instance.render_charts(data);
	assert.equal(charts.get("region-dist").series.length, 2);
	assert.equal(charts.get("trend").series.find(row => row.name === "实际支出").data[0], 0);
});
