const test = require("node:test");
const assert = require("node:assert/strict");
const editor = require("../public/js/expense_template_mapping.js");
const {esc} = require("../public/js/cn_approval_workbench.js");
const empty = () => ({headers:{},children:{},currency:{}});
function sample() {
  return {processors:["cn","mx"],sample_count:1,
    fields:[{id:"MoneyField_total",label:"Total",type:"MoneyField"},
      {id:"TableField_one",label:"Details",type:"TableField"}, {id:"TableField_two",label:"Other table",type:"TableField"}],
    headers:[{key:"amount",label:"Total amount",types:["MoneyField"]},
      {key:"allocation_table",label:"Allocation",types:["TableField"]}],
    table_roles:{allocation_table:[{key:"amount",label:"Detail amount",types:["MoneyField"]}]},
    tables:{TableField_one:[{id:"MoneyField_one",label:"One",type:"MoneyField"}],
      TableField_two:[{id:"MoneyField_two",label:"Two",type:"MoneyField"}]},
    defaults:{allocation_table:["TableField_one"]},fallbacks:{allocation_table:["TableField_one"]},
    samples:[{index:1,header_total:"100.00",currency:"CNY",detail_total:"100.00",detail_currency:"CNY",line_count:1,
      issues:[],lines:[{entity:"Department A",amount:"100.00",currency:"CNY"}]}]};
}
function runtime() {
  const state={dialogs:[],messages:[],alerts:[],calls:[],applied:[],current:true,data:sample()};
  global.__ = (text,args=[])=>text.replace(/\{(\d+)\}/g,(_,i)=>args[i]??`{${i}}`);
  global.frappe={utils:{escape_html:esc},msgprint:message=>state.messages.push(message),show_alert:alert=>state.alerts.push(alert),
    ui:{Dialog:class {
      constructor(config) {
        this.config=config;state.dialogs.push(this);
        this.fields_dict=Object.fromEntries(config.fields.map(field=>[field.fieldname,{df:field,$wrapper:{
          html(value){this.value=value;},text(value){this.value=value;},on(_event,_selector,handler){this.handler=handler;}
        }}]));
      }
      show(){this.is_visible=true;}
      hide(){this.is_visible=false;this.config.onhide?.();}
    }}};
  state.options={args:{template:{application_fields:"{}",expense_fields:"{}",sync_mode:"disabled"},business_timezone:"Asia/Shanghai"},
    call:async(method,args)=>{state.calls.push({method,args});return state.data;},
    isCurrent:()=>state.current,onApply:row=>state.applied.push(row)};
  state.change=(dialog,group,key,value,table)=>dialog.fields_dict.editor.$wrapper.handler.call({dataset:{group,key,table},value});
  state.preview=dialog=>dialog.fields_dict.preview_mapping.df.click();
  return state;
}

