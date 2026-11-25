const test = require("node:test");
const assert = require("node:assert/strict");
const settings = require("../public/js/expense_sync_settings.js");
const helpers = require("../public/js/cn_approval_settings.js");
const mappings = require("../public/js/expense_company_mappings.js");
const {esc} = require("../public/js/cn_approval_workbench.js");

function runtime() {
  const saved = {sync_mode:"mixed", versions:{cn:"cn-v1",mx:"mx-v1"},
    business_timezone:"America/Mexico_City", corporations:["corp"], companies:["CN", "MX"], expense_companies:["MX"],
    template_defaults:{corp_id:"corp",processing_rule:"both"},template_defaults_version:"d1",
    templates:[{key:"shared",corp_id:"corp",process_code:"PROC-test",template_name:"Test",approval_type:"operating",processing_rule:"both",finance_node_names:"Finance",application_fields:"{}",expense_fields:'{"amount":["MXField"]}'}],
    company_mappings:[], management_rules:{warning_percent:90,category_mappings:[{kind:"operating",source_path:["A","B"],category:"service"}]},
    management_categories:{service:"Professional services"}};
  const state = {saved,calls:[],flushed:[],alerts:[],savedCount:0};
  global.__ = (key,args=[]) => key.replace(/\{(\d+)\}/g, (_, index) => args[index] ?? `{${index}}`);
  global.CNApprovalSettings = {...helpers,
    guardGrid() {}, flushGridEditors(grid) {state.flushed.push(grid.name);}};
  global.ExpenseCompanyMappings = {...mappings, Controller: class extends mappings.Controller {
    constructor(options) { super(options); state.mappingEditor = this; }
  }};
  global.frappe = {utils:{escape_html:esc},
    call:async args => {state.calls.push(args);if (state.fail && args.method.endsWith("save_sync_settings")) throw Error("rejected");return {message:saved};},
    msgprint:text=>state.alerts.push(text), show_alert:text=>state.alerts.push(text),
    ui:{Dialog:class {
      constructor(config) {
        this.config=config;
        this.fields_dict=Object.fromEntries(config.fields.map(field=>[field.fieldname,{df:field,grid:{name:field.fieldname,df:field,
          get_data(){return field.data || [];},get_selected_children(){return this.get_data().filter(row=>row.__checked);},refresh(){},
          wrapper:{handlers:{},on(events,callback){this.handlers[events]=callback;}}},$wrapper:{
          value:"",events:{},text(value){this.value=value;},html(value){this.value=value;},empty(){this.value="";},
          find(selector){return {on:(_event,handler)=>{this.events[selector]=handler;}};}
        }}]));
        for (const field of Object.values(this.fields_dict)) {
          const nodes=new Map();
          field.$wrapper[0] = {
            ownerDocument:{activeElement:null}, contains:()=>false, addEventListener(){},removeEventListener(){},
            querySelector(selector){if(!nodes.has(selector))nodes.set(selector,{});return nodes.get(selector);},
          };
        }
        this.$wrapper={addClass(){}};
      }
      get_values(){return state.values ||= {sync_mode:"mixed",business_timezone:saved.business_timezone,
        common_corp_id:saved.template_defaults.corp_id,common_processing_rule:saved.template_defaults.processing_rule,
        templates:saved.templates,company_mappings:this.fields_dict.company_mappings.grid.get_data(),warning_percent:90,
        category_mappings:this.fields_dict.category_mappings.df.data};}
      get_value(name){return this.get_values()[name];}
      show(){this.visible=this.is_visible=true;} hide(){this.visible=this.is_visible=false;this.config.onhide?.();}
      disable_primary_action(){this.disabled=true;} enable_primary_action(){this.disabled=false;}
    }}
  };
  return state;
}

