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
test("bank import opens a compact editor and keeps only the active mapping fields collapsed", () => {
    const {importTable, importMapping}=require("../public/js/bank_journal.js");
    const result={headers:["日期","摘要","收入","支出","余额","流水号","部门"],mapping_used:{date:0,summary:1,income:2,expense:3,balance:4,bank_ref:5,dept:6}};
    const mapped=importMapping(result, result.mapping_used);
    assert.match(mapped,/<details[^>]*><summary>列对应/);
    assert.doesNotMatch(mapped,/<details[^>]*\bopen|归属部门|银行流水号|收支方向/);
    const signed=importMapping(result,{date:0,amount:2,direction:3});
    assert.match(signed,/data-map="amount"/);
    assert.doesNotMatch(signed,/data-map="income"/);
    const table=importTable([{source_row:8,date:"坏日期",summary:'来源 <tag>\n第二行',income:"0.00"}], [{row:8,field:"date",message:"日期无效"}]);
    for (const key of ["date","summary","income","expense","balance"]) assert.match(table,new RegExp('data-field="'+key+'"'));
    assert.match(table,/aria-invalid="true"/);
    assert.match(table,/&lt;tag&gt;\n第二行/);
    assert.match(table,/value="0\.00"/);
});
test("DingTalk preview starts with importable rows and keeps read-only groups separate", () => {
    const {syncPreview}=require("../public/js/bank_journal.js");
    const markup=syncPreview([
        {source_key:"recorded",selectable:false,already_recorded:true,summary:"已有项目",application_amount:"15.50",reason:"重复说明"},
        {source_key:"ready",selectable:true,summary:"可导入项目",application_date:"2026-11-10",application_amount:"23.45",reason:"请核实本账户",date:"2026-11-11",expense:"22.00"},
        {source_key:"review",selectable:false,summary:"单号异常项目",reason:"原单编号对应多个审批实例。"},
    ],"2026-11");
    assert.match(markup, /data-sync-tab="ready" aria-pressed="true"/);
    const [ready, existing, review]=markup.split(/<section data-sync-panel="[^"]+"[^>]*>/).slice(1);
    assert.match(ready,/可导入项目/);
    assert.match(ready,/data-pick="1"/);
    assert.match(ready,/name="date-1"[^>]*value="2026-11-11"/);
    assert.match(ready,/name="expense-1"[^>]*value="22.00"/);
    assert.doesNotMatch(ready,/已有项目|单号异常项目|请核实本账户/);
    assert.match(existing,/已有项目/);
    assert.doesNotMatch(existing,/<input|重复说明/);
    assert.match(review,/单号异常项目.*原单编号对应多个审批实例。/);
    assert.doesNotMatch(review,/<input/);
    assert.match(markup, /data-sync-panel="existing" hidden/);
    assert.doesNotMatch(syncPreview([],"2026-11"),/data-sync-tab="review"/);
});
test("DingTalk inputs default to the cashier date and application amount", () => {
    const {syncPreview}=require("../public/js/bank_journal.js");
    const markup=syncPreview([
        {source_key:"cashier",selectable:true,application_date:"2026-11-12",approved_date:"2026-12-01",
            cashier_date:"2026-11-24",application_amount:"260.00",date:null,expense:null},
        {source_key:"undated",selectable:true,application_date:"2026-11-16",approved_date:"2026-11-24",
            cashier_date:null,application_amount:"22.00",date:null,expense:null},
        {source_key:"registered",selectable:true,cashier_date:"2026-11-24",application_amount:"40.00",
            date:"2026-11-25",expense:"35.00"},
    ],"2026-11");
    assert.match(markup,/name="date-0"[^>]*value="2026-11-24"/);
    assert.match(markup,/name="expense-0"[^>]*value="260.00"/);
    assert.match(markup,/name="date-1"[^>]*value=""/);
    assert.match(markup,/name="expense-1"[^>]*value="22.00"/);
    assert.match(markup,/name="date-2"[^>]*value="2026-11-25"/);
    assert.match(markup,/name="expense-2"[^>]*value="35.00"/);
    assert.doesNotMatch(markup,/\bchecked\b/);
});

