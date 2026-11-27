const test = require("node:test");
const assert = require("node:assert/strict");
const {Model, Controller, key} = require("../public/js/expense_company_mappings.js");
const {esc} = require("../public/js/cn_approval_workbench.js");
global.__ = (text, args = []) => text.replace(/\{(\d+)\}/g, (_, index) => args[index]);
global.frappe = {utils: {escape_html: esc}, msgprint() {}};
const saved = {companies: ["CN", "MX", "MX 2"], expense_companies: ["MX", "MX 2"], corporations: ["corp-a", "corp-b"]};
const row = (source_name, extra = {}) => ({match_kind: "entity", corp_id: "", source_name, company: "MX", excluded: 0, reason: "", ...extra});
const candidate = (name, extra = {}) => row(name, {company: "", count: 2, ...extra});

function controller(rows = [], call) {
  const nodes = new Map(), listeners = new Map();
  const element = {ownerDocument: {activeElement: null}, contains: () => false,
    set innerHTML(value) { this.markup = value; nodes.clear(); }, get innerHTML() { return this.markup; },
    addEventListener(event, callback) { listeners.set(event, callback); },
    removeEventListener(event) { listeners.delete(event); },
    querySelector(selector) {
      if (!nodes.has(selector)) nodes.set(selector, {value: "", scrollTop: 0, scrollLeft: 0});
      return nodes.get(selector);
    }, listeners};
  const grid = {df: {data: rows}, refresh() {}};
  return new Controller({element, grid, saved, call, isCurrent: () => true, isSaving: () => false});
}

test("mapping identities preserve corporation and source domain, while only expense labels normalize whitespace", () => {
  assert.equal(key(row("A  B")), key(row("A B")));
  assert.notEqual(key(row("A  B", {match_kind: "organization"})), key(row("A B", {match_kind: "organization"})));
  assert.notEqual(key(row("A", {corp_id: "corp-a", match_kind: "organization"})), key(row("A", {corp_id: "corp-b", match_kind: "organization"})));
  assert.notEqual(key(row("A")), key(row("A", {match_kind: "department"})));
});

test("batch assignments validate the entire selection before mutating existing mappings", () => {
  const rows = [row("Expense"), row("Organization", {match_kind: "organization", corp_id: "corp-a", company: "CN"})];
  const before = structuredClone(rows), model = new Model(rows, saved);
  assert.throws(() => model.assign(rows.map(key), "Forbidden"), /every selected/);
  assert.deepEqual(rows, before);
  assert.throws(() => model.assign([key(rows[0]), "missing"], "MX"), /every selected/);
  model.assign(rows.map(key), "CN");
  assert(rows.every(item => item.company === "CN"));
  model.assign(rows.map(key), "MX 2");
  assert(model.items().every(item => item.dirty));
});

test("candidate recommendations do not assign companies and duplicate pages cannot duplicate mappings", () => {
  const rows = [], model = new Model(rows, saved), source = candidate("Alias", {suggestions: [{company: "MX", match: "similar"}]});
  model.candidates.set(key(source), source);
  assert.equal(rows.length, 0);
  assert.equal(model.items()[0].mapped, false);
  model.assign([key(source)], "MX");
  model.assign([key(source)], "MX 2");
  assert.equal(rows.length, 1);
  assert.equal(rows[0].source_name, "Alias");
  assert.equal(rows[0].company, "MX 2");
});


test("named sources can use Group in batch, inline and manual editing without adding a Company", () => {
  const targets={...saved,mapping_targets:[...saved.companies,"__expense_group__"]};
  const rows=[row("CEO",{match_kind:"organization",corp_id:"corp-a"}),row("PMO",{match_kind:"department"})];
  const model=new Model(rows,targets);
  model.assign(rows.map(key),"__expense_group__");
  assert(rows.every(item=>item.company==="__expense_group__"));
  assert(model.items().every(item=>item.mapped&&!item.group));
  model.put({match_kind:"entity",source_name:"Group costs",company:"__expense_group__"});
  assert.equal(rows.length,3);
  assert.deepEqual(saved.companies,["CN","MX","MX 2"]);
  model.remove(key(rows[0]));model.undo(key(row("CEO",{match_kind:"organization",corp_id:"corp-a"})));
  assert.equal(rows.find(item=>item.source_name==="CEO").company,"MX");
});

