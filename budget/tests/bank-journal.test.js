const {test} = require("node:test");
const assert = require("node:assert/strict");
const {columns, monthArgs, clientLink, rowMarkup, syncSelection, extractRefs, retryKey, scopeBooks} = require("../public/js/bank_journal.js");
test("defaults to the server-selected month and cannot submit an unbounded list request", () => {
    assert.deepEqual(monthArgs("B", "2026-11"), {book:"B",month:"2026-11"});
    for (const m of ["","all","2026-13"]) assert.throws(() => monthArgs("B",m));
});
test("nine columns preserve the approved order including independent multi-source native links", () => {
    assert.deepEqual(columns.map(([key])=>key),["date","category","level2","level3","project","summary","income","expense","balance"]);
    const link = id => 'https://aflow.dingtalk.com/dingtalk/mobile/homepage.htm?procInstId='+id;
    const html = rowMarkup({name:"X",date:"2026-11-20",refs:["11","22","33"],links:{"11":link("a"),"22":link("b")},summary:"<script>\n第二行",expense:"2.00",balance:"0.00"});
    assert.equal((html.match(/href="dingtalk:/g)||[]).length,2);
    assert.match(html,/33/); assert.match(html,/&lt;script&gt;/); assert.match(html,/0\.00/);
    assert.doesNotMatch(html,/已关联|待核对|<script>/);
    assert.equal(clientLink("https://outside.test/?procInstId=a"),"");
    assert.equal(clientLink("https://aflow.dingtalk.com/?procInstId=a&procInstId=b"),"");
});
test("manual references keep each full number and reject ambiguous scientific notation", () => {
    assert.deepEqual(extractRefs("202610012345678901234\n202610019876543210987"),["202610012345678901234","202610019876543210987"]);
    assert.throws(()=>extractRefs("2.0261E+20"));
});
test("synchronization requires a chosen source, actual payment date and positive payment amount", () => {
    assert.throws(()=>syncSelection([], "2026-11"));
    assert.throws(()=>syncSelection([{source_key:"a",date:"",expense:"2.00"}],"2026-11"));
    assert.throws(()=>syncSelection([{source_key:"a",date:"2026-11-04",expense:"0"}],"2026-11"));
    assert.deepEqual(syncSelection([{source_key:"a",date:"2026-11-04",expense:"2.00"}],"2026-11"),[{source_key:"a",date:"2026-11-04",expense:"2.00"}]);
});
test("retrying identical form data reuses a receipt identity without reusing it for changed inputs", () => {
    const state={};const first=retryKey(state,{x:1});assert.ok(first);
    assert.equal(retryKey(state,{x:1}),first);assert.notEqual(retryKey(state,{x:2}),first);
});

test("companies with the same bank label retain independent receipt and import targets", () => {
    const books=[{name:"A1",company:"A",account:"基本户"},{name:"B1",company:"B",account:"基本户"}];
    assert.deepEqual(scopeBooks(books,"A").map(x=>x.name),["A1"]);
    assert.deepEqual(scopeBooks(books,"B").map(x=>x.name),["B1"]);
    assert.deepEqual(scopeBooks(books,"C"),[]);
});
test("imported movements have a visible row editor with a calculated balance and an explicit read-only state", () => {
    const row = {name:"ROW-1",date:"2026-11-16",source:"Import",summary:"原始摘要",expense:"20.15",balance:"79.85",refs:["202611161336000427210"]};
    const display = rowMarkup(row, {canEdit:true});
    assert.match(display, /data-edit="ROW-1"[^>]*>编辑<\/button>/);
    assert.doesNotMatch(rowMarkup(row, {canEdit:false}), /data-edit=/);
    const edit = rowMarkup(row, {canEdit:true, editing:true});
    for (const name of ["date","income","expense","summary","refs"]) assert.match(edit,new RegExp('name="'+name+'"'));
    assert.match(edit, /data-act="save-edit"[^>]*>保存<\/button>/);
    assert.match(edit, /data-act="cancel-edit"[^>]*>取消<\/button>/);
    assert.doesNotMatch(edit, /name="balance"/);
});
test("the Desk asset loader receives a CSS path and manages its own version parameter", () => {
    const {install}=require("../public/js/bank_journal.js");
    const savedFrappe=globalThis.frappe, savedLocation=globalThis.location;
    const paths=[];
    globalThis.frappe={require:asset=>paths.push(asset)};
    globalThis.location=new URL("https://erp.example.invalid/desk/china-cash-journal");
    try {
        const page={};
        install(page);
        page.on_page_load({});
        assert.deepEqual(paths,["/assets/budget/css/bank_journal.css"]);
    } finally {
        globalThis.frappe=savedFrappe;
        globalThis.location=savedLocation;
    }
});