test("a company without a bank book can open the common import preview", () => {
    const {Workbench}=require("../public/js/bank_journal.js");
    const app=Object.create(Workbench.prototype);
    Object.assign(app,{el:{innerHTML:"",querySelector:()=>({})},opts:{can_create:true},
        books:[],companies:["示例公司"],company:"示例公司",month:"2026-11",book:"",data:null});
    app.render();
    assert.match(app.el.innerHTML,/data-act="import-book"[^>]*>导入账簿/);
    let opened=0;
    app.importDialog=()=>{opened++;};
    app.click({target:{closest:selector=>selector==="[data-act]"?{dataset:{act:"import-book"}}:null}});
    assert.equal(opened,1);
    app.opts.can_create=false;
    app.render();
    assert.doesNotMatch(app.el.innerHTML,/data-act="import-book"/);
    app.opts.can_create=true;
    app.company="";
    app.render();
    assert.doesNotMatch(app.el.innerHTML,/data-act="import-book"/);
});

test("selected books offer explicit append and date-range replacement in the same preview", () => {
    const {Workbench, importModeFields, replacementNotice}=require("../public/js/bank_journal.js");
    const app=Object.assign(Object.create(Workbench.prototype),{company:"A",book:"B",opts:{can_create:true},data:{},importDialog:newBook=>assert.equal(newBook,false)});
    app.click({target:{closest:selector=>selector==="[data-act]"?{dataset:{act:"import-book"}}:null}});
    const controls=importModeFields(true);
    assert.match(controls,/value="append"/);assert.match(controls,/value="replace_range"/);
    assert.doesNotMatch(controls,/\bchecked\b/);
    assert.doesNotMatch(importModeFields(false),/value="replace_range"/);
    const info=replacementNotice({start:"2026-11-05",end:"2026-11-20",old_count:3,new_count:2,
        old_closing:"12.00",new_closing:"15.00",difference:"3.00"});
    for(const value of ["2026-11-05","2026-11-20","3 笔","2 笔","12.00","15.00"])assert.ok(info.includes(value));
});

test("switching date layouts preserves other AI column choices and drops the incompatible date columns", () => {
    const {importMapping,switchImportLayout}=require("../public/js/bank_journal.js");
    const model={year:0,month:1,day:2,income:3,expense:4,summary:5,remark:6};
    const preview={headers:["Año","Mes","Día","Abono","Cargo","Concepto","Nota"],mapping_used:model};
    const html=importMapping(preview,model);
    for(const [key,index] of [["year",0],["month",1],["day",2],["remark",6]]) {
        assert.match(html,new RegExp('data-map="'+key+'"[^>]*>[^<]*(?:(?!</select>)[\\s\\S])*?value="'+index+'" selected'));
    }
    assert.doesNotMatch(html,/data-map="date"/);
    const combined=switchImportLayout(model,"date","combined");
    assert.deepEqual(combined,{income:3,expense:4,summary:5,remark:6});
    combined.date=0;
    assert.deepEqual(switchImportLayout(combined,"date","parts"),{income:3,expense:4,summary:5,remark:6});
    assert.deepEqual(switchImportLayout(model,"amount","signed"),{year:0,month:1,day:2,summary:5,remark:6});
    assert.equal(model.year,0);
});

test("exclusions carry original row numbers independently of page order and preserve edits when restored", () => {
    const {importReviewArgs,importTable}=require("../public/js/bank_journal.js");
    const edits={8:{summary:"核对"},274:{income:"2.00",expense:""}};
    const excluded=new Set([275,274]);
    let args=importReviewArgs(edits,excluded);
    assert.deepEqual(JSON.parse(args.excluded_rows),[274,275]);
    assert.deepEqual(JSON.parse(args.edits),{8:{summary:"核对"}});
    excluded.delete(274);
    args=importReviewArgs(edits,excluded);
    assert.deepEqual(JSON.parse(args.edits),edits);
    const rows=[{source_row:273,summary:"正常"},{source_row:274,excludable:true},
        {source_row:275,excludable:true,excluded:true,remark:"Nota <1>"}];
    const html=importTable(rows,[]);
    assert.doesNotMatch(html,/data-exclude-row="273"/);
    assert.match(html,/data-exclude-row="274"[^>]*>排除/);
    assert.match(html,/data-exclude-row="275"[^>]*>恢复/);
    assert.match(html,/Nota &lt;1&gt;/);
});