test("one save submits all templates and mapping domains with both versions", async()=>{
  const state=runtime();
  const dialog=await settings.open(()=>{state.savedCount++;});
  await dialog.config.primary_action();
  assert.deepEqual(state.flushed,["templates","company_mappings","category_mappings"]);
  const command=state.calls.at(-1);
  assert.equal(command.method,"budget.api.expenses.save_sync_settings");
  assert.deepEqual(command.args.values.versions,state.saved.versions);
  assert.deepEqual(command.args.values.management_rules.category_mappings[0].source_path,["A","B"]);
  assert.equal(command.args.values.templates[0].expense_fields,'{"amount":["MXField"]}');
  assert.equal(command.args.values.sync_mode,"mixed");
  assert.deepEqual(command.args.values.template_defaults,{corp_id:"corp"});
  assert.equal(command.args.values.processing_mode,"approval");
  assert.equal(command.args.values.template_defaults_version,"d1");
  assert.equal(state.savedCount,1);assert.equal(dialog.visible,false);assert.equal(dialog.disabled,false);
});

test("common choices are outside template rows and inherited by every draft",async()=>{
  const state=runtime(), dialog=await settings.open();
  for(const name of ["corp_id","processing_rule"])
    assert.equal(dialog.fields_dict.templates.df.fields.find(field=>field.fieldname===name).hidden,1);
  assert.equal(dialog.fields_dict.common_corp_id.df.read_only,true);
  const draft={approval_type:"operating",template_name:"New",process_code:"PROC-new",corp_id:"stale",processing_rule:"expense",finance_node_names:"Finance"};
  state.values=dialog.get_values();state.values.templates.push(draft);
  await dialog.config.primary_action();
  const rows=state.calls.at(-1).args.values.templates;
  assert.equal(rows[1].corp_id,"corp");assert.equal(rows[1].processing_rule,"both");
  assert.equal(rows[0].processing_rule,"both");
  assert.equal(draft.corp_id,"stale"); // payload does not mutate editor state
});

test("shared rules retain saved bindings and respect budget and expense parsers",()=>{
  runtime();const defaults={corp_id:"corp",processing_rule:"both"};
  assert.equal(settings.inheritDefaults({approval_type:"budget"},defaults).processing_rule,"application");
  assert.equal(settings.inheritDefaults({approval_type:"expense"},defaults).processing_rule,"expense");
  const history={key:"saved",corp_id:"old",processing_rule:"application"};
  assert.equal(settings.inheritDefaults(history,defaults),history);
});

test("missing common corporation and duplicate rows are blocked before saving",async()=>{
  const state=runtime(), dialog=await settings.open();state.values=dialog.get_values();
  state.values.templates.push({approval_type:"operating",template_name:"New",process_code:"PROC-new"});
  state.values.common_corp_id="";
  await dialog.config.primary_action();
  assert.match(state.alerts.at(-1),/row 2: Corporation ID is required/);
  assert.equal(state.calls.length,1);
  state.values.common_corp_id="corp";
  state.values.templates[1].process_code="PROC-test";
  await dialog.config.primary_action();
  assert.match(state.alerts.at(-1),/row 2:.*already listed/);
  assert.equal(state.calls.length,1);
});

test("failed saves keep draft open and invalid category JSON never submits", async()=>{
  const state=runtime();
  const dialog=await settings.open();
  state.fail=true;
  await assert.rejects(dialog.config.primary_action(),/rejected/);
  assert.equal(dialog.visible,true);assert.equal(dialog.disabled,false);
  state.values.category_mappings[0].source_path="unfinished";
  const before=state.calls.length;
  await dialog.config.primary_action();
  assert.equal(state.calls.length,before);
  assert.match(state.alerts.at(-1),/valid JSON/);
});

test("company options retain source permissions and escape names", async()=>{
  runtime();
  const dialog=await settings.open();
  const field=dialog.fields_dict.company_mappings.df.fields.find(f=>f.fieldname==="company");
  assert.deepEqual(field.get_query({match_kind:"organization"}).filters.name,["in",["CN","MX"]]);
  assert.deepEqual(field.get_query({match_kind:"entity"}).filters.name,["in",["MX"]]);
  assert.equal(field.formatter("<img src=x>"),"&lt;img src=x&gt;");
});

