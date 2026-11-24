(function (root, factory) {
  const api = factory(root);
  if (typeof module === "object" && module.exports) module.exports = api;
  root.ExpenseManagement = api;
})(globalThis, function (root) {
  "use strict";
  const API = "budget.api.expenses.";
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
  function dual(amounts) {
    return `<span class="em-money"><strong><span class="em-currency">CNY</span> ${textMoney(
      amounts?.CNY
    )}</strong><small><span class="em-currency">MXN</span> ${textMoney(amounts?.MXN)}</small></span>`;
  }
  function readFilters(search) {
    let incoming;
    try {
      incoming = JSON.parse(new URLSearchParams(search).get("expense_filters"));
    } catch {
      /* Use defaults for malformed bookmarks. */
    }
    const result = {
      month: monthNow(),
      company: "",
      category: "",
      measure: "",
    };
    if (incoming && typeof incoming === "object" && !Array.isArray(incoming)) {
      for (const key of Object.keys(result))
        if (typeof incoming[key] === "string") result[key] = incoming[key];
    }
    if (
      !/^\d{4}-(0[1-9]|1[0-2])$/.test(result.month) ||
      result.month < "1900-01" ||
      result.month > monthNow()
    )
      result.month = monthNow();
    return result;
  }
  function shiftMonth(month, delta) {
    const [year, value] = month.split("-").map(Number),
      number = year * 12 + value - 1 + delta;
    return `${Math.floor(
      number / 12
    )}-${String((number % 12) + 1).padStart(2, "0")}`;
  }
  function errorMessage(error) {
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
    return t(
      "Expense data could not be loaded. Retry or check the source workbenches."
    );
  }
  function table(headers, rows, empty = "No records in this month") {
    return `<div class="em-table-scroll"><table class="em-table"><thead><tr>${headers
      .map((h) => `<th scope="col">${esc(t(h))}</th>`)
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
          this.options = options;
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
      return { ...this.filters, view: this.view };
    }
    syncURL() {
      if (!this.current()) return;
      const url = new URL(root.location.href);
      url.searchParams.set("expense_filters", JSON.stringify(this.filters));
      root.history.replaceState(
        root.history.state,
        "",
        url.pathname + url.search + url.hash
      );
    }
    update(values) {
      Object.assign(this.filters, values);
      this.start = 0;
      this.syncURL();
      this.controls();
      this.load();
    }
    go(view, values = {}) {
      return root.frappe.set_route(ROUTES[view], {
        expense_filters: { ...this.filters, measure: "", ...values },
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
      const f = this.filters,
        categories = this.options.categories || {};
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
      // Preserve a failed/forbidden selection until the user explicitly clears it.
      if (f.company && !companies.includes(f.company))
        companies.push(f.company);
      this.host.querySelector(
        ".em-controls"
      ).innerHTML = `<div class="em-heading"><h2>${esc(
        f.company || t("Subsidiary expense overview")
      )}</h2><div class="em-heading-actions">${
        f.company || f.category ? button("all", "All Subsidiaries") : ""
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
        companies.map((name) => [name, name]),
        "All Subsidiaries"
      )}${select(
        "category",
        "Management category",
        [
          ...Object.entries(categories).map(([key, value]) => [key, t(value)]),
          ["unclassified", t("Unclassified")],
        ],
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
      let html = `<div class="em-status"><span class="em-chip">${esc(
        t("CNY primary · MXN secondary")
      )}</span>${
        this.filters.measure === "approved"
          ? `<span class="em-chip">${esc(
              t("Verified approvals only")
            )}</span>${button("all-statuses", "All statuses")}`
          : ""
      }${
        !d.metadata.complete
          ? `<span class="em-chip em-warning">${esc(
              t("Source data incomplete")
            )}</span>`
          : ""
      }${
        s.excluded
          ? `<button class="em-link em-warning" data-action="review">${esc(
              t("{0} unverified lines excluded", [s.excluded])
            )}</button>`
          : ""
      }${
        d.undated
          ? `<button class="em-link em-warning" data-action="review">${esc(
              t("{0} lines without application dates", [d.undated])
            )}</button>`
          : ""
      }</div>`;
      if (!d.metadata.has_data) {
        this.content(
          html +
            `<div class="em-empty">${esc(
              t("No local approval snapshot is available yet.")
            )}</div>`
        );
        return;
      }
      if (["overview", "analysis"].includes(this.view))
        html += `<div class="em-kpis"><article class="em-kpi em-primary"><span>${esc(
          t("Verified approved expenditure")
        )}</span>${dual(
          s.amounts
        )}<button class="em-link" data-action="details">${esc(
          t("{0} applications · {1} lines", [s.applications, s.count])
        )}</button></article><article class="em-kpi"><span>${esc(
          t("Compared with the same period last month")
        )}</span><strong class="em-number">${
          d.comparison?.percent == null
            ? esc(t("Not comparable"))
            : `${Number(d.comparison.percent) > 0 ? "+" : ""}${esc(
                d.comparison.percent
              )}%`
        }</strong>${button(
          "policy",
          "Comparison details",
          "",
          "em-subtle"
        )}</article><article class="em-kpi"><span>${esc(
          t("Data Review")
        )}</span><button class="em-number em-link" data-action="review">${
          s.review + d.undated
        }</button><span class="em-muted">${esc(
          t("{0} pending approval lines", [s.pending])
        )}</span></article></div>`;
      if (this.view === "overview") html += this.companies();
      if (this.view === "analysis") html += this.analysis();
      if (this.view === "budget") html += this.budget();
      if (["details", "review"].includes(this.view)) html += this.details();
      this.content(html);
    }
    companies() {
      const d = this.data,
        total = Number(d.summary.amounts.CNY);
      const rows = d.company_rows.map((row) => {
        const share =
          total > 0 && row.amounts.CNY != null
            ? Math.max(
                0,
                Math.min(100, (Number(row.amounts.CNY) / total) * 100)
              )
            : null;
        return [
          `<button class="em-link em-company" data-action="company" data-company="${esc(
            row.company
          )}">${esc(row.company)} <span aria-hidden="true">›</span></button>`,
          dual(row.amounts),
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
      return `<section class="em-panel"><div class="em-panel-title"><h3>${esc(
        t("Subsidiaries")
      )}<span class="em-count">${d.company_rows.length}</span></h3></div>${table(["Subsidiary", "Verified approved expenditure", "Share", "Applications", "Needs review"], rows)}${d.unassigned ? `<div class="em-panel-footer">${button("review", "Review unassigned expenses")} <span class="em-warning">${d.unassigned}</span></div>` : ""}</section>`;
    }
    analysis() {
      const d = this.data,
        rows = d.category_rows.map((row) => [
          `<button class="em-link" data-action="category" data-category="${esc(
            row.category
          )}">${esc(t(d.categories[row.category] || "Unclassified"))}</button>`,
          dual(row.amounts),
          row.count,
        ]);
      const max = Math.max(
        ...d.trend.map((row) => Number(row.amounts.CNY) || 0),
        1
      );
      return `<div class="em-analysis"><section class="em-panel"><div class="em-panel-title"><h3>${esc(
        t("Expense composition")
      )}</h3></div>${table(["Management category", "Verified approved expenditure", "Detail lines"], rows)}</section><section class="em-panel"><div class="em-panel-title"><h3>${esc(t("Last six months"))}</h3></div><div class="em-trend">${d.trend.map((row) => `<button class="em-trend-row" data-action="trend" data-month="${row.month}"><span>${esc(row.month)}</span><i style="--share:${(Math.max(0, Number(row.amounts.CNY) || 0) / max) * 100}%"></i>${dual(row.amounts)}</button>`).join("")}</div></section></div>`;
    }
    budget() {
      const rows = this.data.budgets.map((row) => {
        const c = row.metrics.CNY,
          m = row.metrics.MXN;
        const amount = (key) => dual({ CNY: c[key], MXN: m[key] });
        return [
          `<button class="em-link" data-action="budget-company" data-company="${esc(
            row.company
          )}">${esc(row.company)}</button>`,
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
      )}</h3>${button("policy", "Budget policy")}</div>${table(["Subsidiary", "Effective budget", "Approved budget occupation", "Budget difference", "Budget execution rate"], rows)}</section>`;
    }
    details() {
      const d = this.data,
        issues = d.issues || {};
      const rows = d.rows.map((row) => [
        `<button class="em-link" data-action="detail" data-key="${esc(
          row.key
        )}">${esc(row.number)}</button>`,
        esc(row.company || t("Company not matched")),
        esc(row.application_date || "—"),
        esc(t(STATUS[row.status] || "Unknown")),
        esc(t(d.categories[row.management_category] || "Unclassified")),
        dual(row.amounts),
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
      )}<span class="em-count">${d.total}</span></h3></div>${table(["Approval number", "Subsidiary", "Application date", "Approval status", "Management category", "Amount", "Review issues"], rows)}<div class="em-pagination">${button("page-prev", "Previous page", d.start <= 0 ? "disabled" : "")}<span>${esc(t("{0}–{1} of {2}", [d.total ? d.start + 1 : 0, Math.min(d.start + 50, d.total), d.total]))}</span>${button("page-next", "Next page", !d.has_more ? "disabled" : "")}</div></section>`;
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
        const { row } = await this.call("get_detail", { key, filters });
        if (!this.current() || token !== this.detailSeq) return;
        body.innerHTML = `<div class="em-detail">${dual(
          row.amounts
        )}<dl><dt>${esc(t("Subsidiary"))}</dt><dd>${esc(
          row.company || t("Company not matched")
        )}</dd><dt>${esc(t("Approval number"))}</dt><dd>${esc(
          row.number
        )}</dd><dt>${esc(t("Application date"))}</dt><dd>${esc(
          row.application_date || "—"
        )}</dd><dt>${esc(t("Date source"))}</dt><dd>${esc(
          t(
            row.date_basis === "submission"
              ? "OA submission date"
              : row.date_basis === "budget"
              ? "Budget month"
              : "Application date"
          )
        )}</dd><dt>${esc(t("Original amount"))}</dt><dd>${esc(
          row.currency
        )} ${textMoney(row.amount)}</dd><dt>${esc(
          t("Exchange rate date")
        )}</dt><dd>${esc(row.rate_date || "—")}</dd><dt>${esc(
          t("CNY exchange rate")
        )}</dt><dd>${esc(row.rates.CNY || "—")}</dd><dt>${esc(
          t("MXN exchange rate")
        )}</dt><dd>${esc(row.rates.MXN || "—")}</dd><dt>${esc(
          t("Detail item")
        )}</dt><dd>${esc(row.line_label || row.description || "—")}</dd></dl>${
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
        }</div>`;
      } catch (error) {
        if (this.current() && token === this.detailSeq)
          body.textContent = errorMessage(error);
      }
    }
    policy() {
      const meta = this.data?.metadata,
        comparison = this.data?.comparison;
      const lines = [
        "Approved expenses by application month; approval does not confirm payment.",
        "MX forms without an application date field use the OA submission date. Invalid or conflicting dates still require review.",
        "Only verified company allocations and amounts enter approved expenditure. Unassigned or undated expenses remain in Data Review.",
        "CNY and MXN are converted independently from original amounts using the application month-start rate.",
        "Missing exchange rates are shown as unavailable; no partial currency total is presented.",
        "Current-month comparison uses matching elapsed days. Incomplete sources, unverified data or missing history disable comparison.",
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
        return this.go("overview", { company: "", category: "" });
      if (action === "company")
        return this.go("analysis", { company: item.dataset.company });
      if (action === "company-review")
        return this.go("review", { company: item.dataset.company });
      if (action === "budget-company")
        return this.go("budget", { company: item.dataset.company });
      if (action === "category")
        return this.go("details", {
          category: item.dataset.category,
          measure: "approved",
        });
      if (action === "trend")
        return this.go("details", {
          month: item.dataset.month,
          measure: "approved",
        });
      if (action === "details") return this.go(action, { measure: "approved" });
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
  return { Workbench, dual, readFilters, shiftMonth, errorMessage, ROUTES };
});
