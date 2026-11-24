const test = require("node:test");
const assert = require("node:assert/strict");
global.__ = (value, args = []) =>
  value.replace(/\{(\d+)\}/g, (_, n) => args[n] ?? "");
global.frappe = {
  utils: { escape_html: (value) => String(value) },
  datetime: { str_to_user: (value) => value },
};
const { Controller, label } = require("../public/js/expense_sync.js");
const { Workbench } = require("../public/js/expense_management.js");
function state(revision = "one") {
  return {
    scheduler_enabled: true,
    sources: Object.fromEntries(
      ["cn", "mx"].map((key) => [
        key,
        {
          enabled: true,
          configured: true,
          can_manage: true,
          can_sync: true,
          active: false,
          status: "success",
          revision,
        },
      ])
    ),
  };
}
function setup() {
  const loads = [],
    calls = [];
  const work = {
    current: () => true,
    host: { querySelector: () => null },
    data: report(state()),
    load: async (keep) => {
      loads.push(keep);
      work.data = report(sync.state);
      return true;
    },
    call: async (method, args) => {
      calls.push([method, args]);
      return state();
    },
  };
  const sync = new Controller(work);
  sync.active = true;
  return { sync, work, loads, calls };
}
function report(status) {
  return {
    metadata: {
      sync_revision: Object.fromEntries(
        Object.entries(status.sources).map(([key, source]) => [key, source.revision])
      ),
    },
    companies: [], categories: {}, issues: {},
  };
}
function integrated() {
  const work = Object.create(Workbench.prototype);
  const env = { status: state(), requests: 0, renders: [], controls: 0 };
  Object.assign(work, {
    seq: 0, detailSeq: 0, start: 0,
    current: () => true,
    host: { querySelector: () => null },
    requestFilters: () => ({ month: "2026-11", company: "A" }),
    syncURL() {},
    content(html) { env.content = html; },
    controls() { env.controls++; },
    render() { env.renders.push(this.data); },
    call: async (method) => {
      if (method === "get_sync_status") return env.status;
      assert.equal(method, "get_data");
      env.requests++;
      return env.read();
    },
  });
  env.read = async () => report(env.status);
  env.work = work;
  env.sync = new Controller(work);
  env.sync.active = true;
  return env;
}
function pending() {
  let resolve;
  const promise = new Promise((done) => {
    resolve = done;
  });
  return { promise, resolve };
}
test("a completed revision reloads once and retains the month picker", async () => {
  const { sync, work, loads } = setup();
  await sync.poll();
  work.call = async () => state("two");
  await sync.poll();
  await sync.poll();
  assert.deepEqual(loads, [true]);
  sync.stop();
});
test("a late initial report cannot acknowledge a newer status revision", async () => {
  const env = integrated(), initial = pending();
  env.read = () => initial.promise;
  const load = env.work.load();
  env.status = state("two");
  await env.sync.poll();
  assert.equal(env.requests, 1, "polling must not interrupt the initial request");
  initial.resolve(report(state("one")));
  assert.equal(await load, true);
  env.read = async () => report(env.status);
  await env.sync.poll();
  await env.sync.poll();
  assert.equal(env.requests, 2);
  assert.deepEqual(env.work.data.metadata.sync_revision, {cn: "two", mx: "two"});
  assert.equal(env.controls, 1, "the automatic reread retains current controls");
  env.sync.stop();
});
test("a failed automatic reread retries instead of acknowledging the status", async () => {
  const env = integrated();
  await env.work.load();
  await env.sync.poll();
  env.status = state("two");
  env.read = async () => { throw new Error("temporary local read failure"); };
  await env.sync.poll();
  assert.equal(env.work.data, null);
  assert.equal(env.work.loading, false);
  assert.match(env.content, /em-error/);
  env.read = async () => report(env.status);
  await env.sync.poll();
  await env.sync.poll();
  assert.equal(env.requests, 3);
  assert.equal(env.work.data.metadata.sync_revision.cn, "two");
  env.sync.stop();
});
test("rebuilding completion reloads unchanged facts after the waiting response", async () => {
  const env = integrated();
  env.status.sources.cn.allocation_rebuilding = true;
  env.read = async () => { throw new Error("Expense ownership is being updated automatically. Please wait."); };
  assert.equal(await env.work.load(), false);
  await env.sync.poll();
  assert.equal(env.requests, 1);
  env.status = state();
  env.read = async () => report(env.status);
  await env.sync.poll();
  await env.sync.poll();
  assert.equal(env.requests, 2);
  assert.equal(env.work.data.metadata.sync_revision.cn, "one");
  env.sync.stop();
});
test("rebuild completion remains pending while another report is loading", async () => {
  const env = integrated(), initial = pending();
  await env.work.load();
  env.status.sources.cn.allocation_rebuilding = true;
  await env.sync.poll();
  env.read = () => initial.promise;
  const load = env.work.load();
  env.status = state();
  await env.sync.poll();
  assert.equal(env.sync.refreshAfterSync, true);
  initial.resolve(report(state()));
  await load;
  env.read = async () => report(env.status);
  await env.sync.poll();
  await env.sync.poll();
  assert.equal(env.requests, 3);
  assert.equal(env.sync.refreshAfterSync, false);
  env.sync.stop();
});
test("an overlapping poll cannot cancel or duplicate the current report load", async () => {
  const env = integrated(), updated = pending();
  await env.work.load();
  await env.sync.poll();
  env.status = state("two");
  env.read = () => updated.promise;
  const poll = env.sync.poll();
  await Promise.resolve();
  assert.equal(env.work.loading, true);
  await env.sync.poll();
  assert.equal(env.requests, 2);
  updated.resolve(report(state("two")));
  await poll;
  assert.equal(env.work.loading, false);
  await env.sync.poll();
  assert.equal(env.requests, 2);
  env.sync.stop();
});
test("a hidden page ignores delayed synchronization responses", async () => {
  const { sync, work, loads } = setup(),
    request = pending();
  work.call = () => request.promise;
  const result = sync.poll();
  sync.stop();
  request.resolve(state());
  await result;
  assert.equal(sync.state, undefined);
  assert.deepEqual(loads, []);
});
test("independent commands retain a successful source when the other fails", async () => {
  const { sync, work } = setup(),
    sources = [];
  sync.state = state();
  work.call = async (method, args) => {
    if (method === "get_sync_status") return state();
    sources.push(args.source);
    if (args.source === "mx") throw new Error("private upstream response");
    return { status: "queued" };
  };
  await sync.synchronize();
  assert.deepEqual(sources, ["cn", "mx"]);
  assert.deepEqual(sync.errors, {
    mx: "Synchronization could not be queued. Retry.",
  });
  assert.equal(sync.submitting, false);
  sync.stop();
});
test("busy and unauthorized sources cannot be queued by the toolbar", async () => {
  const { sync, work } = setup(),
    requests = [];
  sync.state = state();
  sync.state.sources.mx.can_sync = false;
  work.call = async (method, args) => {
    if (method === "enqueue_sync") requests.push(args.source);
    return state();
  };
  await sync.synchronize();
  assert.deepEqual(requests, ["cn"]);
  sync.stop();
});
test("settings opens a shared dialog in place without navigating to old pages", async () => {
  const { sync } = setup();
  sync.state = state();
  let configured = 0;
  global.frappe.set_route = () => {
    throw new Error("old page navigation");
  };
  global.frappe.require = (_paths, done) => done();
  global.CNApprovalSettings = {
    open: async (_saved, options) => {
      assert(options.isCurrent());
      configured++;
      return { hide() {} };
    },
  };
  await sync.configure("cn");
  assert.equal(configured, 1);
  assert(sync.settingsDialog);
  sync.stop();
});
test("leaving while settings assets load prevents a late dialog", async () => {
  const { sync } = setup();
  sync.state = state();
  let loaded,
    opened = 0;
  global.frappe.require = (_paths, done) => {
    loaded = done;
  };
  global.CNApprovalSettings = {
    open: async () => {
      opened++;
    },
  };
  const request = sync.configure("cn");
  sync.stop();
  loaded();
  await request;
  assert.equal(opened, 0);
});
test("disabled, unconfigured and failed states never claim synchronization success", () => {
  assert.equal(
    label({ configured: false }),
    "Synchronization configuration required"
  );
  assert.equal(
    label({ configured: true, enabled: false }),
    "Automatic synchronization is disabled"
  );
  assert.equal(
    label({ configured: true, enabled: true, status: "failed" }),
    "Synchronization failed. Retry synchronization."
  );
});
