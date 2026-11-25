const test=require("node:test");
const assert=require("node:assert/strict");
const settings=require("../public/js/cn_approval_settings.js");
const {esc}=require("../public/js/cn_approval_workbench.js");
global.frappe={utils:{escape_html:esc}};

test("untrusted names and codes render as text without changing mapping identities",()=>{
	const raw='<img src=x onerror="window.executed=true"> & Organization';
	const fields=settings.textFields([
		{fieldname:"source_name",fieldtype:"Small Text"},
		{fieldname:"template_name",fieldtype:"Data"},
		{fieldname:"process_code",fieldtype:"Data"},
		{fieldname:"corp_id",fieldtype:"Select"},
		{fieldname:"company",fieldtype:"Select"},
	]);
	const row=Object.fromEntries(fields.map(field=>[field.fieldname,raw]));
	for (const field of fields) {
		assert.equal(field.formatter(row[field.fieldname]),esc(raw));
		assert.ok(!field.formatter(raw).includes("<img"));
		assert.equal(row[field.fieldname],raw);
	}
});

test("text formatting preserves translated approval labels and native non-text controls",()=>{
	const kind={fieldtype:"Select",formatter:value=>esc(`Label: ${value}`)};
	const flag={fieldtype:"Check"}, code={fieldtype:"Code"};
	const result=settings.textFields([kind,flag,code]);
	assert.equal(result[0].formatter,kind.formatter);
	assert.equal(result[1],flag);assert.equal(result[2],code);
});

test("corporation and subsidiary option labels escape markup while preserving submitted values",()=>{
	const raw='<img src=x onerror="window.executed=true"> & Company';
	assert.deepEqual(settings.textOptions([raw]),[
		{value:"",label:""},{value:raw,label:esc(raw)},
	]);
});

function makeGrid(count=51,externalFreeze=0) {
	const state={freeze:externalFreeze,closes:0,rendered:0};
	function makeRow(doc) {
		const row={doc,open:false,inputs:{field_mapping:'{"region":["new-region"]}',classification_fields:'[["new-category"]]'}};
		row.wrapper={hasClass:()=>row.open};
		row.grid_form={fields_dict:Object.fromEntries(Object.keys(row.inputs).map(name=>[name,{editor:{},get_value:()=>row.inputs[name]}]))};
		row.hide_form=function() {
			state.closes++;state.freeze--;this.open=false;
			// A native close refreshes controls from their draft model.
			this.inputs={field_mapping:this.doc.field_mapping,classification_fields:this.doc.classification_fields};
		};
		row.toggle_view=function(show) {
			if (!show) return this.hide_form();
			this.open=true;state.freeze++;
		};
		return row;
	}
	const grid={data:Array.from({length:count},(_,i)=>({name:`row-${i}`,field_mapping:"{}",classification_fields:""})),
		grid_rows:[],grid_pagination:{page_index:1,page_length:50},render_result_rows(marker) {
			state.rendered++;
			const old=new Map(this.grid_rows.filter(Boolean).map(row=>[row.doc,row]));
			this.grid_rows=[];
			const start=(this.grid_pagination.page_index-1)*50;
			this.data.slice(start,start+50).forEach((doc,index)=>this.grid_rows[start+index]=old.get(doc) || makeRow(doc));
			// Like Frappe, simply drop old rows without closing them here.
			return marker;
		}};
	grid.render_result_rows();settings.guardGrid(grid);
	return {grid,state};
}

test("cross-page rendering closes outgoing editors and retains unrelated freezes",()=>{
	const {grid,state}=makeGrid(51,1);
	grid.grid_rows[0].toggle_view(true);assert.equal(state.freeze,2);
	grid.grid_pagination.page_index=2;
	assert.equal(grid.render_result_rows("result"),"result");
	assert.equal(state.freeze,1);assert.equal(state.closes,1);
	grid.grid_rows[50].toggle_view(true);
	grid.grid_rows[50].hide_form();
	assert.equal(state.freeze,1);assert.equal(state.closes,2);
});

