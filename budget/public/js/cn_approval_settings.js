/* CN configuration uses the same native Dialog/Table controls as MX. */
(function(root,factory) {
	const api=factory(root);
	if (typeof module==="object" && module.exports) module.exports=api;
	root.CNApprovalSettings=api;
})(typeof globalThis!=="undefined" ? globalThis : this,function(root) {
	"use strict";
	const t=(text,args)=>root.__(text,args);
	const esc=value=>root.frappe.utils.escape_html(String(value ?? ""));
	const KINDS={budget:"Budget declaration",operating:"Operating expenditure",purchase:"Purchase expenditure"};
	const call=async(method,args={})=>(await root.frappe.call({method:`budget.api.cn_approvals.${method}`,args,silent:true})).message;
	const errorText=(error)=>root.CNApprovalWorkbench?.userError
		? root.CNApprovalWorkbench.userError(error,"CN approval settings could not be loaded or saved. Retry.")
		: t("CN approval settings could not be loaded or saved. Retry.");
	// Native Select also inserts option labels as HTML. Keep values unchanged.
	const textOptions=values=>[{value:"",label:""},...values.map(value=>({value,label:esc(value)}))];
	const pathLabel=(kind,path)=>path.map((part,index)=>kind==="budget"&&index===0?t(({hr:"Personnel budget",office:"Office premises budget",operation:"Operating budget",unitemized:"Unitemized budget"})[part]||part):part).join(" / ");
	function textFields(fields) {
		// Frappe's default Data/Select/Small Text grid formatters return HTML.
		// Escape at display time; keep exact source names in the mapping payload.
		return fields.map(field=>["Data","Select","Small Text"].includes(field.fieldtype)
			? {...field,formatter:field.formatter || esc} : field);
	}
	function flushRowEditors(row) {
		if (!row?.wrapper.hasClass("grid-row-open")) return;
		for (const [name,field] of Object.entries(row.grid_form?.fields_dict || {})) {
			// Ace debounces the draft update. Read the visible editor now, before
			// a request or native row refresh can use/reset the previous value.
			// The API still validates JSON and allowed component IDs on submission.
			if (field?.editor) {
				if (field.get_status && field.get_status()!=="Write") continue;
				const value=field.get_value();
				// Hidden native controls can return undefined or a stale model value.
				// A shared rule change must not erase a previously captured draft.
				if (value!==undefined) row.doc[name]=value;
			}
		}
	}
	function flushGridEditors(grid) {
		for (const row of grid.grid_rows || []) flushRowEditors(row);
	}
	function guardGrid(grid) {
		const guarded=new WeakSet();
		function guardRows() {
			for (const row of grid.grid_rows || []) {
				if (!row || guarded.has(row)) continue;
				guarded.add(row);
				const hide=row.hide_form;
				row.hide_form=function(...args) {
					flushRowEditors(this);
					return hide.apply(this,args);
				};
			}
		}
		const render=grid.render_result_rows;
		grid.render_result_rows=function(...args) {
			const {page_index,page_length}=this.grid_pagination;
			const visible=new Set(this.data.slice((page_index-1)*page_length,page_index*page_length));
			for (const row of this.grid_rows || []) {
				if (!row?.wrapper.hasClass("grid-row-open")) continue;
				flushRowEditors(row);
				// Native pagination/filtering removes old DOM without hide_form().
				// Release only this row's freeze, through the native lifecycle.
				if (!visible.has(row.doc)) row.toggle_view(false);
			}
			const result=render.apply(this,args);
			guardRows();
			return result;
		};
		guardRows();
	}
	async function open(onSaved, options={}) {
		const saved=await call("get_settings");
		if(options.isCurrent && !options.isCurrent()) return;
		let dialog, pending=false, page=0, sourcesSeq=0, categorySeq=0;
		const rules=saved.management_rules||{warning_percent:90,category_mappings:[]};
		const categoryOptions=[{value:"",label:""},...Object.entries(saved.management_categories||{}).map(([value,label])=>({value,label:esc(t(label))}))];
		const categoryRows=rules.category_mappings.map(row=>({...row,source_path:JSON.stringify(row.source_path),source_label:pathLabel(row.kind,row.source_path)}));
		const corporations=textOptions(saved.corporations);
		const corpField=()=>({fieldname:"corp_id",fieldtype:"Select",label:t("Corporation ID"),
			options:corporations,reqd:1,default:""});
		dialog=new root.frappe.ui.Dialog({title:t("CN Approval Synchronization"),size:"extra-large",fields:[
			{fieldname:"enabled",fieldtype:"Check",label:t("Enable CN approval synchronization"),default:saved.enabled},
			{fieldtype:"HTML",options:`<p class="text-muted">${esc(t("Approvals are included by enabled template without filtering by execution region. Budgets use the budget month; operating and purchase approvals use the application month."))}</p>`},
			{fieldname:"templates",fieldtype:"Table",label:t("Approval templates"),data:saved.templates,
				in_place_edit:false,fields:textFields([
					{fieldname:"enabled",fieldtype:"Check",label:t("Enabled"),in_list_view:1,columns:1,default:0},
					{fieldname:"approval_type",fieldtype:"Select",label:t("Approval form"),reqd:1,in_list_view:1,columns:2,
						formatter:value=>esc(t(KINDS[value] || value || "")),
						options:[{value:"",label:""},...Object.entries(KINDS).map(([value,label])=>({value,label:t(label)}))]},
					{fieldname:"template_name",fieldtype:"Data",label:t("Template name"),in_list_view:1,columns:3},
					corpField(),
					{fieldname:"process_code",fieldtype:"Data",label:t("Process code"),reqd:1,in_list_view:1,columns:4},
					{fieldname:"field_mapping",fieldtype:"Code",label:t("Field mapping"),options:"JSON",default:"",
						description:t("Optional JSON: logical field names mapped to arrays of component IDs. Applies to operating and purchase approvals; execution region mappings are ignored.")},
					{fieldname:"classification_fields",fieldtype:"Code",label:t("Classification fields"),options:"JSON",
						description:t("Optional JSON array of levels; each level is an array of component IDs. Applies to operating and purchase approvals.")},
				])},
			{fieldtype:"HTML",options:`<p class="text-muted">${esc(t("Select one template to preview. Disable saved templates instead of deleting them. Disabled templates leave the current reporting scope; stored snapshots are retained."))}</p>`},
			{fieldname:"preview",fieldtype:"Button",label:t("Preview selected template"),click:()=>preview()},
			{fieldname:"preview_result",fieldtype:"HTML"},
			{fieldtype:"Section Break",label:t("Company mappings")},
			{fieldtype:"HTML",options:`<p class="text-muted">${esc(t("Match the corporation and source organization exactly. Multiple historical names may map to the same subsidiary. Changes take effect only after saving."))}</p>`},
			{fieldname:"company_mappings",fieldtype:"Table",label:t("Company mappings"),hide_label:1,data:saved.company_mappings,
				in_place_edit:false,fields:textFields([
					{...corpField(),in_list_view:1,columns:3},
					{fieldname:"source_name",fieldtype:"Small Text",label:t("Source organization"),reqd:1,in_list_view:1,columns:4,max_height:64},
					{fieldname:"company",fieldtype:"Select",label:t("Subsidiary"),reqd:1,in_list_view:1,columns:3,
						options:textOptions(saved.companies)},
				])},
			{fieldtype:"Section Break",label:t("Unmapped source organizations")},
			{fieldname:"unmapped",fieldtype:"HTML"},
			{fieldtype:"Section Break",label:t("Management classification")},
			{fieldtype:"HTML",options:`<p class="cnam-rule-note">${esc(t("Latest approved budget per company and month; all approved operating and purchase applications occupy the budget."))}<br>${esc(t("Map budget items and expenditure categories to the same management category. Unmapped items remain visible and are never treated as zero."))}</p>`},
			{fieldname:"warning_percent",fieldtype:"Float",label:t("Budget warning percentage"),default:rules.warning_percent,precision:2,reqd:1},
			{fieldname:"category_mappings",fieldtype:"Table",label:t("Management category mappings"),data:categoryRows,
				cannot_add_rows:true,in_place_edit:false,fields:textFields([
					{fieldname:"kind",fieldtype:"Select",label:t("Approval form"),options:Object.keys(KINDS),read_only:1,in_list_view:1,columns:2,formatter:value=>esc(t(KINDS[value]||value||""))},
					{fieldname:"source_label",fieldtype:"Small Text",label:t("Source category path"),read_only:1,in_list_view:1,columns:5},
					{fieldname:"source_path",fieldtype:"Small Text",hidden:1},
					{fieldname:"category",fieldtype:"Select",label:t("Management category"),options:categoryOptions,reqd:1,in_list_view:1,columns:3,formatter:value=>esc(t(saved.management_categories?.[value]||value||""))},
				])},
			{fieldname:"unmapped_categories",fieldtype:"HTML"},
			{fieldname:"settings_error",fieldtype:"HTML"},
		],primary_action_label:t("Save settings"),primary_action:async()=>{
			if (pending) return;
			flushGridEditors(dialog.fields_dict.templates.grid);
			const values=dialog.get_values();if (!values) return;
			pending=true;dialog.disable_primary_action();showError("");
			try {
				await call("save_settings",{enabled:values.enabled ? 1 : 0,templates:values.templates || [],
					company_mappings:values.company_mappings || [],expected_config_version:saved.config_version,
					management_rules:{warning_percent:Number(values.warning_percent),category_mappings:(values.category_mappings||[]).map(row=>({kind:row.kind,source_path:JSON.parse(row.source_path),category:row.category}))}});
				dialog.hide();
				root.frappe.show_alert({message:t("CN approval settings saved."),indicator:"green"});
				onSaved();
			} catch(error) {showError(errorText(error));}
			finally {pending=false;dialog.enable_primary_action();}
		}});
		const showError=message=>dialog.fields_dict.settings_error.$wrapper.html(message ? `<div class="alert alert-danger" role="alert">${esc(message)}</div>` : "");
		async function preview() {
			if (pending) return;
			flushGridEditors(dialog.fields_dict.templates.grid);
			const selected=dialog.fields_dict.templates.grid.get_selected_children();
			if (selected.length!==1) {showError(t("Select exactly one template to preview."));return;}
			pending=true;dialog.disable_primary_action();showError("");
			const wrapper=dialog.fields_dict.preview_result.$wrapper;
			wrapper.text(t("Loading preview…"));
			try {
				const result=await call("preview_template",{template:selected[0]});
				wrapper.html(`<div class="alert alert-info" role="status"><p>${esc(t("Sample approvals: {0}; included in CN: {1}.",[result.sample_count,result.cn_count]))}</p>${result.issues.length ? `<ul>${result.issues.map(issue=>`<li>${esc(issue)}</li>`).join("")}</ul>` : ""}<small>${esc(t("This sample does not represent a complete synchronization. Review the data after synchronization."))}</small></div>`);
			} catch(error) {wrapper.empty();showError(errorText(error));}
			finally {pending=false;dialog.enable_primary_action();}
		}
		async function loadSources(start=0) {
			const seq=++sourcesSeq, wrapper=dialog.fields_dict.unmapped.$wrapper;
			wrapper.text(t("Loading approvals…"));
			try {
				const result=await call("get_unmapped_sources",{start});if (seq!==sourcesSeq) return;
				page=start;
				wrapper.html(`<div class="cnaw-mapping-sources">${result.rows.length ? result.rows.map((row,index)=>`<div class="cnaw-mapping-source"><div><strong>${esc(row.source_name)}</strong><small>${esc(row.corp_id)} · ${esc(t("{0} approvals",[row.count]))}</small></div><button class="btn btn-xs btn-default" type="button" data-add="${index}">${esc(t("Add mapping"))}</button></div>`).join("") : `<p class="text-muted">${esc(t("No unmapped organizations in the current local snapshot."))}</p>`}</div><div class="cnaw-mapping-pages"><span>${esc(t("{0} source organizations",[result.total]))}</span><button class="btn btn-xs btn-default" type="button" data-page="previous" ${start===0 ? "disabled" : ""}>${esc(t("Previous"))}</button><button class="btn btn-xs btn-default" type="button" data-page="next" ${start+50>=result.total ? "disabled" : ""}>${esc(t("Next"))}</button></div>`);
				wrapper.find("[data-add]").on("click",function() {
					if (pending) return;
					const row=result.rows[Number(this.dataset.add)], grid=dialog.fields_dict.company_mappings.grid;
					if (grid.get_data().some(item=>item.corp_id===row.corp_id && item.source_name?.trim()===row.source_name)) {
						showError(t("This source organization is already in the mapping table."));return;
					}
					grid.df.data.push({corp_id:row.corp_id,source_name:row.source_name,company:""});grid.refresh();
					grid.grid_pagination.go_to_page(Math.ceil(grid.df.data.length/grid.grid_pagination.page_length));
					const added=grid.grid_rows[grid.grid_rows.length-1];
					added?.toggle_view(true);
					added?.wrapper[0]?.scrollIntoView({block:"center"});
				});
				wrapper.find("[data-page]").on("click",function() {loadSources(Math.max(0,page+(this.dataset.page==="next" ? 50 : -50)));});
			} catch(error) {
				if (seq!==sourcesSeq) return;
				wrapper.html(`<div class="alert alert-warning">${esc(errorText(error))} <button class="btn btn-xs btn-default" type="button">${esc(t("Retry"))}</button></div>`);
				wrapper.find("button").on("click",()=>loadSources(start));
			}
		}
		async function loadCategories(start=0) {
			const seq=++categorySeq,wrapper=dialog.fields_dict.unmapped_categories.$wrapper;
			wrapper.text(t("Loading unmapped categories…"));
			try {
				const result=await call("get_unmapped_categories",{start});if(seq!==categorySeq)return;
				wrapper.html(`<p><strong>${esc(t("Unmapped management categories"))}</strong></p><div class="cnaw-mapping-sources">${result.rows.map((row,index)=>`<div class="cnaw-mapping-source"><div><strong>${esc(pathLabel(row.kind,row.source_path))}</strong><small>${esc(t(KINDS[row.kind]))} · ${esc(t("{0} approvals",[row.count]))}</small></div><button class="btn btn-xs btn-default" type="button" data-category-add="${index}">${esc(t("Add mapping"))}</button></div>`).join("")||`<p class="text-muted">${esc(t("No unmapped management categories."))}</p>`}</div><div class="cnaw-mapping-pages"><span>${esc(t("{0} source categories",[result.total]))}</span><button class="btn btn-xs btn-default" data-category-page="${Math.max(0,start-50)}" ${start===0?"disabled":""}>${esc(t("Previous"))}</button><button class="btn btn-xs btn-default" data-category-page="${start+50}" ${start+50>=result.total?"disabled":""}>${esc(t("Next"))}</button></div>`);
				wrapper.find("[data-category-add]").on("click",function() {
					if(pending)return;
					const row=result.rows[Number(this.dataset.categoryAdd)],grid=dialog.fields_dict.category_mappings.grid,path=JSON.stringify(row.source_path);
					if(grid.get_data().some(item=>item.kind===row.kind&&item.source_path===path)){showError(t("This category path is already in the mapping table."));return;}
					grid.df.data.push({kind:row.kind,source_path:path,source_label:pathLabel(row.kind,row.source_path),category:""});grid.refresh();
					grid.grid_pagination.go_to_page(Math.ceil(grid.df.data.length/grid.grid_pagination.page_length));
					const added=grid.grid_rows[grid.grid_rows.length-1];added?.toggle_view(true);added?.wrapper[0]?.scrollIntoView({block:"center"});
				});
				wrapper.find("[data-category-page]").on("click",function(){loadCategories(Number(this.dataset.categoryPage));});
			}catch(error){if(seq!==categorySeq)return;wrapper.html(`<div class="alert alert-warning">${esc(errorText(error))} <button class="btn btn-xs btn-default">${esc(t("Retry"))}</button></div>`);wrapper.find("button").on("click",()=>loadCategories(start));}
		}
		for (const name of ["templates","company_mappings","category_mappings"]) guardGrid(dialog.fields_dict[name].grid);
		dialog.$wrapper.addClass("cnaw-settings-dialog");
		dialog.show();loadSources();loadCategories();
		return dialog;
	}
	return {open,textFields,textOptions,flushGridEditors,guardGrid,pathLabel};
});