test("nameless source groups cannot partially apply a batch assignment", () => {
  for (const name of ["", "   ", null, undefined]) {
    const rows = [row("Existing")], before = structuredClone(rows), model = new Model(rows, saved);
    const valid = candidate("Unmapped"), missing = candidate(name, {count: 1370});
    for (const item of [valid, missing]) model.candidates.set(key(item), item);
    assert.throws(() => model.assign([key(rows[0]), key(valid), key(missing)], "CN"), /reported under Group automatically/);
    assert.deepEqual(rows, before);
    assert.throws(() => model.put({...missing, company: "CN"}), /source information/);
    assert.deepEqual(rows, before);
  }
});

test("select all skips nameless groups while preserving their counts and other draft changes", () => {
  const rows = [row("Existing", {company: "CN"})], view = controller(rows);
  const valid = candidate("Unmapped"), missing = candidate("", {count: 1370, suggestions: ["CN"]});
  for (const item of [valid, missing]) view.model.candidates.set(key(item), item);
  view.render();
  view.change({target: {dataset: {mappingRole: "all"}, checked: true}});
  assert.deepEqual([...view.selected], [key(valid)]);
  assert.equal(view.element.querySelector('[data-mapping-role="all"]').indeterminate, false);
  const item = view.model.items().find(item => item.key === key(missing));
  const html = view.rowHTML(item, 0, "");
  assert.match(html, /Assigned to Group automatically/);
  assert.doesNotMatch(html, /Source name missing|data-mapping-action="remove"/);
  assert.match(html, /1370 expense lines/);
  assert.match(html, /data-mapping-role="row"[^>]*disabled/);
  assert.doesNotMatch(html, /data-mapping-role="row-company"|data-mapping-action="suggest"/);
  view.model.assign(view.selected, "CN");
  assert.equal(rows.length, 2);
  assert.equal(rows.find(item => item.source_name === "Unmapped").company, "CN");
  assert.equal(view.model.items().find(item => item.key === key(missing)).mapped, true);
  assert.equal(view.model.items().find(item => item.key === key(missing)).dirty, false);
});

test("details for an automatic Group source explain reporting without opening a mapping editor", () => {
  const rows = [row("Existing")], before = structuredClone(rows), view = controller(rows);
  const missing = candidate("", {count: 1370});
  view.model.candidates.set(key(missing), missing);
  let message;
  const original = global.frappe.msgprint;
  try {
    global.frappe.msgprint = value => { message = value; };
    view.edit(view.model.items().find(item => item.key === key(missing)));
    assert.equal(message.title, "Group");
    assert.match(message.message, /reported under Group automatically/);
    assert.equal(view.editor, undefined);
    assert.deepEqual(rows, before);
  } finally { global.frappe.msgprint = original; }
});

test("missing source counts merge null, empty and whitespace groups without merging source domains", async () => {
  const view = controller([], async (_method, {kind}) => ({rows: kind === "entities" ? [
    candidate(null, {count: 1370}), candidate("", {count: "10"}), candidate("   ", {count: 5}),
    candidate(null, {match_kind: "department", count: 7}),
  ] : [candidate(null, {corp_id: "corp-a", count: 8}), candidate("", {corp_id: "corp-b", count: 9})], has_more: false}));
  await view.load();
  assert.equal(view.model.items().length, 4);
  for (const [source, count] of [
    [candidate(""), 1385], [candidate("", {match_kind: "department"}), 7],
    [candidate("", {match_kind: "organization", corp_id: "corp-a"}), 8],
    [candidate("", {match_kind: "organization", corp_id: "corp-b"}), 9],
  ]) assert.equal(view.model.candidates.get(key(source)).count, count);
  view.tab = "mapped"; view.render();
  assert.match(view.element.querySelector('[data-mapping-part="results"]').innerHTML, /1385 expense lines/);
  assert.equal(view.model.rows.length, 0);
});

