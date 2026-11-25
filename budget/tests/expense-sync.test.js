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
    can_manage: true,
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
  global.ExpenseSyncSettings = {
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
  global.ExpenseSyncSettings = {
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
test("saving mappings waits for local rebuild and then reloads once without queuing OA", async () => {
  const { sync, work, loads, calls } = setup();
  sync.state = state();
  let saved;
  const rebuilding = state();
  rebuilding.sources.mx.allocation_rebuilding = true;
  global.frappe.require = (_paths, done) => done();
  global.ExpenseSyncSettings = {
    open: async (callback) => { saved = callback; return { hide() {} }; },
  };
  await sync.configure();
  work.call = async (method) => { calls.push([method]); return rebuilding; };
  await saved();
  assert.deepEqual(loads, []);
  assert.equal(sync.refreshAfterSync, true);
  rebuilding.sources.mx.allocation_rebuilding = false;
  await sync.poll();
  await sync.poll();
  assert.deepEqual(loads, [true]);
  assert(calls.every(([method]) => method === "get_sync_status"));
  assert.equal(sync.refreshAfterSync, false);
  sync.stop();
});
test("a settings save whose rebuild already finished refreshes unchanged facts once", async () => {
  const { sync, loads } = setup();
  sync.state = state();
  let saved;
  global.frappe.require = (_paths, done) => done();
  global.ExpenseSyncSettings = { open: async (callback) => { saved = callback; return { hide() {} }; } };
  await sync.configure();
  await saved();
  await sync.poll();
  assert.deepEqual(loads, [true]);
  sync.stop();
});
test("toolbar and dialog distinguish OA reads, local rebuilds and overlapping jobs", () => {
  for (const [active, rebuilding, text] of [
    [false, true, "Recalculating local mappings…"],
    [true, false, "Synchronizing OA approvals…"],
    [true, true, "Synchronizing OA approvals and recalculating local mappings…"],
  ]) {
    const { sync, work } = setup();
    sync.state = state();
    sync.state.sources.cn.active = active;
    sync.state.sources.mx.allocation_rebuilding = rebuilding;
    sync.state.overall = { status: "running", completed: 14, total: 14 };
    let html, disabled;
    const node = { querySelector: () => ({}) };
    work.host.querySelector = () => node;
    work.syncButton = { prop: (_key, value) => { disabled = value; } };
    sync.dialog = { is_visible: true, fields_dict: { content: { $wrapper: { html: value => { html = value; } } } }, hide() {} };
    sync.render();
    assert(node.innerHTML.includes(text));
    assert(html.includes(text));
    assert(html.includes("14 of 14 templates updated successfully"));
    assert.equal(disabled, true);
    if (!active) assert(!html.includes("Synchronizing OA approvals…"));
    sync.stop();
  }
  assert.equal(label({configured: true, allocation_rebuilding: true}), "Recalculating local mappings…");
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

test("the dialog has one configuration and sync action with merged template progress", () => {
  const {sync} = setup();
  sync.state = state();
  sync.state.overall = {status: "partial_failure", completed: 1, total: 2};
  sync.state.sources.cn.status = "failed";
  sync.state.templates = [{name: "Shared approval", approval_type: "operating", status: "failed"}];
  let html;
  sync.dialog = {fields_dict: {content: {$wrapper: {html: value => {html = value;}}}}, hide() {}};
  sync.renderDialog();
  assert.equal((html.match(/data-sync-action="configure"/g) || []).length, 1);
  assert.equal((html.match(/data-sync-action="sync"/g) || []).length, 1);
  assert.doesNotMatch(html, /Budget and approval source|Expense approval source|data-source=/);
  assert.match(html, /Synchronization partially failed/);
  assert.match(html, /1 of 2 templates updated successfully/);
  assert.match(html, /data-sync-action="retry-failed"/);
  sync.stop();
});

test("retry failed items leaves a successful source alone", async () => {
  const {sync,work} = setup(), calls = [];
  sync.state = state();
  sync.state.sources.cn.status = "failed";
  work.call = async (method,args) => {
    if (method === "enqueue_sync") calls.push(args);
    return state();
  };
  await sync.synchronize(undefined,true);
  assert.deepEqual(calls,[{source:"cn",retry_failed:1}]);
  sync.stop();
});

test("configuration load errors retry configuration and survive status refresh", async () => {
  const {sync} = setup();
  sync.state = state();
  let html;
  sync.dialog = {is_visible:true,fields_dict:{content:{$wrapper:{html:value=>{html=value;}}}},hide(){}};
  sync.settingsError = "Synchronization settings could not be opened. Retry.";
  await sync.poll();
  assert.equal(sync.settingsError,"Synchronization settings could not be opened. Retry.");
  assert.match(html,/data-sync-action="configure">Retry/);
  sync.stop();
});

test("a reader cannot open unified configuration through a source-specific permission", async () => {
  const {sync} = setup();
  sync.state = state();
  sync.state.can_manage = false;
  global.frappe.require = () => {throw new Error("unauthorized editor load");};
  await sync.configure();
  sync.stop();
});
