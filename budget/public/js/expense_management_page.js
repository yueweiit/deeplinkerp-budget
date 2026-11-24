(function (root) {
  root.ExpenseManagementPage = {
    register(route) {
      frappe.pages[route].on_page_load = (wrapper) => {
        frappe.require(
          [
            "/assets/deeplinkerp_branding/css/mexico_expenses.css",
            "/assets/deeplinkerp_branding/js/mexico_expense_date_range.js",
            "/assets/deeplinkerp_branding/js/mexico_expense_money.js",
            "/assets/budget/css/expense_management.css",
            "/assets/budget/js/expense_sync.js",
            "/assets/budget/js/expense_management.js",
          ],
          () => {
            if (!wrapper.expense_management)
              wrapper.expense_management = new root.ExpenseManagement.Workbench(
                wrapper,
                route
              );
          }
        );
      };
      frappe.pages[route].on_page_show = (wrapper) =>
        wrapper.expense_management?.show();
      frappe.pages[route].on_page_hide = (wrapper) =>
        wrapper.expense_management?.hide();
    },
  };
})(globalThis);