test("filtering out an edited row closes it before its DOM would be removed",()=>{
	const {grid,state}=makeGrid();
	const row=grid.grid_rows[0];row.toggle_view(true);
	grid.data=[grid.data[1]];grid.render_result_rows();
	assert.equal(state.freeze,0);assert.equal(row.open,false);
	assert.equal(row.doc.field_mapping,'{"region":["new-region"]}');
});

test("refreshing a still-visible row captures the latest JSON without collapsing it",()=>{
	const {grid,state}=makeGrid();
	const row=grid.grid_rows[0];row.toggle_view(true);
	grid.render_result_rows();
	assert.equal(state.freeze,1);assert.equal(state.closes,0);assert.equal(row.open,true);
	assert.equal(row.doc.classification_fields,'[["new-category"]]');
});

test("save and preview can capture both JSON edits before Ace's delayed change fires",()=>{
	const {grid}=makeGrid();
	const row=grid.grid_rows[0];row.toggle_view(true);
	settings.flushGridEditors(grid);
	assert.equal(row.doc.field_mapping,'{"region":["new-region"]}');
	assert.equal(row.doc.classification_fields,'[["new-category"]]');
	// Invalid JSON must reach the API validator, not silently fall back to old rules.
	row.inputs.field_mapping="{unfinished";
	settings.flushGridEditors(grid);assert.equal(row.doc.field_mapping,"{unfinished");
});

test("manual collapse flushes before native refresh and does not double-wrap rows",()=>{
	const {grid,state}=makeGrid();
	const row=grid.grid_rows[0];row.toggle_view(true);
	grid.render_result_rows();grid.render_result_rows();
	row.inputs.field_mapping='{"amount":["new-amount"]}';row.hide_form();
	assert.equal(row.doc.field_mapping,'{"amount":["new-amount"]}');
	assert.equal(state.closes,1);assert.equal(state.freeze,0);
});

test("closed or uninitialized editors cannot erase a newer draft value",()=>{
	const {grid}=makeGrid();
	const row=grid.grid_rows[0];row.doc.field_mapping='{"region":["current"]}';
	settings.flushGridEditors(grid);
	assert.equal(row.doc.field_mapping,'{"region":["current"]}');
	row.toggle_view(true);delete row.grid_form.fields_dict.field_mapping.editor;
	settings.flushGridEditors(grid);
	assert.equal(row.doc.field_mapping,'{"region":["current"]}');
});

test("unified template fields flush independently before delayed editor changes",()=>{
	const {grid}=makeGrid();
	const row=grid.grid_rows[0];row.toggle_view(true);
	for (const name of ["application_fields", "expense_fields", "expense_classification"]) {
		row.grid_form.fields_dict[name]={editor:{},get_value:()=>`["${name}"]`};
	}
	settings.flushGridEditors(grid);
	assert.equal(row.doc.application_fields,'["application_fields"]');
	assert.equal(row.doc.expense_fields,'["expense_fields"]');
	assert.equal(row.doc.expense_classification,'["expense_classification"]');
});

test("temporarily hidden rule editors do not erase captured drafts",()=>{
	const {grid}=makeGrid();
	const row=grid.grid_rows[0];row.toggle_view(true);
	row.doc.expense_fields='{"amount":["draft"]}';
	row.grid_form.fields_dict.expense_fields={editor:{},get_value:()=>undefined};
	settings.flushGridEditors(grid);
	assert.equal(row.doc.expense_fields,'{"amount":["draft"]}');
	row.grid_form.fields_dict.expense_fields={editor:{},get_status:()=>"None",get_value:()=>"stale"};
	settings.flushGridEditors(grid);
	assert.equal(row.doc.expense_fields,'{"amount":["draft"]}');
});

test("dialog grid collapses only explicitly requested sections after native form rendering",()=>{
 const calls=[];
 const section=name=>({df:{fieldname:name},collapse:value=>calls.push([name,value])});
 const row={hide_form(){},show_form(){calls.push('native');return 'shown';},
  grid_form:{layout:{sections:[section('basic'),section('advanced')]}},wrapper:{hasClass:()=>false}};
 const grid={grid_rows:[row],render_result_rows(){}};
 settings.guardGrid(grid,{collapsedSections:['advanced']});
 assert.equal(row.show_form(),'shown');
 assert.deepEqual(calls,['native',['advanced',true]]);
});
