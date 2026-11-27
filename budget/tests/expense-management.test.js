const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
global.frappe = {
  datetime: { get_today: () => "2026-11-24" },
  format: (value, options = {}) => ["Currency", "Float"].includes(options.fieldtype)
    ? Number(value).toFixed(options.precision ?? 2) : String(value),
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
  amountReconciliation,
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

test("native Desk anchor navigation preserves all drilldown filters after route serialization", () => {
  const expected = {month: "2026-10", company: "A & B + 中国", region: "cn", category: "",
    source_category: "b".repeat(64), measure: "approved"};
  let anchorClick;
  const jquery = () => ({on: (event, selector, handler) => {anchorClick = handler;}});
  jquery.isArray = Array.isArray;
  jquery.isPlainObject = value => value != null && Object.prototype.toString.call(value) === "[object Object]";
  jquery.map = (values, callback) => values.flatMap((value, index) => {
    const result = callback(value, index);
    return result == null ? [] : [result];
  });
  const context = {URLSearchParams, $: jquery, location: new URL("https://expense.test/desk/expense-analysis"),
    frappe: {provide() {}, utils: {make_event_emitter() {}}, after_ajax: callback => callback()},
    localStorage: {getItem: () => null}, addEventListener() {}, setTimeout: callback => callback()};
  context.window = context;
  context.history = {pushState: (_state, _title, url) => {context.location = new URL(url, context.location);}};
  vm.createContext(context);
  vm.runInContext(fs.readFileSync(path.join(__dirname, "../../../frappe/frappe/public/js/frappe/router.js"), "utf8"), context);
  context.frappe.router.route = () => {};
  context.frappe.router.show_external_link_warning_if_needed = () => false;
  const target = new URL("/desk/expense-details?expense_filters=" + encodeURIComponent(JSON.stringify({...expected, category: "salary"})), context.location);
  anchorClick({currentTarget: {hostname: target.hostname, pathname: target.pathname, search: target.search, hash: "",
    getAttribute: name => name === "href" ? target.href : null}, preventDefault() {}});
  // Execute Frappe's actual click handler and router, rather than only checking
  // the arguments passed to set_route. A native anchor encodes the JSON again.
  const encoded = new URLSearchParams(context.location.search).get("expense_filters");
  assert.equal(typeof JSON.parse(encoded), "string");
  assert.deepEqual(readFilters(context.location.search), expected);
});

test("nested filter decoding stays bounded and rejects malformed or non-object input", () => {
  const defaults = readFilters("");
  for (const value of ["{broken", "[]", "null", "123", JSON.stringify("{broken"),
    JSON.stringify(JSON.stringify(JSON.stringify({company: "Too deeply nested"})))]) {
    assert.deepEqual(readFilters("?expense_filters=" + encodeURIComponent(value)), defaults);
  }
  const value = JSON.stringify(JSON.stringify({company: "A", region: "mx", view: "review", amount: "999"}));
  assert.deepEqual(readFilters("?expense_filters=" + encodeURIComponent(value)), {...defaults, company: "A", region: "mx"});
});

test("local amount preserves cents and unavailable currency is not zero", () => {
  const amounts = { CNY: "10000000000000.09", MXN: null };
  assert.match(money(amounts, "CNY"), /<strong>.*CNY.*10,000,000,000,000.09<\/strong>/);
  assert.doesNotMatch(money(amounts, "CNY"), /MXN/);
  assert.match(money(amounts, "MXN"), /MXN.*—/);
  assert.doesNotMatch(money(amounts, "MXN"), /CNY|0\.00/);
  assert.match(money(amounts, null), /Region not configured/);
});

test("verified comparison remains visible with review notices and explains unavailable baselines", () => {
  const work=Object.create(Workbench.prototype);
  Object.assign(work,{view:'overview',filters:{},content(html){this.html=html;},data:{
    metadata:{complete:false,has_data:true},summary:{review:2,pending:1,excluded:1},undated:14,company_rows:[],
    regions:[{region:'cn',label:'China',currency:'CNY',summary:{amounts:{CNY:'200'},applications:1,count:1},
      comparison:{percent:'100.0',status:'comparable',basis:'verified'}}]
  }});
  work.render();
  assert.match(work.html,/\+100\.0%/);
  assert.match(work.html,/Source data incomplete/);
  assert.match(work.html,/14 lines without application dates/);
  assert.doesNotMatch(work.html,/Not comparable/);
  for(const [status,label] of Object.entries({no_current_verified:'No verified expenditure this month',
    no_previous_verified:'No verified expenditure last month',amount_unavailable:'Comparison exchange rate unavailable',
    previous_zero:"Last month's verified expenditure was zero"})) {
    work.data.regions[0].comparison={percent:null,status};work.render();
    assert(work.html.includes(label.replace(/'/g, "&#39;")));
    assert.doesNotMatch(work.html,/Not comparable/);
  }
});

test("comparison shows previous-period review caveats even when the current month has no issues", () => {
  const work=Object.create(Workbench.prototype);
  const comparison={percent:'100.0',status:'comparable',basis:'verified',warnings:['review','excluded'],
    periods:{current:{review:0,excluded:0},previous:{review:3,excluded:2}}};
  Object.assign(work,{view:'overview',filters:{},content(html){this.html=html;},data:{
    metadata:{complete:true,has_data:true},summary:{review:0,pending:0,excluded:0},undated:0,company_rows:[],
    regions:[{region:'cn',label:'China',currency:'CNY',summary:{amounts:{CNY:'200'},applications:1,count:1},comparison}]
  }});
  work.render();
  assert.match(work.html,/\+100\.0%/);
  assert.match(work.html,/Previous period: 3 lines need review; 2 unverified lines excluded\./);
  assert.doesNotMatch(work.html,/Current period:/);
  assert.doesNotMatch(work.html,/em-data-notice/);
  comparison.periods.current={review:2,excluded:1};work.render();
  assert.match(work.html,/Current period: 2 lines need review; 1 unverified lines excluded\./);
  delete comparison.periods;work.render();
  assert.match(work.html,/Some expenses in the comparison periods still need review\./);
  comparison.warnings=[];work.render();
  assert.doesNotMatch(work.html,/Some expenses in the comparison periods still need review\./);
});

test("virtual Group renders in the overview and detail without a Company record or raw filter label", () => {
  const key='__expense_group__',work=Object.create(Workbench.prototype);
  Object.assign(work,{view:'overview',filters:{region:'group'},content(html){this.html=html;},data:{
    metadata:{complete:true,has_data:true},summary:{review:0,pending:0,excluded:0},undated:0,
    regions:[{region:'group',label:'Group',currency:'CNY',summary:{amounts:{CNY:'60'},applications:1,count:1},comparison:{percent:'100.0',status:'comparable'}}],
    company_rows:[{company:key,region:'group',display_currency:'CNY',amounts:{CNY:'60'},applications:1,review:0,regional_share:null}],
    rows:[{key:'line',number:'NO-1',company:'',is_group_expense:1,display_currency:'CNY',amounts:{CNY:'60'},issues:[],status:'approved'}],total:1,start:0
  }});
  work.render();
  assert.match(work.html,/data-company="__expense_group__"/);
  assert.match(work.html,/>Group/);
  assert.doesNotMatch(work.html.replace(/<[^>]*>/g,''),/__expense_group__|Company not matched|Mexico/);
  assert.match(work.details(),/>Group<\/td>/);
  assert.equal(readFilters('?expense_filters='+encodeURIComponent(JSON.stringify({company:key,region:'group'}))).region,'group');
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

test("source category bookmarks discard the retired management filter while retaining the visible scope", () => {
  const filters = {month: "2026-10", company: "A", source_category: "a".repeat(64), category: "service"};
  assert.deepEqual(readFilters("?expense_filters=" + encodeURIComponent(JSON.stringify(filters))), {...filters, category: "", region: "", measure: ""});
  const work = Object.create(Workbench.prototype);
  work.filters = filters;
  let target;
  work.go = (view, values) => {target = {view, values};};
  work.click({target: {closest: () => ({dataset: {action: "source-category", category: "b".repeat(64)}})}});
  assert.deepEqual(target, {view: "details", values: {source_category: "b".repeat(64), measure: "approved", region: ""}});
});

test("unified controls offer one original expense category selector without a second classification section", () => {
  const previous = global.DeepLinkERPMexicoExpenseDateRange;
  const panel = {}, key = "a".repeat(64), work = Object.create(Workbench.prototype);
  global.DeepLinkERPMexicoExpenseDateRange = {wholeMonths: () => ({}), MonthPicker: class {destroy() {}}};
  Object.assign(work, {view: "details", current: () => true,
    filters: {month: "2026-11", company: "A", region: "cn", category: "salary", source_category: key},
    options: {companies: ["A"], categories: {salary: "Salaries"},
      source_categories: [{key, source_path: ["Services", "Rent"]}]},
    host: {querySelector: selector => selector === ".em-controls" ? panel : {}}});
  try {
    work.controls();
    assert.equal((panel.innerHTML.match(/data-filter="source_category"/g) || []).length, 1);
    assert.match(panel.innerHTML, /Expense category/);
    assert.match(panel.innerHTML, /Services \/ Rent/);
    assert.doesNotMatch(panel.innerHTML, /data-filter="category"|Unified classification|Management category|em-classification-options/);
  } finally {global.DeepLinkERPMexicoExpenseDateRange = previous;}
});

test("stale page state cannot carry the retired management filter into queries exports URLs or drilldowns", () => {
  const work = Object.create(Workbench.prototype), source = "a".repeat(64);
  const previous = {location: global.location, history: global.history, setRoute: global.frappe.set_route};
  let writtenURL, routed;
  Object.assign(work, {filters: {month: "2026-10", company: "A", region: "cn", category: "salary",
    source_category: source, measure: "approved"}, current: () => true, controls() {}, load() {}});
  global.location = new URL("https://expense.test/desk/expense-details?sidebar=Expense%20Management#keep");
  global.history = {state: null, replaceState: (_state, _title, url) => {writtenURL = new URL(url, global.location);}};
  global.frappe.set_route = (route, options) => {routed = {route, options};};
  try {
    // Every query, detail and export sends this same normalized filter payload.
    for (const view of ["overview", "analysis", "budget", "details", "review"]) {
      work.view = view;
      assert.deepEqual(work.requestFilters(), {...work.filters, category: "", view});
    }
    work.go("details", {category: "office"});
    assert.equal(routed.options.expense_filters.category, "");
    assert.equal(routed.options.expense_filters.source_category, source);
    work.syncURL();
    assert.equal(work.filters.category, "");
    assert.equal(JSON.parse(writtenURL.searchParams.get("expense_filters")).category, "");
    assert.equal(writtenURL.searchParams.get("sidebar"), "Expense Management");
    assert.equal(writtenURL.hash, "#keep");
    work.update({month: "2026-09", category: "tax"});
    assert.equal(work.filters.category, "");
    assert.equal(work.filters.source_category, source);
    assert.equal(work.filters.month, "2026-09");
  } finally {
    global.location = previous.location;
    global.history = previous.history;
    global.frappe.set_route = previous.setRoute;
  }
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
  const filters = {...work.filters, category: "", source_category: selected, region: "cn", measure: "approved"};
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
  assert.match(details, /Expense category/);
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
    assert.match(body.innerHTML, /Expense category<\/dt><dd>Services/);
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
    hide() {}
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
    work.call = async () => ({row: {number: "approval", amount_reviewed: true,
      amounts: {}, rates: {}, issues: []}, original_expense: {amount: "6400.00", currency: "MXN", unverified: true}});
    await work.detail("reviewed");
    assert.match(body.innerHTML, /Approval amount confirmed for reporting/);
    assert.doesNotMatch(body.innerHTML, /excluded from verified expenditure/);
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

test("unverified source amount and execution location remain distinct from reporting region", async () => {
  const previous = global.frappe.ui;
  let body;
  global.frappe.ui = {Dialog: class {
    constructor() {body = {}; this.fields_dict = {body: {$wrapper: [body]}};}
    show() {}
  }};
  try {
    const work = Object.create(Workbench.prototype);
    Object.assign(work, {detailSeq: 0, filters: {}, view: "review", options: {}, current: () => true,
      call: async () => ({row: {number: "approval", company: "A", region: "cn", display_currency: "CNY",
        amount: null, currency: "MXN", amounts: {CNY: null}, rates: {}, issues: ["total"]},
        original_expense: {origin: "header", amount: "6400.00", currency: "MXN", split_count: 1,
          unverified: true, execution_region: "Mexico<script>bad</script>"}})});
    await work.detail("a");
    assert.match(body.innerHTML, /Original expense amount<\/dt><dd>MXN 6,?400\.00/);
    assert.match(body.innerHTML, /excluded from verified expenditure/);
    assert.match(body.innerHTML, /Reporting region<\/dt><dd>China/);
    assert.match(body.innerHTML, /Execution Region<\/dt><dd>Mexico&lt;script&gt;/);
    assert.doesNotMatch(body.innerHTML, /<script>/);
  } finally {global.frappe.ui = previous;}
});

function amountEvidence() {
  return {source_key: "source", revision: "revision", fingerprint: "fingerprint", can_confirm: true,
    currency: "MXN", header_amount: "6400.00", tables: [{label: '<img src=x onerror="bad()">', total: "6401.00",
      calculated_total: "6400.00", difference: "1.00", rows: [
        {row_number: "1", quantity: "4710", unit_price: "0.64", amount: "3015.00", calculated_amount: "3014.40", difference: "0.60"},
        {row_number: "2", quantity: "1200", unit_price: "0.6400", amount: "768.00", calculated_amount: "768.00", difference: "0.00"}]}]};
}

test("amount reconciliation shows exact header, details and row differences with escaped evidence", () => {
  const data = amountEvidence();
  const html = amountReconciliation(data);
  assert.match(html, /6,?400\.00/);
  assert.match(html, /6,?401\.00/);
  assert.match(html, /0\.60/);
  assert.match(html, /0\.6400/);
  assert.equal((html.match(/class="em-amount-mismatch"/g) || []).length, 1);
  assert.match(html, /data-confirm-amount/);
  assert.match(html, /maxlength="1000" required/);
  assert.doesNotMatch(html, /<img/);
  data.can_confirm = false;
  data.review = {state: "confirmed", amount: "6400.00", currency: "MXN", note: "<script>unsafe</script>", reviewed_by: "Manager", reviewed_at: "2026-11-27"};
  assert.doesNotMatch(amountReconciliation(data), /data-confirm-amount|<script>/);
  assert.match(amountReconciliation(data), /Approval amount confirmed/);
  data.review.state = "stale";
  data.can_confirm = true;
  assert.match(amountReconciliation(data), /Source changed; the previous amount confirmation is no longer valid/);
  assert.match(amountReconciliation(data), /data-confirm-amount/);
});

test("confirmation requires a reason, sends only server identities and prevents duplicate clicks", async () => {
  const request = pending(), calls = [], alerts = [];
  const submit = {disabled: false}, input = {value: "   ", focus() {this.focused = true;}}, error = {};
  const elements = {"[data-confirm-amount]": submit, "[data-amount-note]": input, "[data-amount-error]": error};
  const body = {querySelector: key => elements[key]}, dialog = {hide() {this.hidden = true;}};
  const work = Object.create(Workbench.prototype);
  Object.assign(work, {dialog, current: () => true, load: async () => {work.reloaded = true;},
    call: (method, args) => {calls.push({method, args}); return request.promise;}});
  const oldAlert = global.frappe.show_alert;
  global.frappe.show_alert = value => alerts.push(value);
  try {
    await work.confirmAmount("line", amountEvidence(), dialog, body);
    assert.equal(calls.length, 0);
    assert.equal(error.textContent, "A review note is required");
    input.value = " Verified amount ";
    const saving = work.confirmAmount("line", amountEvidence(), dialog, body);
    assert(submit.disabled && input.disabled);
    await work.confirmAmount("line", amountEvidence(), dialog, body);
    assert.deepEqual(calls, [{method: "confirm_approval_amount", args: {key: "line", source_key: "source",
      expected_revision: "revision", expected_fingerprint: "fingerprint", note: "Verified amount"}}]);
    request.resolve({ok: true});
    await saving;
    assert(dialog.hidden && work.reloaded && alerts.length === 1);
    assert(!submit.disabled && !input.disabled);
  } finally {global.frappe.show_alert = oldAlert;}
});

test("failed confirmation preserves the reason and shows an error without a success message", async () => {
  const submit = {disabled: false}, input = {value: "Checked the approval"}, error = {};
  const elements = {"[data-confirm-amount]": submit, "[data-amount-note]": input, "[data-amount-error]": error};
  const body = {querySelector: key => elements[key]}, dialog = {hide() {assert.fail("must stay open");}};
  const work = Object.create(Workbench.prototype);
  Object.assign(work, {dialog, current: () => true, call: async () => {throw {responseJSON: {
    exc_type: "ValidationError", _server_messages: JSON.stringify([JSON.stringify({message: "Source changed"})])}};}});
  await work.confirmAmount("line", amountEvidence(), dialog, body);
  assert.equal(error.textContent, "Source changed");
  assert.equal(input.value, "Checked the approval");
  assert(!submit.disabled && !input.disabled);
});