test("book selection changes ledger and range units and limits Chinese DingTalk payments to CNY books", () => {
    const {Workbench,replacementNotice}=require("../public/js/bank_journal.js");
    const books=[{name:"b1",company:"A",account:"Banco",currency:"MXN"},
        {name:"b2",company:"A",account:"同名跨境户",currency:"CNY"}];
    const app=Object.assign(Object.create(Workbench.prototype),{books,company:"A",companies:["A"],book:"b1",
        el:{querySelector:()=>({})},opts:{can_write:true,can_sync:true},month:"2026-11",data:{rows:[]}});
    app.render();
    assert.match(app.el.innerHTML,/本月收入（MXN）/);
    assert.doesNotMatch(app.el.innerHTML,/data-act="sync"/);
    const edit=rowMarkup({name:"R",balance:"123.00"},{editing:true,currency:"MXN"});
    assert.match(edit,/aria-label="收入（MXN）"/);
    const impact={start:"2023-01-01",end:"2023-03-31",old_count:1,new_count:2,old_closing:"1.00",new_closing:"2.00"};
    assert.match(replacementNotice(impact,"MXN"),/2\.00 MXN/);
    app.book="b2";
    app.render();
    assert.match(app.el.innerHTML,/本月收入（CNY）/);
    assert.match(app.el.innerHTML,/data-act="sync"/);
});