test("leaving page while configuration loads cannot open a stale dialog",async()=>{
  const state=runtime();
  assert.equal(await settings.open(null,{isCurrent:()=>false}),undefined);
  assert.equal(state.calls.length,1);
});

function grid(data, selected=[]) {
  return {df:{data},grid_rows:[],get_data(){return this.df.data;},get_selected_children(){return selected;},refresh(){this.refreshed=true;},
    grid_pagination:{page_length:50,go_to_page(page){this.page=page;}}};
}

test("only draft templates can be removed and unrelated edits survive",()=>{
  const state=runtime(), saved={key:"persisted",expense_fields:"changed rules"}, draft={}, clone={...saved};
  const rows=[saved,draft,clone], persisted=new WeakSet([saved]);
  const g=grid(rows,[draft,clone]);let closed=0;
  g.grid_rows=[{doc:draft,wrapper:{hasClass:()=>true},toggle_view(){closed++;}}];
  assert.equal(settings.removeDraftTemplates(g,persisted),true);
  assert.equal(closed,1);assert.equal(g.df.data,rows);assert.deepEqual(rows,[{...saved,idx:1}]);
  assert.equal(saved.expense_fields,"changed rules");
  const guarded=grid(rows,[saved]);
  settings.removeDraftTemplates(guarded,persisted);
  assert.equal(rows.length,1);assert.match(state.alerts.at(-1),/Disable historical/);
});

test("mapping candidates preserve long names, separate matching scopes, and reject duplicates",()=>{
  const state=runtime(), g=grid([]), name="Long source name ".repeat(20);
  settings.appendCandidate(g,"organizations",{corp_id:"corp",source_name:name});
  settings.appendCandidate(g,"entities",{match_kind:"department",source_name:name});
  assert.equal(g.df.data.length,2);assert.equal(g.df.data[0].source_name,name);
  assert.equal(g.df.data[0].match_kind,"organization");assert.equal(g.df.data[1].company,"");
  settings.appendCandidate(g,"organizations",{corp_id:"corp",source_name:name});
  assert.equal(g.df.data.length,2);assert.match(state.alerts.at(-1),/already in/);
});

test("category candidates display a path but submit the untouched JSON identity",async()=>{
  runtime();
  const g=grid([]), candidate={kind:"operating",source_path:['A "quoted"',"B / C"]};
  settings.appendCandidate(g,"categories",candidate);
  assert.deepEqual(JSON.parse(g.df.data[0].source_path),candidate.source_path);
  assert.equal(g.df.data[0].source_label,'A "quoted" / B / C');
  settings.appendCandidate(g,"categories",candidate);assert.equal(g.df.data.length,1);
  const dialog=await settings.open();
  assert.equal(dialog.fields_dict.category_mappings.df.fields.find(f=>f.fieldname==="source_path").hidden,1);
  assert.equal(dialog.fields_dict.company_mappings.df.fields.find(f=>f.fieldname==="source_name").fieldtype,"Small Text");
});

