frappe.pages["budget-dashboard"].on_page_load = function (wrapper) {
	if (frappe.boot?.cn_budget_workbench) {
		frappe.require("/assets/deeplinkerp_branding/js/china_expenses_page.js", () => {
			DeepLinkERPChinaExpensesPage.register("budget-dashboard");
			frappe.pages["budget-dashboard"].on_page_load(wrapper);
		});
	} else {
		frappe.require("/assets/budget/js/budget_report.js", () => { wrapper.budget_dashboard = new BudgetReport(wrapper); });
	}
};
frappe.pages["budget-dashboard"].on_page_show = function (wrapper) {
	wrapper.cn_expenses?.show();
	window.dispatchEvent(new Event("resize"));
};