test("Group mappings are searchable and filterable without entering the saved Company mappings", async () => {
  const rows=[row("Saved")],before=structuredClone(rows);
  const view=controller(rows,async(_method,{kind})=>({rows:kind==='entities'?[candidate('',{count:1380})]:[],has_more:false}));
  await view.load();
  assert.equal(view.filtered().length,0);
  view.tab='mapped';view.query='Group';view.render();
  assert.equal(view.visible.length,1);
  assert.equal(view.visible[0].group,true);
  view.query='';view.company='__expense_group__';view.render();
  assert.equal(view.visible.length,1);
  assert.equal(view.element.querySelector('[data-mapping-role="all"]').indeterminate,false);
  assert.deepEqual(rows,before);
});

test("overlapping candidate pages count each raw empty group once and retain staged assignments", async () => {
  const rows = [row("Existing")];
  const view = controller(rows, async (_method, {kind, start}) => ({rows: kind !== "entities" ? [] : start === 0
    ? [candidate(null, {count: 1370}), candidate("Pending")]
    : [candidate(null, {count: start === 50 ? 1370 : 1372}), candidate("", {count: 10}), candidate("", {count: 10})],
    has_more: kind === "entities" && start < 100}));
  await view.load();
  view.model.assign([key(rows[0])], "CN");
  view.model.assign([key(candidate("Pending"))], "MX 2");
  const staged = structuredClone(rows);
  await view.load();
  assert.equal(view.model.candidates.get(key(candidate(""))).count, 1380);
  await view.load();
  assert.equal(view.model.candidates.get(key(candidate(""))).count, 1382);
  assert.equal(view.model.candidates.size, 2);
  assert.equal(view.streams.entities.done, true);
  assert.deepEqual(rows, staged);
});

test("source refresh replaces empty-group counts and failed refresh retains the last loaded counts", async () => {
  let cycle = "initial";
  const rows = [row("Existing")];
  const view = controller(rows, async (_method, {kind}) => {
    if (kind === "entities" && cycle === "failed") throw Error("offline");
    return {rows: kind !== "entities" ? [] : cycle === "initial"
      ? [candidate(null, {count: 1370}), candidate("", {count: 10})]
      : [candidate(null, {count: 20})], has_more: false};
  });
  view.model.assign([key(rows[0])], "CN");
  const staged = structuredClone(rows);
  await view.load();
  assert.equal(view.model.candidates.get(key(candidate(""))).count, 1380);
  cycle = "failed"; await view.load(true);
  assert.equal(view.model.candidates.get(key(candidate(""))).count, 1380);
  assert.equal(view.streams.entities.error, true);
  cycle = "updated"; await view.load(true);
  assert.equal(view.model.candidates.get(key(candidate(""))).count, 20);
  assert.equal(view.streams.entities.error, false);
  assert.deepEqual(rows, staged);
});

test("all mapping kinds allow authorized Chinese companies in inline and batch controls", () => {
  const rows = [row("Entity"), row("Department", {match_kind: "department"}),
    row("Organization", {match_kind: "organization", corp_id: "corp-a"})];
  const view = controller(rows);
  view.tab = "mapped"; view.render();
  view.selected = new Set(rows.map(key)); view.render();
  const html = view.element.querySelector('[data-mapping-part="results"]').innerHTML;
  assert.equal((html.match(/value="CN"/g) || []).length, 4);
  for (const item of rows) view.model.assign([key(item)], "CN");
  assert(rows.every(item => item.company === "CN"));
  view.model.put(row("Manual", {match_kind: "department", company: "CN"}));
  assert.equal(rows.at(-1).company, "CN");
});

test("undo restores original mapping and exclusion metadata without touching unrelated drafts", () => {
  const initial = row("Excluded", {company: "", excluded: 1, reason: "Reviewed reason"});
  const rows = [initial, row("Other")], model = new Model(rows, saved);
  model.assign([key(initial)], "MX 2");
  model.assign([key(rows[1])], "MX 2");
  assert.equal(initial.excluded, 0);
  model.undo(key(initial));
  assert.deepEqual(rows.find(item => item.source_name === "Excluded"), row("Excluded", {company: "", excluded: 1, reason: "Reviewed reason"}));
  model.remove(key(initial));
  assert.equal(model.items().find(item => item.key === key(initial)).removed, true);
  model.undo(key(initial));
  assert.equal(rows.find(item => item.source_name === "Other").company, "MX 2");
});