test("manual book creation requires a supported currency and submits the explicitly selected code",async t=>{
    const {Workbench}=require("../public/js/bank_journal.js");
    const fields={company:"A",account:"Banco",opening:"0",opening_date:"2023-01-01",currency:""};
    let submit,created,chooseCurrency;
    const openingLabel={};
    const old=globalThis.frappe;
    t.after(()=>{globalThis.frappe=old;});
    globalThis.frappe={call:async({args})=>{created=args;return {message:{book:"B"}};}};
    const app=Object.assign(Object.create(Workbench.prototype),{company:"A",month:"2026-11",opts:{currencies:["CNY","MXN"]},
        openDialog(_title,html){
            assert.match(html,/<select name="currency"[^>]*>/);
            assert.match(html,/<option value="MXN">MXN<\/option>/);
            assert.doesNotMatch(html,/<option value="USD"/);
            return {d:{hide(){}},el:{querySelector:s=>s==="[data-opening-label]"?openingLabel:
                {value:fields[s.match(/name="([^"]+)"/)[1]],addEventListener(_name,fn){chooseCurrency=fn;}}},action:(_label,fn)=>{submit=fn;}};
        },initialize:async()=>{}});
    app.bookDialog();
    await assert.rejects(submit,/币种/);
    fields.currency="MXN";
    chooseCurrency({target:{value:"MXN"}});
    assert.equal(openingLabel.textContent,"期初余额（MXN）");
    await submit();
    assert.deepEqual(created,fields);
});

test("creating a movement submits the selected book currency with its numeric amount",async t=>{
    const {Workbench}=require("../public/js/bank_journal.js");
    const old=globalThis.frappe;
    t.after(()=>{globalThis.frappe=old;});
    let payload,submit;
    globalThis.frappe={datetime:{get_today:()=>"2026-11-27"},call:async({args})=>{payload=args;return {message:{}};}};
    const values={date:"2026-11-27",expense:"8.00",summary:"收费"};
    const app=Object.assign(Object.create(Workbench.prototype),{book:"B",books:[{name:"B",currency:"MXN"}],
        opts:{can_create:true},month:"2026-11",data:{version:3},async load(){},
        openDialog(_title,html){
            assert.match(html,/支出（MXN）/);
            return {d:{hide(){}},el:{querySelector(s){
                const match=s.match(/name="([^"]+)"/);
                return match?{value:values[match[1]]||""}:null;
            }},action:(_action,fn)=>{submit=fn;}};
        }});
    app.rowDialog(null);
    await submit();
    assert.equal(payload.row.currency,"MXN");
    assert.equal(payload.row.expense,"8.00");
    assert.equal(payload.book,"B");
});

function importDialogFixture(t, reply, newBook=true) {
    const {Workbench}=require("../public/js/bank_journal.js");
    const classList={add(){},toggle(){}};
    const region=()=>({classList,innerHTML:"",querySelector:()=>null,replaceChildren(){this.innerHTML="";}});
    const fields=Object.fromEntries(["account","currency","opening","opening_date"].map(name=>[name,{
        name,value:"",setAttribute(){},matches:selector=>selector.includes(name==="currency"?"select":"input"),
    }]));
    const file={files:[]}, mapping=region(), result=region(), settings=region();
    const issues=region(),openingLabel=region(),mode={value:"append"};
    const modes={addEventListener(_event,handler){this.change=handler;}};
    settings.querySelectorAll=selector=>Object.values(fields).filter(field=>field.matches(selector));
    settings.querySelector=s=>s.includes("issues")?issues:s==="[data-opening-label]"?openingLabel:
        fields[s.match(/name="([^"]+)"/)?.[1]] || region();
    const el={classList,closest:()=>null,querySelectorAll:()=>[],querySelector(s){
        if(s.includes("statement"))return file;
        if(s.includes("book-settings"))return newBook?settings:null;
        if(s.includes("mapping"))return mapping;
        if(s.includes("data-result"))return result;
        if(s.includes("import-mode")&&s.includes("checked"))return mode.value?mode:null;
        if(s===".bj-import-modes")return newBook?null:modes;
        return null;
    }};
    const view={calls:[],fields,result,settings,openingLabel,
        chooseFile(name="estado.xlsx",size=100){file.files=name?[{name,size}]:[];return file.onchange();},
        pickMode(value){mode.value=value;return modes.change();},
        map(key,index){mapping.onchange({target:{value:String(index),dataset:{map:key},matches:s=>s==="[data-map]"}});},
    };
    const primary={addClass(){view.primaryHidden=true;},removeClass(){view.primaryHidden=false;},
        toggleClass(_name,hidden){view.primaryHidden=hidden;}};
    const old={frappe:globalThis.frappe,FileReader:globalThis.FileReader};
    t.after(()=>Object.assign(globalThis,old));
    globalThis.FileReader=class {readAsDataURL(){this.result="data:application/octet-stream;base64,WA==";this.onload();}};
    globalThis.frappe={call:async({method,args})=>{
        const name=method.split(".").at(-1);
        view.calls.push({name,args});
        return {message:await reply(name,args)};
    },show_alert(){}};
    const app=Object.assign(Object.create(Workbench.prototype),{company:"A",book:newBook?"":"B",month:"2026-11",
        books:[{name:"B",company:"A",account:"Banco",currency:"MXN"}],opts:{currencies:["CNY","MXN","USD"]},
        openDialog(_title,html){
            view.html=html;settings.hidden=/<div[^>]*data-book-settings[^>]* hidden[^>]*>/.test(html);
            let busy=false;
            return {d:{hide(){},get_primary_btn:()=>primary},el,check(){},error(text){view.error=text;},
                action:(label,run)=>{
                    view.label=label;view.primaryHidden=false;
                    view.submit=async()=>{
                        if(busy)return;
                        busy=true;
                        try{await run();}finally{busy=false;}
                        await new Promise(resolve=>setImmediate(resolve));
                    };
                }};
        },
        async initialize(){},async load(){}});
    app.importDialog(newBook);
    view.edit=(name,value)=>{
        const target=fields[name];
        target.value=value;
        (name==="currency"?settings.onchange:settings.oninput)({target});
    };
    view.sourceRow=(number,values)=>{
        const tr={dataset:{sourceRow:String(number)},classList};
        const cells=Object.fromEntries(Object.entries(values).map(([field,value])=>[field,{
            dataset:{field},value,classList,disabled:false,closest:()=>tr,matches:s=>s==="[data-field]",
        }]));
        tr.querySelectorAll=()=>Object.values(cells);
        tr.querySelector=s=>cells[s.match(/data-field="([^"]+)"/)[1]];
        const toggle={dataset:{excludeRow:String(number)},closest:s=>s==="[data-exclude-row]"?toggle:tr,setAttribute(){}};
        return {cells,edit(field,value){cells[field].value=value;result.oninput({target:cells[field]});},
            toggle(){result.onclick({target:toggle});}};
    };
    view.page=next=>{
        return result.onclick({target:{closest:s=>s==="[data-review-offset]"?{dataset:{reviewOffset:String(next)}}:null}}) || view.pending;
    };
    return view;
}

const bankDefaults={account:"识别账户",opening:"120.00",opening_date:"2023-01-01",currency:"MXN"};
const filePreview={review_rows:[{source_row:5,date:"2023-01-01",income:"5.00"}],review_total:1,row_count:1,
    mapping_used:{year:0,month:1,day:2,income:3,balance:4},header_row:4,headers:["年","月","日","收入","余额"],
    version:0,currency:"MXN",book_settings:bankDefaults,excluded_source_rows:[],preview_id:"verified"};

test("choosing a file reveals four editable defaults and its first signed preview can be confirmed directly",async t=>{
    const view=importDialogFixture(t,method=>method==="apply_import"?{book:"B",created:270}:
        {...filePreview,excluded_source_rows:[274,275],excluded_count:2,review_total:272,row_count:270});
    assert.ok(view.html.indexOf('name="statement"')<view.html.indexOf("data-book-settings"));
    assert.equal(view.settings.hidden,true);
    assert.equal(view.primaryHidden,true);
    await view.chooseFile();
    assert.deepEqual(view.calls.map(x=>x.name),["preview_import"]);
    const initial=view.calls[0].args;
    assert.equal(initial.company,"A");
    assert.equal(initial.suggest_book_defaults,1);
    assert.equal(initial.auto_exclude,1);
    assert.ok(!initial.currency);
    assert.ok(!("excluded_rows" in initial));
    assert.equal(view.settings.hidden,false);
    for(const [name,value] of Object.entries(bankDefaults))assert.equal(view.fields[name].value,value);
    assert.equal(view.openingLabel.textContent,"期初余额（MXN）");
    assert.match(view.result.innerHTML,/收入（MXN）/);
    assert.match(view.result.innerHTML,/待导入 270 笔[^]*已排除 2 行/);
    assert.equal(view.label,"建账并导入");
    await view.submit();
    assert.deepEqual(view.calls.map(x=>x.name),["preview_import","apply_import"]);
    const args=view.calls[1].args;
    for(const [name,value] of Object.entries(bankDefaults))assert.equal(args[name],value);
    assert.deepEqual(args.mapping,filePreview.mapping_used);
    assert.equal(args.preview_id,filePreview.preview_id);
    assert.equal(args.header_row,4);
    assert.deepEqual(JSON.parse(args.excluded_rows),[274,275]);
    for(const key of ["suggest_book_defaults","auto_exclude","month"])assert.ok(!(key in args));
});

test("incomplete book settings are editable and typing during validation keeps the newer account and amount",async t=>{
    let delay,resolvePreview;
    const view=importDialogFixture(t,(method,args)=>{
        if(method==="apply_import")return {book:"B",created:1};
        if(delay)return new Promise(resolve=>{resolvePreview=resolve;});
        const currency=args.currency || "";
        return {...filePreview,currency,preview_id:currency?"fresh-token":"",
            book_settings:{...bankDefaults,...(args.account?{account:args.account}:{}),opening:args.opening||bankDefaults.opening,currency},
            book_issues:currency?[]:[{field:"currency",message:"请选择账簿币种"}],
            opening_adjustment:args.opening?{file_opening:"120.00",book_opening:args.opening,difference:"5.00",closing:"130.00"}:null};
    });
    await view.chooseFile();
    assert.equal(view.fields.currency.value,"");
    assert.doesNotMatch(view.label,/建账并导入/);
    view.edit("currency","MXN");
    view.edit("opening","125.00");
    assert.equal(view.openingLabel.textContent,"期初余额（MXN）");
    delay=true;
    const pending=view.submit();
    await Promise.resolve();
    view.edit("account","最终账户");
    resolvePreview({...filePreview,book_settings:{account:"过时账户",opening:"1.00"},preview_id:"stale-token"});
    await pending;
    assert.equal(view.fields.account.value,"最终账户");
    assert.equal(view.fields.opening.value,"125.00");
    assert.doesNotMatch(view.label,/建账并导入/);
    delay=false;
    await view.submit();
    assert.match(view.label,/建账并导入/);
    assert.match(view.result.innerHTML,/文件期初[^<]*120\.00/);
    assert.match(view.result.innerHTML,/账簿期末[^<]*130\.00 MXN/);
    await view.submit();
    const {name,args}=view.calls.at(-1);
    assert.equal(name,"apply_import");
    assert.equal(args.currency,"MXN");
    assert.equal(args.account,"最终账户");
    assert.equal(args.opening,"125.00");
    assert.equal(args.preview_id,"fresh-token");
    assert.deepEqual(JSON.parse(args.excluded_rows),[]);
});

test("selecting another file discards old edits and a late preview cannot replace the new file",async t=>{
    let resolveFirst;
    const view=importDialogFixture(t,(method,args)=>{
        if(method==="apply_import")return {book:"B",created:1};
        if(args.filename==="first.csv" && args.account==="手工账户")return new Promise(resolve=>{resolveFirst=resolve;});
        return {...filePreview,preview_id:args.filename,book_settings:{...bankDefaults,account:args.filename},
            excluded_source_rows:args.filename==="first.csv"?[275]:[]};
    });
    await view.chooseFile("first.csv");
    view.edit("account","手工账户");
    view.sourceRow(5,{income:"5.00",expense:""}).edit("income","7.00");
    const pending=view.submit();
    await Promise.resolve();
    await view.chooseFile("second.csv");
    assert.equal(view.fields.account.value,"second.csv");
    const args=view.calls.at(-1).args;
    assert.ok(!args.account);
    assert.deepEqual(JSON.parse(args.edits),{});
    assert.equal(args.auto_exclude,1);
    await view.submit();
    assert.equal(view.calls.at(-1).name,"apply_import");
    resolveFirst({...filePreview,preview_id:"old"});
    await pending;
    assert.equal(view.fields.account.value,"second.csv");
    assert.equal(view.calls.at(-1).args.filename,"second.csv");
    assert.equal(view.calls.at(-1).args.preview_id,"second.csv");
    assert.deepEqual(JSON.parse(view.calls.at(-1).args.excluded_rows),[]);
});

test("a signed replacement must show its range impact before confirmation",async t=>{
    const view=importDialogFixture(t,()=>filePreview,false);
    await view.pickMode("replace_range");
    await view.chooseFile();
    assert.doesNotMatch(view.label,/确认/);
});

test("manual currency survives remapping while the server can still propose defaults for the other fields",async t=>{
    const view=importDialogFixture(t,(_method,args)=>({...filePreview,currency:args.currency||"MXN"}));
    await view.chooseFile();
    view.edit("currency","USD");
    view.map("summary",2);
    await view.submit();
    const {args}=view.calls.at(-1);
    assert.equal(args.currency,"USD");
    assert.equal(args.suggest_book_defaults,1);
    assert.ok(!args.account);
});

test("renaming a new book refreshes its suggested currency before the next confirmation",async t=>{
    const view=importDialogFixture(t,(method,args)=>{
        if(method==="apply_import")return {book:"B",created:1};
        const account=args.account ?? "普通账户";
        const currency=args.currency ?? (account==="BBVA"?"MXN":"CNY");
        return {...filePreview,currency,book_settings:{...bankDefaults,account,currency},preview_id:account+"-"+currency};
    });
    await view.chooseFile();
    assert.equal(view.fields.currency.value,"CNY");
    view.edit("account","BBVA");
    await view.submit();
    const reviewed=view.calls.at(-1).args;
    assert.equal(reviewed.suggest_book_defaults,1);
    assert.equal(reviewed.account,"BBVA");
    for(const name of ["currency","opening_date","opening","auto_exclude"])assert.ok(!(name in reviewed));
    assert.equal(view.fields.currency.value,"MXN");
    assert.equal(view.openingLabel.textContent,"期初余额（MXN）");
    assert.equal(view.label,"建账并导入");
    await view.submit();
    assert.deepEqual(view.calls.map(({name})=>name),["preview_import","preview_import","apply_import"]);
    const applied=view.calls.at(-1).args;
    assert.equal(applied.preview_id,"BBVA-MXN");
    for(const [name,value] of Object.entries({...bankDefaults,account:"BBVA"}))assert.equal(applied[name],value);
    assert.ok(!("suggest_book_defaults" in applied));
});

test("correcting the first movement refreshes inferred date and opening, while explicit book values survive paging",async t=>{
    const original={date:"2023-01-05",income:"5.00",expense:"",balance:"125.00"};
    const view=importDialogFixture(t,(method,args)=>{
        if(method==="apply_import")return {book:"B",created:1};
        const first={...original,...JSON.parse(args.edits)[5]};
        const opening=args.opening ?? (Number(first.balance)-Number(first.income)).toFixed(2);
        const opening_date=args.opening_date ?? first.date;
        return {...filePreview,review_rows:[{source_row:5,...first}],review_total:101,review_page_size:100,
            book_settings:{...bankDefaults,opening,opening_date},preview_id:opening_date+"-"+opening};
    });
    await view.chooseFile();
    assert.equal(view.fields.opening_date.value,"2023-01-05");
    assert.equal(view.fields.opening.value,"120.00");
    const first=view.sourceRow(5,original);
    first.edit("date","2022-12-20");
    first.edit("income","20.00");
    await view.submit();
    const inferred=view.calls.at(-1).args;
    for(const name of Object.keys(bankDefaults))assert.ok(!(name in inferred));
    assert.equal(view.fields.opening_date.value,"2022-12-20");
    assert.equal(view.fields.opening.value,"105.00");
    assert.equal(view.label,"建账并导入");
    view.edit("opening_date","2022-12-01");
    view.edit("opening","80.00");
    first.edit("date","2022-12-15");
    first.edit("income","25.00");
    await view.page(100);
    const page=view.calls.at(-1).args;
    assert.equal(page.suggest_book_defaults,1);
    assert.deepEqual([page.opening_date,page.opening],["2022-12-01","80.00"]);
    for(const name of ["account","currency","auto_exclude"])assert.ok(!(name in page));
    assert.deepEqual(JSON.parse(page.edits)[5],{date:"2022-12-15",income:"25.00",expense:""});
    assert.equal(view.fields.opening_date.value,"2022-12-01");
    assert.equal(view.fields.opening.value,"80.00");
    assert.equal(view.label,"建账并导入");
    await view.submit();
    assert.deepEqual(view.calls.map(({name})=>name),["preview_import","preview_import","preview_import","apply_import"]);
    const applied=view.calls.at(-1).args;
    assert.equal(applied.preview_id,"2022-12-01-80.00");
    for(const [name,value] of Object.entries({...bankDefaults,opening_date:"2022-12-01",opening:"80.00"})){
        assert.equal(applied[name],value);
    }
});

test("clearing the currency keeps the empty override and still requests defaults for the other fields",async t=>{
    const view=importDialogFixture(t,()=>filePreview);
    await view.chooseFile();
    view.edit("currency","");
    await view.submit();
    const reviewed=view.calls.at(-1).args;
    assert.equal(reviewed.suggest_book_defaults,1);
    assert.equal(reviewed.currency,"");
    assert.equal(view.fields.currency.value,"");
    assert.doesNotMatch(view.label,/建账并导入/);
});

test("a failed file read offers a retry and clearing the chooser removes the old action and defaults",async t=>{
    let fail=true;
    const view=importDialogFixture(t,()=>{
        if(fail)throw Error("网络中断");
        return filePreview;
    });
    await view.chooseFile();
    assert.match(view.error,/网络中断/);
    assert.match(view.label,/重试/);
    assert.equal(view.settings.hidden,true);
    fail=false;
    await view.submit();
    assert.equal(view.label,"建账并导入");
    assert.equal(view.settings.hidden,false);
    await view.chooseFile("");
    assert.equal(view.settings.hidden,true);
    assert.equal(view.primaryHidden,true);
    assert.equal(view.result.innerHTML,"");
});

test("appending uses the existing book currency and does not send new-book suggestions",async t=>{
    const view=importDialogFixture(t,method=>method==="apply_import"?{created:1,matched:0,skipped:0}:filePreview,false);
    await view.chooseFile();
    assert.equal(view.label,"确认新增流水");
    await view.submit();
    assert.deepEqual(view.calls.map(x=>x.name),["preview_import","apply_import"]);
    for(const {args} of view.calls){
        assert.equal(args.book,"B");
        assert.equal(args.currency,"MXN");
        assert.equal(args.mode,"append");
        for(const key of ["company","account","opening","suggest_book_defaults","month"])assert.ok(!(key in args));
    }
});

test("files wait for an explicit mode when replacement is available and changing mode invalidates its signature",async t=>{
    const impact={start:"2023-01-01",end:"2023-01-31",old_count:10,new_count:1,old_closing:"4.00",new_closing:"5.00"};
    const view=importDialogFixture(t,(method,args)=>method==="apply_import"?{created:1,matched:0,skipped:0}:
        {...filePreview,preview_id:args.mode,impact:args.mode==="replace_range"?impact:null},false);
    await view.pickMode("");
    await view.chooseFile();
    assert.equal(view.calls.length,0);
    await view.pickMode("replace_range");
    assert.equal(view.calls.at(-1).args.mode,"replace_range");
    assert.match(view.result.innerHTML,/原有 10 笔将替换为 1 笔/);
    assert.equal(view.label,"确认覆盖这段日期");
    await view.pickMode("append");
    assert.equal(view.label,"确认新增流水");
    await view.submit();
    const {name,args}=view.calls.at(-1);
    assert.equal(name,"apply_import");
    assert.equal(args.mode,"append");
    assert.equal(args.preview_id,"append");
});

test("blank rows from later pages can be restored, corrected and signed without reapplying automatic exclusions",async t=>{
    const amountless={date:"2023-02-01",summary:"原文",income:"",expense:"",balance:"5.00"};
    const view=importDialogFixture(t,(method,args)=>{
        const edits=JSON.parse(args.edits);
        const excluded=args.auto_exclude?[274,275]:JSON.parse(args.excluded_rows);
        assert.ok(excluded.every(n=>!edits[n]));
        if(method==="apply_import")return {created:271};
        const ready=[274,275].every(n=>excluded.includes(n)||edits[n]?.income);
        const rows=args.review_offset===200?[274,275].map(source_row=>({source_row,...amountless,
            excludable:true,excluded:excluded.includes(source_row),...edits[source_row]})):filePreview.review_rows;
        return {...filePreview,preview_id:ready?"verified-rows":"",review_rows:rows,review_total:272,
            row_count:270+(edits[274]?.income?1:0),review_page_size:100,excluded_count:excluded.length,
            ...(args.auto_exclude?{excluded_source_rows:excluded}:{}),error_count:ready?0:1};
    },false);
    await view.chooseFile();
    assert.equal(view.label,"确认新增流水");
    await view.page(200);
    assert.deepEqual(JSON.parse(view.calls.at(-1).args.excluded_rows),[274,275]);
    assert.match(view.result.innerHTML,/data-exclude-row="274"[^>]*>恢复/);
    const item=view.sourceRow(274,amountless);
    item.toggle();
    assert.equal(item.cells.income.disabled,false);
    await view.submit();
    assert.deepEqual(JSON.parse(view.calls.at(-1).args.excluded_rows),[275]);
    assert.doesNotMatch(view.label,/确认新增/);
    item.edit("summary","人工备注");
    item.edit("income","2.00");
    item.toggle();
    assert.equal(item.cells.summary.value,"原文");
    assert.equal(item.cells.income.disabled,true);
    item.toggle();
    assert.equal(item.cells.summary.value,"人工备注");
    assert.equal(item.cells.income.value,"2.00");
    await view.page(0);
    assert.equal(view.label,"确认新增流水");
    assert.match(view.result.innerHTML,/待导入 271 笔[^]*已排除 1 行/);
    await view.submit();
    const {name,args}=view.calls.at(-1);
    assert.equal(name,"apply_import");
    assert.deepEqual(JSON.parse(args.excluded_rows),[275]);
    assert.deepEqual(JSON.parse(args.edits),{274:{summary:"人工备注",income:"2.00",expense:""}});
    assert.equal(view.calls.filter(({args})=>args.auto_exclude).length,1);
});

test("mapping changes reconsider safe rows while keeping a restored row and its pending corrections",async t=>{
    const amountless={date:"2023-02-01",summary:"原文",income:"",expense:"",balance:"5.00"};
    const view=importDialogFixture(t,(_method,args)=>{
        const edits=JSON.parse(args.edits);
        const excluded=args.auto_exclude?[274,275,276].filter(n=>!edits[n]):JSON.parse(args.excluded_rows);
        return {...filePreview,mapping_used:args.mapping||{date:0,income:1},excluded_source_rows:excluded,
            review_rows:[274,275,276].map(source_row=>({source_row,...amountless,excludable:true,excluded:excluded.includes(source_row)}))};
    },false);
    await view.chooseFile();
    const corrected=view.sourceRow(274,amountless);
    corrected.toggle();
    corrected.edit("income","1.00");
    view.sourceRow(275,amountless).toggle();
    view.map("summary",2);
    await view.submit();
    const reads=view.calls.slice(1).map(call=>call.args);
    assert.equal(reads.filter(args=>args.auto_exclude).length,1);
    assert.deepEqual(JSON.parse(reads.at(-1).edits),{274:{income:"1.00",expense:""}});
    assert.deepEqual(JSON.parse(reads.at(-1).excluded_rows),[276]);
    assert.equal(reads.at(-1).mapping.summary,2);
});
