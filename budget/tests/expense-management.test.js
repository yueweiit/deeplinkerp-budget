const test = require("node:test");
const assert = require("node:assert/strict");
global.frappe = {
  datetime: { get_today: () => "2026-11-24" },
  format: (value) => String(value),
};
global.DeepLinkERPMexicoExpenseMoney =
  require("../../../deeplinkerp_branding/deeplinkerp_branding/public/js/mexico_expense_money.js")(
    global
  );
const {
  Workbench,
  dual,
  readFilters,
  shiftMonth,
} = require("../public/js/expense_management.js");

test("current month defaults and corrupt bookmarks cannot break the month picker", () => {
  assert.deepEqual(readFilters(""), {
    month: "2026-11",
    company: "",
    category: "",
    source_category: "",
    measure: "",
  });
  for (const month of ["invalid", "2026-13", "2026-12", "1899-12"]) {
    assert.equal(
      readFilters(
        "?expense_filters=" + encodeURIComponent(JSON.stringify({ month }))
      ).month,
      "2026-11"
    );
  }
  assert.equal(readFilters("?expense_filters=%7Bbroken").company, "");
});

test("dual amount hierarchy preserves cents and unavailable currency is not zero", () => {
  const html = dual({ CNY: "10000000000000.09", MXN: null });
  assert.match(html, /<strong>.*CNY.*10,000,000,000,000.09<\/strong>/);
  assert.match(html, /<small>.*MXN.*—<\/small>/);
  assert.doesNotMatch(html, /MXN.*0\.00/);
});

test("all-company navigation explicitly clears company and category while retaining month", () => {
  const work = Object.create(Workbench.prototype);
  work.filters = { month: "2026-10", company: "Mexico A", category: "salary" };
  let route, options;
  global.frappe.set_route = (r, o) => {
    route = r;
    options = o;
  };
  work.click({ target: { closest: () => ({ dataset: { action: "all" } }) } });
  assert.equal(route, "expense-management");
  assert.deepEqual(options, {
    expense_filters: {
      month: "2026-10",
      company: "",
      category: "",
      source_category: "",
      measure: "",
    },
  });
});

test("month navigation crosses years correctly", () => {
  assert.equal(shiftMonth("2026-01", -1), "2025-12");
  assert.equal(shiftMonth("2026-12", 1), "2027-01");
});

test("source category bookmarks and drilldowns retain exact scope alongside optional legacy filters", () => {
  const filters = {month: "2026-10", company: "A", source_category: "a".repeat(64), category: "service"};
  assert.deepEqual(readFilters("?expense_filters=" + encodeURIComponent(JSON.stringify(filters))), {...filters, measure: ""});
  const work = Object.create(Workbench.prototype);
  work.filters = filters;
  let target;
  work.go = (view, values) => {target = {view, values};};
  work.click({target: {closest: () => ({dataset: {action: "source-category", category: "b".repeat(64)}})}});
  assert.deepEqual(target, {view: "details", values: {source_category: "b".repeat(64), measure: "approved"}});
});

test("expense composition and detail tables use escaped OA paths without requiring management categories", () => {
  const work = Object.create(Workbench.prototype), path = ["Services <source>", "Payroll"];
  work.view = "details";
  work.data = {
    source_category_rows: [{category: "a".repeat(64), source_path: path, count: 1, amounts: {CNY: "10", MXN: "25"}}],
    category_rows: [{category: "unclassified", count: 1}], categories: {}, trend: [],
    rows: [{key: "a", number: "approval", company: "A", application_date: "2026-11-10", status: "approved",
      source_path: path, management_category: "", issues: [], amounts: {CNY: "10", MXN: "25"}}],
    total: 1, start: 0, has_more: false,
  };
  const analysis = work.analysis(), details = work.details();
  assert.match(analysis, /data-action="source-category"/);
  assert.match(analysis, /Services &lt;source&gt; \/ Payroll/);
  assert.match(details, /OA expense category/);
  assert.match(details, /Services &lt;source&gt; \/ Payroll/);
  assert.doesNotMatch(details, /Unclassified|Needs review/);
  work.data.rows[0].source_path = [];
  assert.match(work.details(), /Category not provided/);
});