test("no-op preserves different legacy arrays and whitespace exactly",()=>{
  const row={application_fields:'{ "amount": ["old-cn", "alternative"] }',expense_fields:'{"amount":["old-mx"]}',
    application_classification:'[["cn-category"]]',expense_classification:'[["mx-category"]]'};
  assert.deepEqual(editor.patch(row,["cn","mx"],empty()),row);
});
test("one explicit change updates both processors and preserves unrelated config and enablement",()=>{
  const row={application_fields:'{"amount":["old-cn"],"entity":["cn-owner"]}',expense_fields:'{"amount":["old-mx"]}',sync_mode:"disabled"};
  const result=editor.patch(row,["cn","mx"],{headers:{amount:"MoneyField_total"},children:{allocation_table:{amount:"MoneyField_one"}},currency:{allocation_table:"header"}});
  for(const key of ["application_fields","expense_fields"]){
    const map=JSON.parse(result[key]);
    assert.deepEqual(map.amount,["MoneyField_total"]);
    assert.deepEqual(map.table_fields.allocation_table.amount,["MoneyField_one"]);
    assert.equal(map.table_currency.allocation_table,"header");
  }
  assert.equal(result.sync_mode,"disabled");
  assert.deepEqual(JSON.parse(result.application_fields).entity,["cn-owner"]);
  assert.deepEqual(JSON.parse(row.expense_fields).amount,["old-mx"]);
});
test("using defaults removes only requested overrides and preserves other table roles",()=>{
  const row={expense_fields:JSON.stringify({amount:["old"],table_fields:{allocation_table:{amount:["old-detail"],entity:["owner"]}},table_currency:{allocation_table:"header"}})};
  const result=editor.patch(row,["mx"],{headers:{amount:editor.DEFAULT},children:{allocation_table:{amount:editor.DEFAULT}},currency:{allocation_table:editor.DEFAULT}});
  assert.deepEqual(JSON.parse(result.expense_fields),{table_fields:{allocation_table:{entity:["owner"]}}});
  assert.equal(result.application_fields,undefined);
});
test("malformed legacy mappings are never silently erased",()=>{
  for(const value of ["[]","null","bad JSON"])assert.throws(()=>editor.patch({expense_fields:value},["mx"],empty()));
});
test("child choices come only from the selected table and respect component types",()=>{
  runtime();const row={application_fields:"{}",expense_fields:"{}"},edits=empty();
  let html=editor.editorHTML(row,sample(),edits);
  assert.match(html,/value="MoneyField_one"/);assert.doesNotMatch(html,/value="MoneyField_two"/);
  edits.headers.allocation_table="TableField_two";
  html=editor.editorHTML(row,sample(),edits);
  assert.match(html,/value="MoneyField_two"/);assert.doesNotMatch(html,/value="MoneyField_one"/);
});
test("different historical table bindings require an explicit common table selection",()=>{
  runtime();const row={application_fields:'{"allocation_table":["TableField_one"]}',expense_fields:'{"allocation_table":["TableField_two"]}'};
  const html=editor.editorHTML(row,sample(),empty());
  assert.match(html,/Choose one detail table/);assert.doesNotMatch(html,/value="MoneyField_one"/);
});
test("server labels, identity hints and preview values are escaped",()=>{
  runtime();const data=sample(), attack='<img src=x onerror="alert(1)">';
  data.fields[0].label=attack;data.samples[0].lines[0].entity=attack;data.samples[0].issues=[attack];
  const html=editor.editorHTML({application_fields:"{}",expense_fields:"{}"},data,empty())+editor.sampleHTML(data);
  assert.doesNotMatch(html,/<img/);assert.match(html,/&lt;img/);
});
test("mapping is applied to parent draft only after previewing the current revision",async()=>{
  const state=runtime(),dialog=await editor.open(state.options);
  state.change(dialog,"headers","amount","MoneyField_total");
  dialog.config.primary_action();assert.equal(state.applied.length,0);assert.match(state.messages.at(-1),/Preview the changed/);
  await state.preview(dialog);dialog.config.primary_action();
  assert.equal(state.applied.length,1);assert.equal(state.applied[0].sync_mode,"disabled");
  assert.deepEqual(JSON.parse(state.applied[0].expense_fields).amount,["MoneyField_total"]);
  assert.ok(state.calls.every(call=>call.method==="inspect_sync_template"));
});
test("parent template changes discard discovery responses",async()=>{
  const state=runtime();let resolve;
  state.options.call=()=>new Promise(done=>{resolve=done;});
  const pending=editor.open(state.options);state.current=false;resolve(state.data);await pending;
  assert.equal(state.dialogs.length,0);assert.equal(state.applied.length,0);
});
test("an older preview cannot authorize applying a newer edit",async()=>{
  const state=runtime(),dialog=await editor.open(state.options);let resolve;
  state.options.call=()=>new Promise(done=>{resolve=done;}); // open captured the original call
  // Replace the response used by that original function with a deferred thenable.
  state.data=new Promise(done=>{resolve=done;});
  state.change(dialog,"headers","amount","MoneyField_total");const pending=state.preview(dialog);
  state.change(dialog,"headers","allocation_table","TableField_two");resolve(sample());await pending;
  dialog.config.primary_action();assert.equal(state.applied.length,0);
  assert.match(state.messages.at(-1),/Preview the changed/);
});
test("closing the dialog prevents a late preview from rendering or applying",async()=>{
  const state=runtime(),dialog=await editor.open(state.options);let resolve;
  state.data=new Promise(done=>{resolve=done;});
  const pending=state.preview(dialog);dialog.hide();resolve(sample());await pending;
  dialog.config.primary_action();assert.equal(state.applied.length,0);
});
test("a changed parent cannot receive draft bindings",async()=>{
  const state=runtime(),dialog=await editor.open(state.options);
  state.current=false;dialog.config.primary_action();assert.equal(state.applied.length,0);
});
test("changing a table clears unsaved child choices but preserves saved bindings until explicit correction",async()=>{
  const state=runtime(),dialog=await editor.open(state.options);
  state.change(dialog,"children","amount","MoneyField_one","allocation_table");
  state.change(dialog,"headers","allocation_table","TableField_two");
  await state.preview(dialog);
  const sent=JSON.parse(state.calls.at(-1).args.template.expense_fields);
  assert.deepEqual(sent,{allocation_table:["TableField_two"]});
});

test("restoring keep-current after preview restores original default table scope",async()=>{
  const state=runtime(),dialog=await editor.open(state.options);
  state.change(dialog,"headers","allocation_table","TableField_two");
  state.data={...sample(),defaults:{allocation_table:["TableField_two"]}};
  await state.preview(dialog);
  state.change(dialog,"headers","allocation_table",editor.KEEP);
  const html=dialog.fields_dict.editor.$wrapper.value;
  assert.match(html,/value="MoneyField_one"/);assert.doesNotMatch(html,/value="MoneyField_two"/);
});