test("manual mapping validation rejects duplicate identities and invalid exclusions", () => {
  const rows = [row("Name")], model = new Model(rows, saved);
  assert.throws(() => model.put(row(" Name ")), /already in/);
  assert.throws(() => model.put(row("New", {company: "Forbidden"})), /every selected/);
  assert.throws(() => model.put(row("New", {company: "", excluded: 1, reason: ""})), /reason/);
  assert.throws(() => model.put(row("New", {match_kind: "organization", corp_id: "unknown"})), /source information/);
  assert.equal(rows.length, 1);
});

test("source loading is bounded and independent; failed streams retain edits and retry their original page", async () => {
  const calls = []; let fail = true;
  const view = controller([row("Saved")], async (_method, args) => {
    calls.push(args);
    if (args.kind === "entities" && fail) throw Error("offline");
    return {rows: [args.kind === "entities" ? candidate("New") : candidate("Org", {corp_id: "corp-a"})], has_more: false};
  });
  view.model.assign([key(row("Saved"))], "MX 2");
  await view.load();
  assert.equal(calls.length, 2);
  assert.equal(view.streams.organizations.error, false);
  assert.equal(view.streams.entities.error, true);
  assert.equal(view.model.rows[0].company, "MX 2");
  assert.match(view.element.querySelector('[data-mapping-part="results"]').innerHTML, /Some sources could not be loaded/);
  fail = false; await view.load();
  assert.deepEqual(calls[2], {kind: "entities", start: 0});
  assert.equal(view.model.candidates.size, 2);
});

test("pagination and search never allow a batch to change hidden selections", () => {
  const rows = Array.from({length: 25}, (_, n) => row(`Source ${n}`));
  const view = controller(rows);
  view.tab = "mapped"; view.render();
  assert.equal(view.visible.length, 20);
  view.selected = new Set(rows.map(key)); view.render();
  assert.equal(view.selected.size, 20);
  view.page = 1; view.render();
  assert.equal(view.visible.length, 5);
  assert.equal(view.selected.size, 0);
  view.query = "Source 24"; view.resetView(); view.render();
  assert.equal(view.visible.length, 1);
  view.model.assign(view.visible.map(item => item.key), "MX 2");
  assert.equal(rows.filter(item => item.company === "MX 2").length, 1);
  assert.equal(rows.length, 25);
});

test("loading more sources preserves staged assignments and old source identities", async () => {
  const view = controller([], async (_method, args) => ({rows: args.kind === "entities" ? [candidate(args.start ? "Second" : "First")] : [], has_more: args.kind === "entities" && args.start === 0}));
  await view.load();
  view.model.assign([key(candidate("First"))], "MX");
  await view.load();
  assert.equal(view.model.candidates.size, 2);
  assert.equal(view.model.rows.length, 1);
  assert.equal(view.model.rows[0].company, "MX");
  assert.equal(view.streams.entities.next, 100);
});

test("closing the editor ignores both late successes and failures", async () => {
  const pending = [];
  const view = controller([], () => new Promise((resolve, reject) => pending.push({resolve, reject})));
  const loading = view.load();
  view.destroy();
  pending[0].resolve({rows: [candidate("Hidden")], has_more: false});
  pending[1].reject(Error("late"));
  await loading;
  assert.equal(view.model.candidates.size, 0);
  assert.equal(view.active, false);
});

test("untrusted names and object recommendations render safely without exposing object strings", () => {
  const view = controller([]);
  const source = candidate('<img src=x onerror="bad()">', {suggestions: [{company: "MX", match: "similar"}, {company: "CN", match: "similar"}, {company: "Forbidden", match: "similar"}]});
  view.model.candidates.set(key(source), source); view.render();
  const html = view.element.querySelector('[data-mapping-part="results"]').innerHTML;
  assert.match(html, /&lt;img/);
  assert.doesNotMatch(html, /<img|\[object Object\]/);
  assert.match(html, /data-mapping-action="suggest"/);
  assert.equal((html.match(/data-mapping-action="suggest"/g) || []).length, 2);
  assert.doesNotMatch(html, /Forbidden/);
});

