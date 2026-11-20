frappe.pages["budget-dashboard"].on_page_load = function (wrapper) {
	if (frappe.boot?.cn_budget_workbench) return;
	frappe.require("/assets/budget/js/budget_report.js", () => { wrapper.budget_dashboard = new BudgetReport(wrapper); });
};
frappe.pages["budget-dashboard"].on_page_show = function (wrapper) {
	if (frappe.boot?.cn_budget_workbench) return;
	window.dispatchEvent(new Event("resize"));
};
