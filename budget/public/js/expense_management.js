(function (root, factory) {
  const api = factory(root);
  if (typeof module === "object" && module.exports) module.exports = api;
  root.ExpenseManagement = api;
})(globalThis, function (root) {
  "use strict";
  const API = "budget.api.expenses.";
  const GROUP_COMPANY = "__expense_group__";
  const ROUTES = {
    overview: "expense-management",
    analysis: "expense-analysis",
    budget: "expense-budget",
    details: "expense-details",
    review: "expense-review",
  };
  const TITLES = {
    overview: "Expense Overview",
    analysis: "Expenditure Analysis",
    budget: "Budget Execution",
    details: "Expense Details",
    review: "Data Review",
  };
  const STATUS = {
    approved: "Approved",
    pending: "In progress",
    rejected: "Rejected",
    terminated: "Terminated",
    withdrawn: "Withdrawn",
    unknown: "Unknown",
  };
  const t = (value, args) =>
    root.__
      ? root.__(value, args)
      : value.replace(/\{(\d+)\}/g, (_, n) => args?.[+n] ?? "");
  const esc = (value) =>
    String(value ?? "").replace(
      /[&<>"']/g,
      (c) =>
        ({
          "&": "&amp;",
          "<": "&lt;",
          ">": "&gt;",
          '"': "&quot;",
          "'": "&#39;",
        }[c])
    );
  const today = () => root.frappe.datetime.get_today();
  const monthNow = () => today().slice(0, 7);
  const button = (action, label, attrs = "", css = "") =>
    `<button type="button" class="btn btn-default btn-sm ${css}" data-action="${action}" ${attrs}>${esc(
      t(label)
    )}</button>`;
  const textMoney = (value) =>
    value == null ? "—" : root.DeepLinkERPMexicoExpenseMoney.format(value);
  const companyLabel = row => row.is_group_expense || row.company === GROUP_COMPANY
    ? t("Group") : row.company || t("Company not matched");
  function money(amounts, currency) {
    if (!["CNY", "MXN"].includes(currency))
      return `<span class="em-muted">${esc(t("Region not configured"))}</span>`;
    return `<span class="em-money"><strong><span class="em-currency">${currency}</span> ${textMoney(amounts?.[currency])}</strong></span>`;
  }
  function comparisonNotes(comparison) {
    const labels = {current: "Current period: {0} lines need review; {1} unverified lines excluded.",
      previous: "Previous period: {0} lines need review; {1} unverified lines excluded."};
    const notes = Object.entries(labels).flatMap(([period, label]) => {
      const counts = comparison?.periods?.[period];
      return counts && (counts.review || counts.excluded) ? [t(label, [counts.review, counts.excluded])] : [];
    });
    if (!notes.length && comparison?.warnings?.some(code => ["review", "excluded"].includes(code)))
      notes.push(t("Some expenses in the comparison periods still need review."));
    return notes.map(note => `<span class="em-warning">${esc(note)}</span>`).join("");
  }
  function sourceLabels(row) {
    const path = [...(row.source_path || [])];
    const groups = {hr: "Personnel budget", office: "Office premises budget", operation: "Operating budget"};
    if (row.kind === "budget" && groups[path[0]]) path[0] = t(groups[path[0]]);
    return path;
  }
  function originalExpense(original, row) {
    if (!original) return `<p class="em-muted em-source-unavailable">${esc(t("Original expense details are unavailable or outside your permitted scope."))}</p>`;
    const amount = (currency, value) => `${esc(currency || "—")} ${textMoney(value)}`;
    const field = (label, value) => `<dt>${esc(t(label))}</dt><dd>${value}</dd>`;
    const split = original.split_count > 1;
    return `<section class="em-original-expense"><h4>${esc(t("Original expense line"))}</h4>
      <p class="em-muted">${esc(split ? t("Allocated into {0} detail lines", [original.split_count])
        : t(original.origin === "header" ? "From approval header" : "From approval detail"))}</p><dl>
      ${field("Original expense amount", amount(original.currency, original.amount))}
      ${original.unverified ? field("Amount verification", esc(t(row.amount_reviewed
        ? "Approval amount confirmed for reporting; original detail differences are retained."
        : "Source amount recognized; excluded from verified expenditure until review is resolved."))) : ""}
      ${split ? field("Current detail amount", amount(row.currency, row.amount)) : ""}
      ${field("Original expense description", esc(original.description || "—"))}
      ${original.source_entity ? field("Source entity", esc(original.source_entity)) : ""}
      ${original.execution_region ? field("Execution Region", esc(original.execution_region)) : ""}
      ${original.table_label ? field("Source table", esc(original.table_label)) : ""}
      ${original.row_number ? field("Source row", esc(original.row_number)) : ""}</dl></section>`;
  }
  function amountReconciliation(data) {
    if (!data) return "";
    const value = amount => `${esc(data.currency || "—")} ${textMoney(amount)}`;
    const confirmed = data.review?.state === "confirmed";
    const reviewedAt = data.review?.reviewed_at;
    const tables = (data.tables || []).map(table => `<section class="em-amount-table">
      <h5>${esc(table.label || t("Approval details"))}</h5>
      <dl><dt>${esc(t("Detail total"))}</dt><dd>${value(table.total)}</dd>
      <dt>${esc(t("Calculated detail total"))}</dt><dd>${value(table.calculated_total)}</dd>
      <dt>${esc(t("Detail total minus approval amount"))}</dt><dd class="em-warning">${value(table.difference)}</dd></dl>
      <div class="em-amount-rows"><table class="table"><thead><tr>${[
        "Source row", "Quantity", "Unit price", "Detail amount", "Quantity × unit price", "Row difference"
      ].map(label => `<th>${esc(t(label))}</th>`).join("")}</tr></thead><tbody>${(table.rows || []).map(row =>
        `<tr class="${row.difference != null && Number(row.difference) !== 0 ? "em-amount-mismatch" : ""}">
        <td>${esc(row.row_number)}</td><td>${esc(row.quantity ?? "—")}</td><td>${esc(row.unit_price ?? "—")}</td>
        <td>${textMoney(row.amount)}</td><td>${textMoney(row.calculated_amount)}</td><td>${textMoney(row.difference)}</td></tr>`
      ).join("")}</tbody></table></div></section>`).join("");
    return `<section class="em-amount-review"><h4>${esc(t("Amount reconciliation"))}</h4>
      <dl><dt>${esc(t("Approval amount"))}</dt><dd><strong>${value(data.header_amount)}</strong></dd></dl>
      <p class="em-muted">${esc(t("Row difference is the detail amount minus quantity × unit price. All amounts below are in the original currency."))}</p>
      ${tables}${data.review ? `<div class="em-amount-decision"><strong>${esc(t(confirmed
        ? "Approval amount confirmed" : "Source changed; the previous amount confirmation is no longer valid"))}</strong>
        <dl><dt>${esc(t("Adopted approval amount"))}</dt><dd>${esc(data.review.currency || data.currency)} ${textMoney(data.review.amount)}</dd>
        <dt>${esc(t("Reviewed By"))}</dt><dd>${esc(data.review.reviewed_by)}</dd>
        <dt>${esc(t("Reviewed At"))}</dt><dd>${esc(reviewedAt && root.frappe.datetime?.str_to_user
          ? root.frappe.datetime.str_to_user(reviewedAt) : reviewedAt)}</dd>
        <dt>${esc(t("Review Note"))}</dt><dd class="em-review-note">${esc(data.review.note)}</dd></dl></div>` : ""}
      ${data.can_confirm ? `<label class="em-amount-note"><span>${esc(t("Reason for adopting the approval amount"))} *</span>
        <textarea class="form-control" data-amount-note rows="3" maxlength="1000" required></textarea></label>
        <p class="em-muted">${esc(t("Confirmation uses the approval amount for expense reports and preserves the original approval and detail amounts."))}</p>
        <button type="button" class="btn btn-primary btn-sm" data-confirm-amount>${esc(t("Confirm using approval amount"))}</button>`
        : !confirmed && data.unavailable_reason ? `<p class="em-muted">${esc(t(data.unavailable_reason))}</p>` : ""}
      <p class="text-danger" data-amount-error role="alert"></p></section>`;
  }
  function readFilters(search) {
    let incoming;
    try {
      incoming = new URLSearchParams(search).get("expense_filters");
      // Native Desk anchor clicks serialize query values once more than bookmarks.
      for (let depth = 0; depth < 2 && typeof incoming === "string"; depth++)
        incoming = JSON.parse(incoming);
    } catch {
      /* Use defaults for malformed bookmarks. */
    }
    const result = {
      month: monthNow(),
      company: "",
      region: "",
      category: "",
      source_category: "",
      measure: "",
    };
    if (incoming && typeof incoming === "object" && !Array.isArray(incoming)) {
      for (const key of Object.keys(result))
        if (key !== "category" && typeof incoming[key] === "string") result[key] = incoming[key];
    }
    if (
      !/^\d{4}-(0[1-9]|1[0-2])$/.test(result.month) ||
      result.month < "1900-01" ||
      result.month > monthNow()
    )
      result.month = monthNow();
    if (!["", "cn", "mx", "group"].includes(result.region)) result.region = "";
    return result;
  }
  function shiftMonth(month, delta) {
    const [year, value] = month.split("-").map(Number),
      number = year * 12 + value - 1 + delta;
    return `${Math.floor(
      number / 12
    )}-${String((number % 12) + 1).padStart(2, "0")}`;
  }
  function errorMessage(error, fallback = "Expense data could not be loaded. Retry or check the source workbenches.") {
    try {
      const response = error?.responseJSON;
      if (["ValidationError", "PermissionError"].includes(response?.exc_type)) {
        const last = JSON.parse(response._server_messages).at(-1),
          value = typeof last === "string" ? JSON.parse(last) : last;
        if (typeof value?.message === "string" && value.message.length < 1500)
          return value.message;
      }
    } catch {
      /* Do not expose tracebacks or raw server errors. */
    }
    return t(fallback);
  }
  function table(headers, rows, empty = "No records in this month", count = null) {
    return `<div class="em-table-scroll"><table class="em-table"><thead><tr>${headers
      .map((h, index) => `<th scope="col">${esc(t(h))}${index === 0 && count != null ? `<span class="em-count">${esc(count)}</span>` : ""}</th>`)
      .join(
        ""
      )}</tr></thead><tbody>${rows.length ? rows.map((cells) => `<tr>${cells.map((cell) => `<td>${cell}</td>`).join("")}</tr>`).join("") : `<tr><td colspan="${headers.length}" class="em-empty">${esc(t(empty))}</td></tr>`}</tbody></table></div>`;
  }
  function installNavigation() {
    if (root.frappe.expense_navigation_installed) return;
    root.frappe.expense_navigation_installed = true;
    root.document.addEventListener(
      "click",
      (event) => {
        if (
          event.defaultPrevented ||
          event.button !== 0 ||
          event.ctrlKey ||
          event.metaKey ||
          event.shiftKey ||
          event.altKey
        )
          return;
        const link = event.target.closest?.(".body-sidebar a[href]");
        const work =
          root.frappe.pages[root.frappe.get_route()?.[0]]?.expense_management;
        if (
          !link ||
          !work?.active ||
          root.frappe.app?.sidebar?.editor?.edit_mode ||
          link.target === "_blank"
        )
          return;
        const url = new URL(link.href, root.location.href),
          route = url.pathname.replace(/^\/desk\//, "");
        if (
          url.origin !== root.location.origin ||
          !Object.values(ROUTES).includes(route) ||
          url.search ||
          url.hash
        )
          return;
        root.frappe.route_options = { expense_filters: { ...work.filters } };
      },
      true
    );
    const breadcrumbs = root.frappe.breadcrumbs;
    if (breadcrumbs) {
      const original = breadcrumbs.set_custom_breadcrumbs;
      breadcrumbs.set_custom_breadcrumbs = function (options) {
        const view = Object.keys(ROUTES).find(
          (key) => ROUTES[key] === root.frappe.get_route()?.[0]
        );
        if (!view) return original.call(this, options);
        const filters = readFilters(root.location.search);
        const home = { month: filters.month, company: "", category: "" };
        this.append_breadcrumb_element(
          "/desk/expense-management?expense_filters=" +
            encodeURIComponent(JSON.stringify(home)),
          esc(t("Expense Management"))
        );
        this.append_breadcrumb_element(null, esc(t(TITLES[view])));
      };
    }
  }
  class Workbench {
    constructor(wrapper, route) {
      this.wrapper = wrapper;
      this.route = route;
      this.view = Object.keys(ROUTES).find((key) => ROUTES[key] === route);
      this.seq = 0;
      this.detailSeq = 0;
      this.optionsSeq = 0;
      this.start = 0;
      this.options = { companies: [], categories: {}, issues: {} };
      this.active = false;
      this.filters = readFilters(root.location.search);
      this.page = root.frappe.ui.make_app_page({
        parent: wrapper,
        title: t(TITLES[this.view]),
        single_column: true,
      });
      this.host = this.page.main[0];
      this.host.classList.add("em-host");
      root.frappe.pages[route].expense_management = this;
      this.page.set_secondary_action(t("Refresh"), () => this.load());
      this.page.add_inner_button(t("Export Excel"), () => this.export());
      this.sync = new root.ExpenseSync.Controller(this);
      this.syncButton = this.page.add_inner_button(t("Synchronize Now"), () =>
        this.sync.synchronize()
      );
      this.page.add_menu_item(t("Reporting policy"), () => this.policy());
      this.page.add_menu_item(t("Synchronization Settings"), () =>
        this.sync.open()
      );
      root.$(wrapper).on("hide.expense-management", () => this.hide());
      this.host.addEventListener("click", (e) => this.click(e));
      this.host.addEventListener("change", (e) => {
        if (!e.target.dataset.filter) return;
        this.update({ [e.target.dataset.filter]: e.target.value });
      });
      installNavigation();
      this.show();
    }
    current() {
      return this.active && root.frappe.get_route()?.[0] === this.route;
    }
    async show() {
      if (root.frappe.get_route()?.[0] !== this.route) return;
      this.active = true;
      this.start = 0;
      this.filters = readFilters(root.location.search);
      if (this.view !== "details") this.filters.measure = "";
      root.frappe.breadcrumbs?.add({
        type: "Custom",
        label: t(TITLES[this.view]),
      });
      this.shell();
      this.sync?.start();
      const optionsToken = ++this.optionsSeq;
      this.call("get_options")
        .then((options) => {
          if (!this.current() || optionsToken !== this.optionsSeq) return;
          this.options = { ...this.options, ...options };
          this.controls();
        })
        .catch(() => {
          /* get_data renders the authoritative permission/error result. */
        });
      await this.load();
    }
    hide() {
      this.active = false;
      this.seq++;
      this.loading = false;
      this.detailSeq++;
      this.optionsSeq++;
      this.sync?.stop();
      this.picker?.destroy();
      this.dialog?.hide();
    }
    call(method, args = {}) {
      return root.frappe
        .call({ method: API + method, args })
        .then((result) => result.message);
    }
    requestFilters() {
      return { ...this.filters, category: "", view: this.view };
    }
    syncURL() {
      if (!this.current()) return;
      this.filters.category = "";
      const url = new URL(root.location.href);
      url.searchParams.set("expense_filters", JSON.stringify(this.filters));
      root.history.replaceState(
        root.history.state,
        "",
        url.pathname + url.search + url.hash
      );
    }
    update(values) {
      if (Object.hasOwn(values, "company") && !Object.hasOwn(values, "region")) values = {...values, region: ""};
      Object.assign(this.filters, values, { category: "" });
      this.start = 0;
      this.syncURL();
      this.controls();
      this.load();
    }
    go(view, values = {}) {
      if (Object.hasOwn(values, "company") && !Object.hasOwn(values, "region")) values = {...values, region: ""};
      return root.frappe.set_route(ROUTES[view], {
        expense_filters: { ...this.filters, measure: "", ...values, category: "" },
      });
    }
    shell() {
      this.picker?.destroy();
      this.host.innerHTML =
        '<div class="em-controls"></div><div class="em-sync-status" aria-live="polite"></div><div class="em-content" aria-live="polite"></div>';
      this.controls();
    }
    controls() {
      if (!this.current()) return;
      this.picker?.destroy();
      const f = this.filters;
      const select = (key, label, entries, empty) =>
        `<label class="em-filter"><span>${esc(
          t(label)
        )}</span><select class="form-control" data-filter="${key}"><option value="">${esc(
          t(empty)
        )}</option>${entries
          .map(
            ([value, title]) =>
              `<option value="${esc(value)}" ${
                f[key] === value ? "selected" : ""
              }>${esc(title)}</option>`
          )
          .join("")}</select></label>`;
      const companies = [...(this.options.companies || [])];
      const sourceCategories = (this.options.source_categories || []).map((row) => [
        row.key, sourceLabels(row).join(" / ") || t("Category not provided"),
      ]);
      if (f.source_category && !sourceCategories.some(([key]) => key === f.source_category))
        sourceCategories.push([f.source_category, t("Selected OA category")]);
      // Preserve a failed/forbidden selection until the user explicitly clears it.
      if (f.company && !companies.includes(f.company))
        companies.push(f.company);
      this.host.querySelector(
        ".em-controls"
      ).innerHTML = `<div class="em-heading"><h2>${esc(
        f.company ? companyLabel({company:f.company}) : t("Subsidiary expense overview")
      )}</h2><div class="em-heading-actions">${
        this.view === "details" && f.source_category ? button("analysis", "Back to expense analysis", "", "em-back-link") : ""
      }${
        f.company || f.region || f.source_category ? button("all", "All Subsidiaries") : ""
      }${button(
        "policy",
        "Reporting policy"
      )}</div></div><div class="em-filters"><div class="em-filter em-month"><span>${esc(
        t(
          this.view === "budget"
            ? "Budget month"
            : this.view === "review"
            ? "Reporting month"
            : "Application month"
        )
      )}</span><div class="em-month-actions">${button(
        "previous",
        "Previous month",
        f.month <= "1900-01" ? "disabled" : ""
      )}<div class="em-month-picker"></div>${button(
        "next",
        "Next month",
        f.month >= monthNow() ? "disabled" : ""
      )}${
        f.month !== monthNow() ? button("current", "This month") : ""
      }</div></div>${select(
        "company",
        "Subsidiary",
        companies.map((name) => [name, name === GROUP_COMPANY ? t("Group") : name]),
        "All Subsidiaries"
      )}${select(
        "source_category",
        "Expense category",
        sourceCategories,
        "All categories"
      )}</div>`;
      const range = root.DeepLinkERPMexicoExpenseDateRange.wholeMonths(
        f.month,
        f.month
      );
      this.picker = new root.DeepLinkERPMexicoExpenseDateRange.MonthPicker({
        parent: this.host.querySelector(".em-month-picker"),
        value: range,
        today: today(),
        getToday: today,
        onChange: (value) =>
          this.update({ month: value.from_date.slice(0, 7) }),
      });
    }
    async load(preserveControls = false) {
      if (!this.current()) return false;
      const token = ++this.seq,
        filters = this.requestFilters();
      this.loading = true;
      this.detailSeq++;
      this.dialog?.hide();
      this.data = null;
      this.syncURL();
      this.content(
        `<div class="em-empty" role="status">${esc(
          t("Loading expense data…")
        )}</div>`
      );
      try {
        const result = await this.call("get_data", {
          filters,
          start: this.start,
        });
        if (!this.current() || token !== this.seq) return false;
        this.data = result;
        this.options = {
          ...this.options,
          companies: result.companies,
          categories: result.categories,
          source_categories: result.source_categories,
          issues: result.issues,
        };
        if (!preserveControls) this.controls();
        this.render();
        return true;
      } catch (error) {
        if (!this.current() || token !== this.seq) return false;
        this.data = null;
        this.content(
          `<div class="em-error" role="alert"><p>${esc(
            errorMessage(error)
          )}</p>${button("retry", "Retry")}</div>`
        );
        return false;
      } finally {
        if (token === this.seq) this.loading = false;
      }
    }
    content(html) {
      const node = this.host.querySelector(".em-content");
      if (node) node.innerHTML = html;
    }
    render() {
      const d = this.data,
        s = d.summary;
      let html = this.filters.measure === "approved"
        ? `<div class="em-status"><span class="em-chip">${esc(t("Verified approvals only"))}</span>${button("all-statuses", "All statuses")}</div>`
        : "";
      if (this.filters.region)
        html += `<div class="em-status"><span class="em-chip">${esc(t({cn:"China",mx:"Mexico",group:"Group"}[this.filters.region]))}</span>${button("clear-region", "All regions")}</div>`;
      const notices = [];
      if (d.metadata.reporting_pending) notices.push(t("Expense reports are being updated. Showing the last completed version."));
      if (!d.metadata.complete) notices.push(t("Source data incomplete"));
      if (s.excluded) notices.push(t("{0} unverified lines excluded", [s.excluded]));
      if (d.undated) notices.push(t("{0} lines without application dates", [d.undated]));
      if (notices.length)
        html += `<aside class="em-data-notice" aria-label="${esc(t("Data Review"))}"><div><strong>${esc(t("Data Review"))}</strong><ul>${notices.map((notice) => `<li>${esc(notice)}</li>`).join("")}</ul></div>${button("review", "View review items")}</aside>`;
      if (!d.metadata.has_data) {
        this.content(
          html +
            `<div class="em-empty">${esc(
              t("No local approval snapshot is available yet.")
            )}</div>`
        );
        return;
      }
      if (["overview", "analysis"].includes(this.view)) {
        const regions = d.regions || [];
        html += `<div class="em-kpis"><article class="em-kpi em-primary"><span>${esc(t("Verified approved expenditure"))}</span>
          ${regions.map((region) => `<div class="em-regional-kpi"><span class="em-region-label">${esc(t(region.label))}</span>${money(region.summary.amounts, region.currency)}
            <button class="em-link" data-action="details" data-region="${esc(region.region)}">${esc(t("{0} applications · {1} lines", [region.summary.applications, region.summary.count]))}</button></div>`).join("") || `<span>${esc(t("Region not configured"))}</span>`}
          </article><article class="em-kpi"><span>${esc(t("Verified expenditure compared with last month"))}</span>
          ${regions.map((region) => {
            const labels = {no_current_verified: "No verified expenditure this month", no_previous_verified: "No verified expenditure last month",
              amount_unavailable: "Comparison exchange rate unavailable", previous_zero: "Last month's verified expenditure was zero"};
            return `<div class="em-regional-kpi"><span class="em-region-label">${esc(t(region.label))}</span><strong class="em-number">${region.comparison?.percent == null ? esc(t(labels[region.comparison?.status] || "Not comparable")) : `${Number(region.comparison.percent) > 0 ? "+" : ""}${esc(region.comparison.percent)}%`}</strong>${comparisonNotes(region.comparison)}</div>`;
          }).join("")}
          ${button("policy", "Comparison details", "", "em-subtle")}</article>
          <article class="em-kpi"><span>${esc(t("Data Review"))}</span><button class="em-number em-link" data-action="review">${s.review + d.undated}</button><span class="em-muted">${esc(t("{0} pending approval lines", [s.pending]))}</span></article></div>`;
      }
      if (this.view === "overview") html += this.companies();
      if (this.view === "analysis") html += this.analysis();
      if (this.view === "budget") html += this.budget();
      if (["details", "review"].includes(this.view)) html += this.details();
      this.content(html);
    }
    companies() {
      const d = this.data;
      const rows = d.company_rows.map((row) => {
        const share = row.regional_share == null ? null : Number(row.regional_share);
        return [
          `<button class="em-link em-company" data-action="company" data-company="${esc(
            row.company
          )}">${esc(companyLabel(row))} <span aria-hidden="true">›</span></button>`,
          money(row.amounts, row.display_currency),
          share == null
            ? "—"
            : `<div class="em-share"><span>${share.toFixed(
                1
              )}%</span><i style="--share:${share}%"></i></div>`,
          row.applications,
          `<button class="em-link ${
            row.review ? "em-warning" : ""
          }" data-action="company-review" data-company="${esc(row.company)}">${
            row.review
          }</button>`,
        ];
      });
      return `<section class="em-panel em-companies" aria-label="${esc(t("Subsidiaries"))}">${table(["Subsidiary", "Verified approved expenditure", "Share within region", "Applications", "Needs review"], rows, "No records in this month", d.company_rows.filter(row=>row.company!==GROUP_COMPANY).length)}${d.unassigned ? `<div class="em-panel-footer">${button("review", "Review unassigned expenses")} <span class="em-warning">${d.unassigned}</span></div>` : ""}</section>`;
    }
    analysis() {
      return (this.data.regions || []).map((region) => {
        const rows = (region.source_category_rows || []).map((row) => {
          const attrs = `type="button" class="em-link em-category-link" data-action="source-category" data-category="${esc(row.category)}" data-region="${esc(region.region)}" title="${esc(t("View expense details"))}"`;
          return [
            `<button ${attrs}>${esc(sourceLabels(row).join(" / ") || t("Category not provided"))}</button>`,
            money(row.amounts, region.currency),
            `<span class="em-category-count">${esc(row.count)}<svg class="em-category-arrow" aria-hidden="true" width="14" height="14" viewBox="0 0 16 16"><path d="m6 4 4 4-4 4" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg></span>`,
          ];
        });
        const trend = region.trend || [];
        const max = Math.max(...trend.map((row) => Number(row.amounts[region.currency]) || 0), 1);
        return `<section class="em-regional-analysis"><h3 class="em-region-label">${esc(t(region.label))} · ${esc(region.currency)}</h3><div class="em-analysis"><section class="em-panel em-composition"><div class="em-panel-title"><h3>${esc(t("Expense composition"))}</h3></div>
          ${table(["Expense category", "Verified approved expenditure", "Detail lines"], rows)}</section>
          <section class="em-panel"><div class="em-panel-title"><h3>${esc(t("Last six months"))}</h3></div><div class="em-trend">${trend.map((row) => `<button class="em-trend-row" data-action="trend" data-month="${esc(row.month)}" data-region="${esc(region.region)}"><span>${esc(row.month)}</span><i style="--share:${(Math.max(0, Number(row.amounts[region.currency]) || 0) / max) * 100}%"></i>${money(row.amounts, region.currency)}</button>`).join("")}</div></section></div></section>`;
      }).join("");
    }
    budget() {
      const rows = this.data.budgets.map((row) => {
        const currency = row.display_currency;
        const c = row.metrics[currency] || {budget: null, approved: null, balance: null, execution_rate: null, flags: []};
        const amount = (key) => money({[currency]: c[key]}, currency);
        return [
          `<button class="em-link" data-action="budget-company" data-company="${esc(
            row.company
          )}">${esc(companyLabel(row))}</button>`,
          c.budget == null
            ? `<span class="em-muted">${esc(
                t(
                  c.flags.includes("incomplete")
                    ? "Needs review"
                    : "No verified budget"
                )
              )}</span>`
            : amount("budget"),
          amount("approved"),
          amount("balance"),
          c.execution_rate == null ? "—" : `${esc(c.execution_rate)}%`,
        ];
      });
      return `<section class="em-panel"><div class="em-panel-title"><h3>${esc(
        t("Company budget execution")
      )}</h3>${button("policy", "Budget policy")}</div>${this.filters.source_category ? `<p class="em-category-note">${esc(t("Budget differences and execution rates are unavailable when filtering by expense category."))}</p>` : ""}${table(["Subsidiary", "Effective budget", "Approved budget occupation", "Budget difference", "Budget execution rate"], rows)}</section>`;
    }
    details() {
      const d = this.data,
        issues = d.issues || {};
      const rows = d.rows.map((row) => [
        `<button class="em-link" data-action="detail" data-key="${esc(
          row.key
        )}">${esc(row.number)}</button>`,
        esc(companyLabel(row)),
        esc(row.application_date || "—"),
        esc(t(STATUS[row.status] || "Unknown")),
        esc(sourceLabels(row).join(" / ") || t("Category not provided")),
        money(row.amounts, row.display_currency),
        row.issues.length
          ? `<button class="em-link em-warning" data-action="detail" data-key="${esc(
              row.key
            )}" title="${esc(
              row.issues
                .map((issue) => t(issues[issue] || "Needs review"))
                .join("; ")
            )}">${esc(t("Needs review"))}</button>`
          : "—",
      ]);
      return `<section class="em-panel"><div class="em-panel-title"><h3>${esc(
        t(TITLES[this.view])
      )}<span class="em-count">${d.total}</span></h3></div>${table(["Approval number", "Subsidiary", "Application date", "Approval status", "Expense category", "Amount", "Review issues"], rows)}<div class="em-pagination">${button("page-prev", "Previous page", d.start <= 0 ? "disabled" : "")}<span>${esc(t("{0}–{1} of {2}", [d.total ? d.start + 1 : 0, Math.min(d.start + 50, d.total), d.total]))}</span>${button("page-next", "Next page", !d.has_more ? "disabled" : "")}</div></section>`;
    }
    async detail(key) {
      const token = ++this.detailSeq,
        filters = this.requestFilters();
      this.dialog?.hide();
      const dialog = (this.dialog = new root.frappe.ui.Dialog({
        title: t("Expense detail"),
        fields: [{ fieldtype: "HTML", fieldname: "body" }],
      }));
      const body = dialog.fields_dict.body.$wrapper[0];
      body.textContent = t("Loading expense data…");
      dialog.show();
      try {
        const { row, approval_url, original_expense, amount_reconciliation } = await this.call("get_detail", { key, filters });
        if (!this.current() || token !== this.detailSeq) return;
        const path = sourceLabels(row);
        const headline = original_expense?.unverified
          ? `<span class="em-money"><strong><span class="em-currency">${esc(original_expense.currency)}</span> ${textMoney(original_expense.amount)}</strong></span><p class="em-warning">${esc(t("Original expense amount"))} · ${esc(t("Needs review"))}</p>`
          : money(row.amounts, row.display_currency);
        body.innerHTML = `<div class="em-detail">${headline}<dl><dt>${esc(t("Subsidiary"))}</dt><dd>${esc(
          companyLabel(row)
        )}</dd><dt>${esc(t("Approval number"))}</dt><dd>${esc(
          row.number
        )}</dd><dt>${esc(t("Reporting region"))}</dt><dd>${esc(t(
          ({cn: "China", mx: "Mexico", group: "Group"})[row.region] || "Region not configured"
        ))}</dd><dt>${esc(t("Application date"))}</dt><dd>${esc(
          row.application_date || "—"
        )}</dd><dt>${esc(t("Date source"))}</dt><dd>${esc(
          t(
            row.date_basis === "submission"
              ? "Approval creation date"
              : row.date_basis === "budget"
              ? "Budget month"
              : "Application date"
          )
        )}</dd><dt>${esc(t("Detail amount in original currency"))}</dt><dd>${esc(
          row.currency
        )} ${textMoney(row.amount)}</dd><dt>${esc(
          t("Exchange rate date")
        )}</dt><dd>${esc(row.rate_date || "—")}</dd><dt>${esc(
          t("Exchange rate")
        )}</dt><dd>${esc(row.display_currency || "—")} ${esc(row.rates[row.display_currency] || "—")}</dd><dt>${esc(
          t("Detail item")
        )}</dt><dd>${esc(row.line_label || row.description || "—")}</dd><dt>${esc(t("Expense category"))}</dt><dd>${esc(path[0] || t("Category not provided"))}</dd><dt>${esc(t("OA expense subcategory"))}</dt><dd>${esc(path.slice(1).join(" / ") || "—")}</dd></dl>${
          row.issues.length
            ? `<ul>${row.issues
                .map(
                  (issue) =>
                    `<li>${esc(
                      t(this.options.issues?.[issue] || "Needs review")
                    )}</li>`
                )
                .join("")}</ul>`
            : ""
        }${originalExpense(original_expense, row)}${amountReconciliation(amount_reconciliation)}<div class="em-detail-actions">${typeof approval_url === "string" && /^dingtalk:\/\/dingtalkclient\/page\/link\?url=/.test(approval_url)
          ? `<a class="btn btn-primary btn-sm" href="${esc(approval_url)}" rel="noopener noreferrer">${esc(t("Open Original in DingTalk"))}</a>`
          : `<span class="text-muted">${esc(t("Original approval link is unavailable"))}</span>`}</div></div>`;
        body.querySelector?.("[data-confirm-amount]")?.addEventListener("click", () =>
          this.confirmAmount(key, amount_reconciliation, dialog, body));
        body.querySelector?.("[data-amount-note]")?.addEventListener("input", () => {
          body.querySelector("[data-amount-error]").textContent = "";
        });
      } catch (error) {
        if (this.current() && token === this.detailSeq)
          body.textContent = errorMessage(error);
      }
    }
    async confirmAmount(key, data, dialog, body) {
      const submit = body.querySelector("[data-confirm-amount]"),
        input = body.querySelector("[data-amount-note]"),
        error = body.querySelector("[data-amount-error]");
      if (!data?.can_confirm || !submit || submit.disabled || this.dialog !== dialog || !this.current()) return;
      const note = input.value.trim();
      error.textContent = "";
      if (!note) {
        error.textContent = t("A review note is required");
        input.focus();
        return;
      }
      submit.disabled = input.disabled = true;
      try {
        await this.call("confirm_approval_amount", {key, source_key: data.source_key,
          expected_revision: data.revision, expected_fingerprint: data.fingerprint, note});
        if (this.dialog === dialog) dialog.hide();
        root.frappe.show_alert({message: t("Approval amount confirmed. Expense reports are being updated."), indicator: "green"});
        if (this.current()) await this.load();
      } catch (failure) {
        if (this.current() && this.dialog === dialog)
          error.textContent = errorMessage(failure, "Amount confirmation failed. Reload the approval and retry.");
      } finally {
        submit.disabled = input.disabled = false;
      }
    }
    policy() {
      const meta = this.data?.metadata,
        comparison = this.data?.comparison;
      const lines = [
        "Amounts follow the subsidiary country: China in CNY, Mexico in MXN. Regional totals are kept separate.",
        "Expenses without a source name are reported under Group in CNY. Group is a reporting scope, not a Company record; original currencies are retained.",
        "Shares and comparisons are calculated within each region. Companies without a supported country are not included in regional totals.",
        "Approved expenses by application month; approval does not confirm payment.",
        "Expenses without an application date or with a blank date use the approval creation date. Invalid or conflicting dates still require review.",
        "Only verified subsidiary or Group allocations and amounts enter approved expenditure. Invalid or undated expenses remain in Data Review.",
        "Expense categories come directly from the original approvals.",
        "CNY and MXN are converted independently from original amounts using the application month-start rate.",
        "Missing exchange rates are shown as unavailable; no partial currency total is presented.",
        "Current-month comparison uses matching elapsed days and verified approved expenditure in both periods. Other review items do not prevent comparison; source completeness and excluded lines are shown separately.",
        "Budgets keep their budget month. The latest approved budget per subsidiary is used; a missing budget is not zero.",
      ];
      let message = `<ul class="em-policy">${lines
        .map((line) => `<li>${esc(t(line))}</li>`)
        .join("")}</ul>`;
      if (comparison?.window) {
        const w = comparison.window;
        message += `<p>${esc(t("Comparison period"))}: ${esc(
          w.from_date
        )} → ${esc(w.to_date)} / ${esc(w.previous_from)} → ${esc(
          w.previous_to
        )}</p>`;
      }
      if (meta)
        message += `<p>${esc(t("Data updated at"))}: CN ${esc(
          meta.updated_at.cn || "—"
        )} · MX ${esc(meta.updated_at.mx || "—")}</p>`;
      root.frappe.msgprint({
        title: t("Reporting policy"),
        message,
        wide: true,
      });
    }
    async export() {
      if (!this.data || this.exporting) return;
      this.exporting = true;
      try {
        const result = await this.call("export_data", {
          filters: this.requestFilters(),
        });
        const bytes = Uint8Array.from(root.atob(result.content), (char) =>
          char.charCodeAt(0)
        );
        const url = root.URL.createObjectURL(
          new Blob([bytes], {
            type: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
          })
        );
        const anchor = root.document.createElement("a");
        anchor.href = url;
        anchor.download = result.filename;
        anchor.click();
        root.setTimeout(() => root.URL.revokeObjectURL(url), 1000);
      } catch (error) {
        root.frappe.msgprint({
          title: t("Export failed"),
          message: esc(errorMessage(error)),
          indicator: "red",
        });
      } finally {
        this.exporting = false;
      }
    }
    click(event) {
      const item = event.target.closest("[data-action]");
      if (!item || item.disabled) return;
      const action = item.dataset.action;
      if (action === "all")
        return this.go("overview", { company: "", category: "", source_category: "" });
      if (action === "company")
        return this.go("analysis", { company: item.dataset.company });
      if (action === "analysis") return this.go("analysis");
      if (action === "company-review")
        return this.go("review", { company: item.dataset.company });
      if (action === "budget-company")
        return this.go("budget", { company: item.dataset.company });
      if (action === "source-category")
        return this.go("details", {
          source_category: item.dataset.category,
          region: item.dataset.region || this.filters.region || "",
          measure: "approved",
        });
      if (action === "trend")
        return this.go("details", {
          month: item.dataset.month,
          region: item.dataset.region || this.filters.region || "",
          measure: "approved",
        });
      if (action === "details") return this.go(action, { measure: "approved", region: item.dataset.region || this.filters.region || "" });
      if (action === "clear-region") return this.update({ region: "" });
      if (action === "review") return this.go(action);
      if (action === "all-statuses") return this.update({ measure: "" });
      if (action === "detail") return this.detail(item.dataset.key);
      if (action === "policy") return this.policy();
      if (action === "previous" || action === "next")
        return this.update({
          month: shiftMonth(this.filters.month, action === "next" ? 1 : -1),
        });
      if (action === "current") return this.update({ month: monthNow() });
      if (action === "page-prev" || action === "page-next") {
        this.start = Math.max(
          0,
          this.start + (action === "page-next" ? 50 : -50)
        );
        return this.load();
      }
      if (action === "retry") return this.load();
    }
  }
  return { Workbench, money, readFilters, shiftMonth, errorMessage, sourceLabels, amountReconciliation, ROUTES };
});