test("the combined mapping workspace reads local candidates, escapes names and ignores closed responses",async()=>{
  const state=runtime(), dialog=await settings.open();
  assert.equal(state.calls.length,1);
  assert.equal(dialog.fields_dict.company_mappings.df.hidden,1);
  assert.equal(dialog.fields_dict.discover_organizations,undefined);
  assert.equal(dialog.fields_dict.discover_entities,undefined);
  global.frappe.call=async()=>({message:{rows:[{corp_id:"corp",source_name:"<img src=x>",count:2}],start:0,has_more:true}});
  // The expense stream has its own matching domain even for identical labels.
  global.frappe.call=async args=>({message:{rows:[{corp_id:"corp",match_kind:"entity",source_name:"<img src=x>",count:2}],start:args.args.start,has_more:true}});
  await state.mappingEditor.load();
  const element=dialog.fields_dict.company_mapping_workspace.$wrapper[0];
  const html=element.querySelector('[data-mapping-part="results"]').innerHTML;
  assert.match(html,/&lt;img src=x&gt;/);assert.doesNotMatch(html,/<img/);
  const before=state.mappingEditor.model.candidates.size, resolves=[];
  global.frappe.call=()=>new Promise(done=>{resolves.push(done);});
  const pending=state.mappingEditor.load();
  dialog.hide();resolves.forEach(resolve=>resolve({message:{rows:[],has_more:false}}));await pending;
  assert.equal(state.mappingEditor.model.candidates.size,before);
  assert.equal(state.mappingEditor.active,false);
});

test("mapping filters do not truncate the atomic settings payload and failed saves retain drafts",async()=>{
  const state=runtime(), dialog=await settings.open(), editor=state.mappingEditor;
  editor.model.put({match_kind:"entity",source_name:"One",company:"MX"});
  editor.model.put({match_kind:"organization",corp_id:"corp",source_name:"Two",company:"CN"});
  editor.tab="mapped";editor.query="One";editor.render();
  assert.equal(editor.visible.length,1);
  state.fail=true;
  await assert.rejects(dialog.config.primary_action(),/rejected/);
  assert.equal(editor.model.rows.length,2);
  assert.equal(editor.active,true);
  state.fail=false;
  await dialog.config.primary_action();
  assert.equal(state.calls.at(-1).args.values.company_mappings.length,2);
  assert.equal(editor.active,false);
});

test("clearing all mappings sends an explicit empty list when native Table omits its value",async()=>{
  const state=runtime(), dialog=await settings.open();
  state.values=dialog.get_values();
  delete state.values.company_mappings;
  await dialog.config.primary_action();
  assert.deepEqual(state.calls.at(-1).args.values.company_mappings,[]);
});

test("pasted codes normalize before save and duplicate checks without rewriting historical identities",async()=>{
  const state=runtime(),dialog=await settings.open();state.values=dialog.get_values();
  const draft={approval_type:"operating",template_name:"New",process_code:"  PROC-new \n",finance_node_names:"Finance"};
  state.values.templates.push(draft);
  await dialog.config.primary_action();
  assert.equal(state.calls.at(-1).args.values.templates[1].process_code,"PROC-new");
  assert.equal(draft.process_code,"  PROC-new \n");
  const error=settings.validateTemplates([state.saved.templates[0],{...state.saved.templates[0],key:undefined,process_code:" PROC-test "}],["corp"]);
  assert.match(error,/row 2:.*already listed/);
});

function previewRuntime(state,dialog) {
  const rows=state.saved.templates;
  rows.push({...rows[0],key:"second",template_name:"Second",process_code:"PROC-second",processing_rule:"expense"});
  rows[0].__checked=1;
  const pending=[];
  global.frappe.call=options=>new Promise((resolve,reject)=>pending.push({options,resolve,reject}));
  return {rows,pending,start:()=>dialog.fields_dict.preview.df.click(),result:()=>dialog.fields_dict.preview_result.$wrapper.value};
}

test("older preview success and failure cannot replace the latest selection",async()=>{
  for(const rejectOlder of [false,true]) {
    const state=runtime(),dialog=await settings.open(),p=previewRuntime(state,dialog);
    const first=p.start();p.rows[0].__checked=0;p.rows[1].__checked=1;
    const second=p.start();
    p.pending[1].resolve({message:[{rule:"expense",count:222,issues:[]}]});await second;
    if(rejectOlder)p.pending[0].reject(Error("old failure"));
    else p.pending[0].resolve({message:[{rule:"application",count:111,issues:[]}]});
    await first;
    assert.match(p.result(),/222/);assert.doesNotMatch(p.result(),/111/);assert.equal(state.alerts.length,0);
  }
});

