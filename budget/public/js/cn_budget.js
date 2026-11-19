/* CN hosts the existing budget ledger, quotas and execution report. No expense totals are merged. */
(function (root, factory) {
    const api = factory(root);
    root.CNBudget = api;
    if (typeof module === "object" && module.exports) module.exports = factory;
})(globalThis, function (root) {
    "use strict";
    const t = value => root.__ ? root.__(value) : value;
    const esc = value => String(value ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;", "<":"&lt;", ">":"&gt;", '"':"&quot;", "'":"&#39;"}[c]));
    const money = value => value == null ? "—" : root.format_currency ? root.format_currency(value, "CNY", 2) : Number(value).toFixed(2);
    const kind = value => t(value === "production" ? "Production Budget" : value === "non_production" ? "Non-production Budget" : "Pending Review");
    const call = async (method, args = {}) => (await root.frappe.call({method, args})).message;
    const scope = "Budgets use complete budget months and CNY amounts. Budget plans, effective quotas and actual expenses are shown separately.";
    const help = label => root.DeepLinkERPMexicoExpenseInsights?.help(t(label)) || "";
    const btn = (label, action, attrs = "") => `<button type="button" class="mx-button" data-budget-action="${action}" ${attrs}>${esc(t(label))}</button>`;
    const table = (head, rows) => `<div class="mx-scroll"><table><thead><tr>${head.map(h => `<th>${esc(t(h))}</th>`).join("")}</tr></thead><tbody>${rows.length ? rows.map(cells => `<tr>${cells.map(cell => `<td>${cell}</td>`).join("")}</tr>`).join("") : `<tr><td colspan="${head.length}"><div class="mx-empty">${esc(t("No records"))}</div></td></tr>`}</tbody></table></div>`;
    const cards = values => `<div class="mx-cards">${values.map(([label,value]) => `<div class="mx-card"><span>${esc(t(label))}</span><div class="mx-card-value">${value}</div></div>`).join("")}</div>`;
    function normalize(filters, shared) {
        return {...filters, from_date:shared.monthRange(filters.from_date.slice(0,7)).from_date,
            to_date:shared.monthRange(filters.to_date.slice(0,7)).to_date, basis:"period", currency:"CNY", status:"approved"};
    }
    function details(data, wb) {
        const quota = wb.filters.budget_tab === "quota";
        const tabs = `<div class="mx-analysis-group">${btn("Budget Ledger", "ledger", `aria-pressed="${!quota}"`)}${wb.budgetOptions.can_read_quota ? btn("Budget Quotas", "quota", `aria-pressed="${quota}"`) : ""}</div>`;
        let content;
        if (quota) {
            const totals = data.summary.totals;
            content = cards([["Effective Budget Quotas",money(totals.total_amount)], ["Effective Quotas",totals.quota_count], ["Departments",totals.department_count], ["Amount Differences",totals.detail_amount_mismatch_count]]);
            const rollup = wb.budgetRollup || "company";
            const rollups = [["company","Subsidiary"],["group","Group"],["department","Department"],["subject","Subject"]];
            content += wb.panel("Quota Distribution", `<div class="mx-analysis-group">${rollups.map(([key,label]) => btn(label,"rollup",`data-rollup="${key}" aria-pressed="${key === rollup}"`)).join("")}</div>` + table([rollups.find(([key])=>key === rollup)[1],"Effective Quotas","Amount"], data.summary.rollups[rollup].map(row=>[esc(row.name),row.quota_count,money(row.total_amount)])));
            content += wb.panel("Budget Quotas", table(["Approval Number", "Budget Month", "Subsidiary", "Department", "Budget Type", "Amount", "Budget Subjects"], data.rows.map(row => [esc(row.form_no), esc(row.budget_month), esc(row.company_name), esc(row.department_name), esc(kind(row.budget_type)), money(row.total_amount), `<details><summary>${esc(t("Details"))}</summary>${table(["Subject", "Amount"], row.subjects.map(s => [esc(s.name),money(s.amount)]))}</details>`])));
        } else {
            content = wb.panel(wb.view === "review" ? "Budget Data Review" : "Budget Ledger", table(["Approval Number", "Budget Month", "Subsidiary", "Department", "Budget Type", "Budget Amount", "Review Issues"], data.rows.map(row => [btn(row.form_no, "open", `data-name="${esc(row.form_no)}"`), esc(String(row.budget_month || "").slice(0,7)), esc(row.company), esc(row.department), esc(kind(row.budget_kind)), money(row.total_amount ?? row.amount), row.issues.map(x => esc(t(x))).join(" · ") + (wb.budgetOptions.can_review_organization && row.department_id ? btn("Review Organization", "organization", `data-name="${esc(row.name)}"`) : "")])));
        }
        return (wb.view === "review" ? "" : tabs) + content + `<div class="mx-panel-head"><span>${esc(t("Records"))}: ${Number(data.total_count || 0)}</span><div>${btn("Previous", "previous", wb.start === 0 ? "disabled" : "")}${btn("Next", "next", wb.start + 20 >= data.total_count ? "disabled" : "")}</div></div>`;
    }
    function overview(data, wb) {
        const groups = data.groups;
        return cards([["Budget Amount",money(data.amount)], ["Budget Applications",data.total_count], ["Subsidiaries Included",data.company_count], ["Budget Records Requiring Review",Number(data.review_count || 0)]]) +
            `<div class="mx-insight-columns">${wb.panel("Monthly Budget",table(["Budget Month", "Applications", "Budget Amount"], (groups.budget_month || []).map(r => [esc(String(r.label || "").slice(0,7)),r.count,money(r.amount)])))}${wb.panel("Budget Composition",table(["Budget Type", "Applications", "Budget Amount"], (groups.budget_kind || []).map(r => [esc(kind(r.label)),r.count,money(r.amount)])))}</div>` +
            wb.panel("Subsidiary Budget Distribution",table(["Subsidiary", "Applications", "Budget Amount"], (groups.company || []).map(r => [esc(r.label || t("Pending Review")),r.count,money(r.amount)])));
    }
    async function render(wb, generation) {
        const current = () => wb.generation === generation && wb.isCurrentRoute();
        wb.loading = true;
        wb.disposeFilterControls();
        wb.exportButton?.prop("disabled", true);
        wb.el.innerHTML = `<div class="mx-empty">${esc(t("Loading..."))}</div>`;
        try {
            wb.budgetOptions = await call("budget.api.cn_workbench.get_options");
            if (!current()) return;
            wb.budgetCompanies = wb.budgetOptions.companies;
            const f = wb.filters = normalize(wb.filters, root.DeepLinkERPMexicoExpenses);
            if (!wb.budgetCompanies.includes(f.company)) f.company = "";
            if (!wb.budgetOptions.can_read_quota || wb.view === "review") f.budget_tab = "ledger";
            if (wb.view !== "details") f.budget_tab = "ledger";
            const report = wb.view === "budget_analysis";
            if (report) { f.budget_type = ""; f.keyword = ""; f.department_id = ""; }
            const title = report ? "Budget Execution Analysis" : wb.view === "details" ? "Budget Details" : wb.view === "review" ? "Budget Data Review" : "Budget Overview";
            wb.page.set_title(t(title));
            wb.el.innerHTML = `<div class="mx-filters">${wb.filter("basis", "Date Basis", [["period",t("Budget Month")]])}
                <div class="mx-filter mx-date-range-filter"><span>${esc(t("Date Range"))}</span><div class="mx-date-range-control"></div></div>
                ${wb.filter("company", "Subsidiary", [["",t("All Subsidiaries")], ...wb.budgetCompanies.map(c=>[c,c])])}
                ${wb.filter("currency", "Reporting Currency", [["CNY","CNY"]])}
                ${report ? "" : wb.filter("budget_type", "Budget Type", [["",t("All Budget Types")],["production",t("Production Budget")],["non_production",t("Non-production Budget")]])}
                <div class="mx-filter-actions"><button type="button" class="mx-button mx-reset" data-cn-action="reset">${esc(t("Reset Filters"))}</button></div></div>
                <div class="mx-scope"><span><i></i>${esc(f.from_date)} ~ ${esc(f.to_date)} · ${esc(t("Budget"))} · ${esc(f.company || t("All Subsidiaries"))} · CNY</span>${help(scope)}</div>
                ${wb.budgetOptions.organization_review_ready === false ? `<div class="mx-notice">${esc(t("Organization review is not enabled yet. Existing budgets remain available."))}</div>` : ""}
                ${f.department_id || f.keyword ? `<div class="mx-notice">${esc(t("Department filter"))}: ${esc(f.department_id || f.keyword)} ${btn("Clear", "clear-department")}</div>` : ""}<div class="mx-body"><div class="mx-empty">${esc(t("Loading..."))}</div></div>`;
            wb.mountDateControls(); wb.mountSelectControls(); wb.mountSidebar(); wb.remember();
            root.DeepLinkERPMexicoExpenseInsights?.mount(wb.el);
            root.frappe.breadcrumbs.add({type:"Custom", label:t(title)});
            if (report) {
                await root.frappe.require("/assets/budget/js/budget_report.js");
                if (!current()) return;
                const body = wb.el.querySelector(".mx-body"); body.innerHTML = "";
                wb.budgetReport = new root.BudgetReport(null, {parent:body, filters:() => ({start_date:f.from_date,end_date:f.to_date,company:f.company}), openDetails:row => {
                    root.frappe.set_route("china-expense-details", {cn_filters:JSON.stringify({...f,budget_tab:"ledger",department_id:row.deptId || "",keyword:row.deptId ? "" : row.deptName})});
                }});
                await wb.budgetReport.load();
            } else {
                const data = await call("budget.api.cn_workbench.get_data", {filters:JSON.stringify(f), start:wb.start, view:wb.view, tab:f.budget_tab});
                if (!current()) return;
                if (wb.start && wb.start >= data.total_count) { wb.start = Math.max(0,Math.floor((data.total_count-1)/20)*20); return wb.refresh(false); }
                wb.data = data;
                wb.el.querySelector(".mx-body").innerHTML = ["details","review"].includes(wb.view) ? details(data, wb) : overview(data, wb);
                wb.exportButton?.prop("disabled", false);
            }
        } catch (error) {
            if (current()) wb.el.innerHTML = `<div class="mx-empty">${esc(t("Budget data could not be loaded"))}<button type="button" class="mx-button" data-cn-action="retry">${esc(t("Retry"))}</button></div>`;
        } finally { if (current()) wb.loading = false; }
    }
    function action(wb, control) {
        const action = control.dataset.budgetAction;
        if (action === "rollup" && wb.data && ["company","group","department","subject"].includes(control.dataset.rollup)) {
            wb.budgetRollup = control.dataset.rollup;
            wb.el.querySelector(".mx-body").innerHTML = details(wb.data, wb);
            return;
        }
        if (action === "open") return root.frappe.require("/assets/budget/js/budget_detail.js", () => root.BudgetLedgerDetail.open(control.dataset.name));
        if (action === "organization") return root.BudgetLedgerSync.openOrganization(control.dataset.name, () => wb.refresh());
        if (["ledger","quota"].includes(action)) wb.filters.budget_tab = action;
        else if (["previous","next"].includes(action)) { wb.start = Math.max(0,wb.start + (action === "next" ? 20 : -20)); return wb.refresh(false); }
        else if (action === "clear-department") { wb.filters.department_id = ""; wb.filters.keyword = ""; }
        return wb.refresh();
    }
    function sync(wb, settings) {
        return root.BudgetLedgerSync[settings ? "openSettings" : "openSync"](() => wb.refresh());
    }
    async function exportCurrent(wb) {
        const response = await call("budget.api.cn_workbench.export_data", {filters:JSON.stringify(wb.filters),view:wb.view,tab:wb.filters.budget_tab});
        const link = root.document.createElement("a");
        link.href = "data:application/vnd.openxmlformats-officedocument.spreadsheetml.sheet;base64," + response.content;
        link.download = "budget.xlsx"; link.click();
    }
    return {normalize, render, action, sync, exportCurrent};
});
