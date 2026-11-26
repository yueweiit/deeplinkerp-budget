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
  money,
  readFilters,
  shiftMonth,
} = require("../public/js/expense_management.js");

test("current month defaults and corrupt bookmarks cannot break the month picker", () => {
  assert.deepEqual(readFilters(""), {
    month: "2026-11",
    company: "",
    region: "",
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

test("local amount preserves cents and unavailable currency is not zero", () => {
  const amounts = { CNY: "10000000000000.09", MXN: null };
  assert.match(money(amounts, "CNY"), /<strong>.*CNY.*10,000,000,000,000.09<\/strong>/);
  assert.doesNotMatch(money(amounts, "CNY"), /MXN/);
  assert.match(money(amounts, "MXN"), /MXN.*—/);
  assert.doesNotMatch(money(amounts, "MXN"), /CNY|0\.00/);
  assert.match(money(amounts, null), /Region not configured/);
});

test("regional cards and company rows display one currency per region and company", () => {
  const work = Object.create(Workbench.prototype);
  Object.assign(work, {view: "overview", filters: {}, content(html) {this.html = html;}, data: {
    metadata: {complete: true, has_data: true}, summary: {review: 0, pending: 0, excluded: 0}, undated: 0,
    regions: [
      {region: "cn", label: "China", currency: "CNY", summary: {amounts: {CNY: "100", MXN: "250"}, applications: 1, count: 1}, comparison: {percent: "50.0"}},
      {region: "mx", label: "Mexico", currency: "MXN", summary: {amounts: {CNY: "80", MXN: "200"}, applications: 1, count: 1}, comparison: {percent: null}},
    ],
    company_rows: [
      {company: "CN <test>", display_currency: "CNY", amounts: {CNY: "100", MXN: "250"}, regional_share: "100.0", applications: 1, review: 0},
      {company: "MX", display_currency: "MXN", amounts: {CNY: "80", MXN: "200"}, regional_share: "100.0", applications: 1, review: 0},
    ],
  }});
  work.render();
  assert.match(work.html, /China/);
  assert.match(work.html, /Mexico/);
  assert.match(work.html, /data-region="mx"/);
  assert.match(work.html, /Share within region/);
  assert.match(work.html, /CN &lt;test&gt;/);
  assert.match(work.html, /100\.0%/);
  assert.doesNotMatch(work.html, /250|80\.00/);
  const rows = work.companies().match(/<tr>.*?<\/tr>/g);
  assert.match(rows[1], /CNY/);
  assert.doesNotMatch(rows[1], /MXN/);
  assert.match(rows[2], /MXN/);
  assert.doesNotMatch(rows[2], /CNY/);
});

test("region drilldowns carry region and company changes clear stale regional filters", () => {
  assert.equal(readFilters('?expense_filters=' + encodeURIComponent(JSON.stringify({region: "bad"}))).region, "");
  const work = Object.create(Workbench.prototype);
  Object.assign(work, {filters: {month: "2026-11", company: "", region: "mx"}, syncURL() {}, controls() {}, load() {}});
  let options;
  global.frappe.set_route = (route, values) => {options = values.expense_filters;};
  for (const action of ["details", "trend", "source-category"]) {
    work.click({target: {closest: () => ({dataset: {action, region: "mx", category: "a".repeat(64), month: "2026-10"}})}});
    assert.equal(options.region, "mx");
    assert.equal(options.measure, "approved");
  }
  work.update({company: "China company"});
  assert.equal(work.filters.region, "");
  work.filters.region = "cn";
  work.go("overview", {company: "Mexico company"});
  assert.equal(options.region, "");
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
      region: "",
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
  assert.deepEqual(readFilters("?expense_filters=" + encodeURIComponent(JSON.stringify(filters))), {...filters, region: "", measure: ""});
  const work = Object.create(Workbench.prototype);
  work.filters = filters;
  let target;
  work.go = (view, values) => {target = {view, values};};
  work.click({target: {closest: () => ({dataset: {action: "source-category", category: "b".repeat(64)}})}});
  assert.deepEqual(target, {view: "details", values: {source_category: "b".repeat(64), measure: "approved", region: ""}});
});

test("composition has one keyboard entry per row and keeps exact filters when returning to analysis", () => {
  const work = Object.create(Workbench.prototype), selected = "b".repeat(64);
  work.filters = {month: "2026-10", company: "Company A", category: "service", source_category: "", region: "", measure: ""};
  work.data = {regions: [{region: "cn", label: "China", currency: "CNY", trend: [],
    source_category_rows: [{category: selected, source_path: ["Services", "Rent"], amounts: {CNY: "100"}, count: 2}]}]};
  const analysis = work.analysis();
  const buttons = [...analysis.matchAll(/<button ([^>]*data-action="source-category"[^>]*)>/g)];
  assert.equal(buttons.length, 1);
  assert.match(analysis, /class="em-category-count">2<svg/);
  assert.doesNotMatch(analysis, /aria-hidden="true">›/);
  const routes = [];
  global.frappe.set_route = (route, options) => routes.push({route, options});
  for (const [, attrs] of buttons) {
    const dataset = Object.fromEntries([...attrs.matchAll(/data-([a-z]+)="([^"]*)"/g)].map(([, key, value]) => [key, value]));
    work.click({target: {closest: () => ({dataset})}});
  }
  const filters = {...work.filters, source_category: selected, region: "cn", measure: "approved"};
  assert.deepEqual(routes, [{route: "expense-details", options: {expense_filters: filters}}]);
  work.filters = readFilters("?expense_filters=" + encodeURIComponent(JSON.stringify(filters)));
  work.click({target: {closest: () => ({dataset: {action: "analysis"}})}});
  assert.deepEqual(routes.at(-1), {route: "expense-analysis", options: {expense_filters: {...filters, measure: ""}}});
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
  work.data.regions = [{region: "cn", label: "China", currency: "CNY", source_category_rows: work.data.source_category_rows, trend: []}];
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

test("allocated detail distinguishes original expense from selected amount and escapes source text", async () => {
  const previous = global.frappe.ui;
  let body;
  global.frappe.ui = {Dialog: class {
    constructor() {body = {}; this.fields_dict = {body: {$wrapper: [body]}};}
    show() {}
  }};
  try {
    const work = Object.create(Workbench.prototype);
    Object.assign(work, {detailSeq: 0, filters: {}, view: "details", options: {}, current: () => true,
      call: async () => ({row: {number: "approval", company: "A", amount: "30.00", currency: "CNY", amounts: {CNY: "30.00"}, rates: {}, issues: []},
        original_expense: {origin: "detail", amount: "100.00", currency: "CNY", split_count: 2, row_number: "4",
          table_label: "Expense table", source_entity: "Department A", description: '<img src=x onerror="bad()">'}})});
    await work.detail("a");
    assert.match(body.innerHTML, /Original expense line/);
    assert.match(body.innerHTML, /Original expense amount<\/dt><dd>CNY 100\.00/);
    assert.match(body.innerHTML, /Current detail amount<\/dt><dd>CNY 30\.00/);
    assert.match(body.innerHTML, /Allocated into 2 detail lines/);
    assert.match(body.innerHTML, /Source row<\/dt><dd>4/);
    assert.match(body.innerHTML, /&lt;img/);
    assert.doesNotMatch(body.innerHTML, /<img/);
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

test("expense detail restores DingTalk entry and rejects unsafe or unavailable links", async () => {
  const previous = global.frappe.ui;
  let body, response;
  global.frappe.ui = {Dialog: class {
    constructor() {body = {}; this.fields_dict = {body: {$wrapper: [body]}};}
    show() {}
    hide() {}
  }};
  try {
    const work = Object.create(Workbench.prototype);
    Object.assign(work, {detailSeq: 0, filters: {}, view: "details", options: {},
      current: () => true, call: async () => response});
    const row = {number: "approval", company: "A", amounts: {CNY: "10"}, rates: {}, issues: []};
    const url = "dingtalk://dingtalkclient/page/link?url=https%3A%2F%2Faflow.dingtalk.com%2F&pc_slide=true";
    response = {row, approval_url: url};
    await work.detail("a");
    assert.match(body.innerHTML, /Open Original in DingTalk<\/a>/);
    assert.match(body.innerHTML, /&amp;pc_slide=true/);
    assert.match(body.innerHTML, /rel="noopener noreferrer"/);
    for (const invalid of ["", null, "javascript:alert(1)", 'https://evil.invalid/" onclick="bad()', {}]) {
      response = {row, approval_url: invalid};
      await work.detail("a");
      assert.doesNotMatch(body.innerHTML, /href=|onclick=/);
      assert.match(body.innerHTML, /Original approval link is unavailable/);
      assert.match(body.innerHTML, /Approval number/);
    }
    const deferred = pending();
    work.call = () => deferred.promise;
    const old = work.detail("old");
    work.detailSeq++;
    deferred.resolve({row, approval_url: url});
    await old;
    assert.equal(body.innerHTML, undefined);
  } finally {global.frappe.ui = previous;}
});
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