test("IME search commits once and retains the input while candidate requests complete", async () => {
  const pending = [], view = controller([row("中文来源"), row("Other")], () => new Promise(resolve => pending.push(resolve)));
  view.tab = "mapped"; view.render();
  const search = view.element.querySelector('[data-mapping-role="search"]');
  search.dataset = {mappingRole: "search"};
  const dispatch = type => view.element.listeners.get(type)({target: search});
  dispatch("compositionstart");
  search.value = "zhong"; dispatch("input");
  assert.equal(view.query, "");
  assert.equal(view.visible.length, 2);
  const loading = view.load();
  pending.forEach(resolve => resolve({rows: [], has_more: false})); await loading;
  assert.equal(view.element.querySelector('[data-mapping-role="search"]'), search);
  assert.equal(search.value, "zhong");
  search.value = "中文"; dispatch("compositionend");
  assert.equal(view.query, "中文");
  assert.equal(view.visible.length, 1);
  assert.equal(view.visible[0].row.source_name, "中文来源");
  view.selected.add(view.visible[0].key);
  dispatch("input"); // Some browsers emit a final input after compositionend.
  assert.equal(view.selected.size, 1);
  assert.equal(view.element.querySelector('[data-mapping-role="search"]'), search);
  search.value = ""; dispatch("input");
  assert.equal(view.visible.length, 2);
  view.destroy();
  assert.equal(view.element.listeners.size, 0);
});

test("same-page selection retains both scroll axes; filters and pagination reset vertical position", () => {
  const view = controller(Array.from({length: 25}, (_, n) => row(`Source ${n}`)));
  view.tab = "mapped"; view.render();
  // Replacing the results DOM creates a fresh scrolling element.
  let table = {scrollTop: 600, scrollLeft: 120};
  const query = view.element.querySelector.bind(view.element);
  view.element.querySelector = selector => selector === ".em-mapping-table-wrap" ? table : query(selector);
  Object.defineProperty(query('[data-mapping-part="results"]'), "innerHTML", {
    set() { table = {scrollTop: 0, scrollLeft: 0}; },
  });
  view.change({target: {dataset: {mappingRole: "row", index: "13"}, checked: true}});
  assert.deepEqual(table, {scrollTop: 600, scrollLeft: 120});
  assert.equal(view.selected.size, 1);
  view.page = 1; view.render();
  assert.equal(table.scrollTop, 0);
  assert.equal(table.scrollLeft, 120);
  table.scrollTop = 200;
  view.query = "Source 24"; view.resetView(); view.render();
  assert.equal(table.scrollTop, 0);
  assert.equal(view.visible[0].row.source_name, "Source 24");
});

test("readonly source kinds show translated labels while retaining stable form values", () => {
  const previous = global.frappe.ui, translate = global.__;
  let fields;
  global.__ = value => ({"Source organization": "来源组织", "Expense-bearing entity": "费用承担主体", "Expense-bearing department": "费用承担部门"})[value] || value;
  global.CNApprovalSettings = {textOptions: values => values};
  global.frappe.ui = {Dialog: class { constructor(options) { fields = options.fields; } show() {} }};
  try {
    for (const [kind, label] of [["organization", "来源组织"], ["entity", "费用承担主体"], ["department", "费用承担部门"]]) {
      const view = controller([row("Saved", {match_kind: kind, corp_id: "corp-a"})]);
      view.edit(view.model.items()[0]);
      const field = fields.find(item => item.fieldname === "match_kind");
      assert.equal(field.read_only, true);
      assert.equal(field.default, kind);
      assert.equal(field.formatter(kind), label);
      assert.equal(field.formatter("<unknown>"), "&lt;unknown&gt;");
    }
  } finally { global.frappe.ui = previous; global.__ = translate; }
});

test("native manual editor lists authorized reporting targets without resolving Group as a Company Link", () => {
  const previous = global.frappe.ui;
  let field;
  global.CNApprovalSettings = {textOptions: values => values};
  global.frappe.ui = {Dialog: class {
    constructor(options) {
      field = options.fields.find(field => field.fieldname === "company");
    }
    get_value() { return "organization"; }
    show() {}
  }};
  try {
    const view = controller([row("Saved")]);
    view.edit(view.model.items()[0]);
    assert.equal(field.fieldtype, "Select");
    assert.deepEqual(field.options.map(option=>option.value), ["", ...saved.companies]);
    assert.equal(field.get_query, undefined);
  } finally { global.frappe.ui = previous; }
});