test("preview rejects changed drafts, rules, selection, timezone, removed rows and closed dialogs",async()=>{
  for(const change of ["draft","corporation","selection","timezone","removed","closed"]) {
    const state=runtime(),dialog=await settings.open(),p=previewRuntime(state,dialog),request=p.start();
    if(change==="draft")p.rows[0].expense_fields='{"amount":["changed"]}';
    if(change==="corporation")dialog.get_values().common_corp_id="changed";
    if(change==="selection"){p.rows[0].__checked=0;p.rows[1].__checked=1;}
    if(change==="timezone")dialog.get_values().business_timezone="Asia/Shanghai";
    if(change==="removed")p.rows.shift();
    if(change==="closed"){dialog.hide();dialog.show();}
    p.pending[0].resolve({message:[{rule:"expense",count:333,issues:[]}]});await request;
    assert.equal(p.result(),"",change);
  }
});

test("rendered preview is cleared on native editing and common corporation changes",async()=>{
  const state=runtime(),dialog=await settings.open(),p=previewRuntime(state,dialog);
  const request=p.start();p.pending[0].resolve({message:[{rule:"expense",count:444,issues:[]}]});await request;
  assert.match(p.result(),/444/);
  const events=dialog.fields_dict.templates.grid.wrapper.handlers;
  events["input.expense-sync-preview change.expense-sync-preview"]();
  assert.equal(p.result(),"");
  dialog.fields_dict.preview_result.$wrapper.html("stale");
  dialog.fields_dict.common_corp_id.df.onchange();
  assert.equal(p.result(),"");
});

test("preview uses the original row number and canonical process code",async()=>{
  const state=runtime(),dialog=await settings.open(),p=previewRuntime(state,dialog);
  p.rows[0].__checked=0;p.rows[1].__checked=1;delete p.rows[1].key;
  p.rows[1].idx=99;p.rows[1].process_code=" \tPROC-second \n";
  p.rows[1].template_name="";
  await p.start();assert.match(state.alerts.at(-1),/row 2:/);assert.equal(p.pending.length,0);
  p.rows[1].template_name="Second";
  p.rows[1].finance_node_names="";
  const request=p.start();
  assert.equal(p.pending[0].options.args.template.idx,2);
  assert.equal(p.pending[0].options.args.template.process_code,"PROC-second");
  p.pending[0].resolve({message:[{rule:"expense",count:1,issues:[]}]});await request;
});


test("unified editor removes processing and finance controls and hides field mappings in advanced settings",async()=>{
  const state=runtime(),dialog=await settings.open();
  assert.equal(dialog.fields_dict.common_processing_rule,undefined);
  const fields=dialog.fields_dict.templates.df.fields;
  assert.equal(fields.some(field=>["finance_node_names","finance_activity_ids"].includes(field.fieldname)),false);
  assert.equal(fields.find(field=>field.fieldname==="template_advanced").collapsible,1);
  assert.equal(dialog.fields_dict.business_timezone.df.label,"OA business timezone");
  for(const kind of ["budget","operating","purchase","expense"]) {
    const row=settings.inheritDefaults({approval_type:kind,process_code:"PROC-"+kind,template_name:kind},{corp_id:"corp",processing_rule:"obsolete"});
    assert.equal(settings.validateTemplates([row],["corp"]),undefined);
  }
  const p=previewRuntime(state,dialog),request=p.start();
  assert.equal(p.pending[0].options.args.processing_mode,"approval");
  p.pending[0].resolve({message:[{rule:"approval",count:3,issues:["<bad field>"]}]});await request;
  assert.match(p.result(),/Approval data preview/);
  assert.match(p.result(),/&lt;bad field&gt;/);
  assert.doesNotMatch(p.result(),/finance|undefined/);
});