test("native expense detail displays source category and subcategory separately", async () => {
  const previous = global.frappe.ui;
  let body;
  global.frappe.ui = {Dialog: class {
    constructor() {body = {}; this.fields_dict = {body: {$wrapper: [body]}};}
    show() {}
  }};
  try {
    const work = Object.create(Workbench.prototype);
    Object.assign(work, {detailSeq: 0, filters: {}, view: "details", options: {issues: {}},
      current: () => true, call: async () => ({row: {number: "approval", company: "A", source_path: ["Services", "Payroll"],
        amount: "990", currency: "MXN", amounts: {CNY: "346.50", MXN: "990"}, rates: {CNY: "0.35", MXN: "1"},
        issues: [], description: "Original approval text"}})});
    await work.detail("a");
    assert.match(body.innerHTML, /OA expense category<\/dt><dd>Services/);
    assert.match(body.innerHTML, /OA expense subcategory<\/dt><dd>Payroll/);
    assert.match(body.innerHTML, /Original approval text/);
    assert.doesNotMatch(body.innerHTML, /Management category|Needs review/);
  } finally {global.frappe.ui = previous;}
});

function pending() {
  let resolve, reject;
  const promise = new Promise((ok, no) => {
    resolve = ok;
    reject = no;
  });
  return { promise, resolve, reject };
}
function workbench() {
  const work = Object.create(Workbench.prototype),
    calls = [],
    renders = [];
  Object.assign(work, {
    seq: 0,
    detailSeq: 0,
    start: 0,
    active: true,
    filters: { month: "2026-11", company: "", category: "" },
    view: "overview",
    options: {},
    current() {
      return this.active;
    },
    syncURL() {},
    controls() {},
    content(html) {
      this.html = html;
    },
    render() {
      renders.push(this.data);
    },
    call() {
      const request = pending();
      calls.push(request);
      return request.promise;
    },
  });
  return { work, calls, renders };
}

test("late responses cannot replace a newer company selection", async () => {
  const { work, calls, renders } = workbench();
  const first = work.load();
  assert.equal(work.loading, true);
  work.filters.company = "A";
  const second = work.load();
  calls[1].resolve({ company: "A", companies: ["A"], categories: {} });
  assert.equal(await second, true);
  assert.equal(work.loading, false);
  calls[0].resolve({ company: "old", companies: [], categories: {} });
  assert.equal(await first, false);
  assert.equal(renders.length, 1);
  assert.equal(work.data.company, "A");
});

test("leaving a page drops pending responses and closes picker", async () => {
  const { work, calls, renders } = workbench();
  let closed = 0;
  work.picker = {
    destroy() {
      closed++;
    },
  };
  const request = work.load();
  work.hide();
  assert.equal(work.loading, false);
  calls[0].resolve({ companies: [] });
  assert.equal(await request, false);
  assert.equal(renders.length, 0);
  assert.equal(closed, 1);
});

test("failed refresh removes stale figures and exposes retry", async () => {
  const { work, calls } = workbench();
  work.data = { old: true };
  const request = work.load();
  assert.equal(work.data, null);
  calls[0].reject(new Error("internal secret"));
  assert.equal(await request, false);
  assert.equal(work.loading, false);
  assert.match(work.html, /data-action="retry"/);
  assert.doesNotMatch(work.html, /internal secret/);
});

test("an obsolete response cannot clear a newer request's loading state", async () => {
  const { work, calls, renders } = workbench();
  const first = work.load();
  work.filters.company = "A";
  const second = work.load();
  calls[0].resolve({ company: "old", companies: [], categories: {} });
  assert.equal(await first, false);
  assert.equal(work.loading, true);
  assert.equal(renders.length, 0);
  calls[1].resolve({ company: "A", companies: ["A"], categories: {} });
  assert.equal(await second, true);
  assert.equal(work.loading, false);
  assert.equal(work.data.company, "A");
});
