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
      measure: "",
    },
  });
});

test("month navigation crosses years correctly", () => {
  assert.equal(shiftMonth("2026-01", -1), "2025-12");
  assert.equal(shiftMonth("2026-12", 1), "2027-01");
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
