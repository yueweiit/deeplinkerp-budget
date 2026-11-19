const test = require("node:test");
const assert = require("node:assert/strict");
const create = require("../public/js/budget_ledger_sync.js");

function host(call) {
	class Element {
		constructor() {this.events = {}; this.properties = {};}
		prop(key, value) {this.properties[key] = value; return this;}
		text(value) {this.content = value; return this;}
		html(value) {this.content = value; return this;}
		removeAttr() {return this;}
		off() {return this;}
		on(name, fn) {this.events[name] = fn; return this;}
	}
	class Dialog {
		constructor(options) {
			Object.assign(this, options);
			this.$wrapper = new Element(); this.primary = new Element(); this.close = new Element();
			this.fields_dict = Object.fromEntries(options.fields.filter(f=>f.fieldname).map(f=>[f.fieldname, {$wrapper:new Element()}]));
		}
		get_primary_btn() {return this.primary;}
		get_close_btn() {return this.close;}
		add_custom_action(label, action) {this.customActions ??= []; this.customActions.push({label,action});}
		show() {this.visible = true;}
		hide() {this.visible = false; this.$wrapper.events["hidden.bs.modal"]?.();}
	}
	return {frappe:{session:{user:"Administrator"}, user:{has_role:()=>false}, ui:{Dialog}, call,
		show_alert() {}, msgprint() {}}, __:text=>text};
}
const settings = {version:"v1",corp_id:"corp-1",profile:"budget",enabled:true,configured:true,blocked_by:null};

test("CN settings offer the shared source without starting synchronization", async () => {
	const requests = [];
	const api = create(host(async req => {
		requests.push(req.method);
		return {message:{...settings,shared_oa_available:true,profile:"operating_oa",enabled:false}};
	}));
	const dialog = await api.openSettings();
	const field = dialog.fields.find(f => f.fieldname === "profile");
	assert.equal(field.default,"operating_oa");
	assert.equal(field.options[0].value,"operating_oa");
	assert.equal(dialog.fields.find(f => f.fieldname === "enabled").default,0);
	assert.deepEqual(requests,["budget.services.ledger_sync_settings.get_sync_settings"]);
});

test("unmigrated settings hide organization mappings and do not submit their values", async () => {
	const requests=[];
	const api=create(host(async req=>{
		requests.push(req);
		return {message:{...settings,organization_review_ready:false}};
	}));
	const dialog=await api.openSettings();
	assert.equal(dialog.fields.find(f=>f.fieldname === "organization_mappings").hidden,true);
	await dialog.primary_action({corp_id:"corp-1",profile:"budget",enabled:0,organization_mappings:[]});
	assert.equal("organization_mappings" in requests[1].args,false);
	assert.equal(api.canStart({settings:{...settings,blocked_by:"schema_update_required"}}),false);
	assert.match(api.statusHTML({settings:{...settings,blocked_by:"schema_update_required"}}),/requires a database update/);
});

test("only complete enabled and idle settings can start synchronization", () => {
	const api = create({});
	assert.equal(api.canStart({settings, state:"success"}),true);
	for (const data of [null, {settings:{...settings,configured:false}}, {settings:{...settings,enabled:false}},
		{settings:{...settings,blocked_by:"source_changed"}}, {settings,state:"queued"}, {settings,state:"running"}]) {
		assert.equal(api.canStart(data),false);
	}
});

test("status clearly separates queued, failed and skipped runs and escapes source text", () => {
	const api = create({});
	assert.match(api.statusHTML({settings,state:"queued"}), /Queued/);
	assert.match(api.statusHTML({settings,state:"failed"}), /Existing ledger data was retained/);
	assert.match(api.statusHTML({settings,state:"skipped",reason:"settings_changed"}), /Settings changed/);
	assert.doesNotMatch(api.statusHTML({settings,state:"failed",last:{status:"Failed",started_at:'<script>alert(1)</script>'}}), /<script>/);
});

test("saving configuration never queues or previews sync and a failed save retains the dialog", async () => {
	const requests = []; let fail = true;
	const api = create(host(async req=>{
		requests.push(req.method.split('.').at(-1));
		if(req.method.endsWith('get_sync_settings')) return {message:settings};
		if(fail) throw Error('save failed');
		assert.equal(req.args.expected_version,'v1');
		return {message:settings};
	}));
	let refreshed=0;
	const dialog = await api.openSettings(()=>refreshed++);
	const values = {corp_id:'corp-1',profile:'budget',enabled:1};
	await dialog.primary_action(values);
	assert.equal(dialog.visible,true);
	assert.match(dialog.fields_dict.save_error.$wrapper.content,/inputs have been retained/);
	fail=false;
	await dialog.primary_action(values);
	assert.equal(dialog.visible,false);
	assert.equal(refreshed,1);
	assert.deepEqual(requests,['get_sync_settings','save_sync_settings','save_sync_settings']);
});

test("in-flight settings save blocks repeated submission and closing", async () => {
	let resolve, saves=0;
	const api = create(host(async req=>{
		if(req.method.endsWith('get_sync_settings'))return {message:settings};
		saves++;
		await new Promise(done=>resolve=done);
		return {message:settings};
	}));
	const dialog=await api.openSettings();
	const pending=dialog.primary_action({corp_id:'corp-1',profile:'budget',enabled:1});
	dialog.hide();
	await dialog.primary_action({corp_id:'corp-1',profile:'budget',enabled:1});
	assert.equal(dialog.visible,true);assert.equal(saves,1);
	resolve();await pending;assert.equal(dialog.visible,false);
});

test("opening sync reads status only and completion refreshes the list", async () => {
	const requests=[];let queued=false,refreshes=0;
	const api=create(host(async req=>{
		requests.push(req.method.split('.').at(-1));
		if(req.method.endsWith('enqueue_oa_budget_sync')){queued=true;return {message:{queued:true,job_id:'budget-ledger-sync'}};}
		return {message:{settings,state:queued?'success':'idle',ledger_count:3}};
	}));
	const dialog=await api.openSync(()=>refreshes++);
	assert.deepEqual(requests,['get_sync_state']);
	await dialog.primary_action();
	assert.deepEqual(requests,['get_sync_state','enqueue_oa_budget_sync','get_sync_state']);
	assert.equal(refreshes,1);
	dialog.hide();
});

test("closing sync cancels a pending status response without scheduling another poll", async () => {
	let resolve,reads=0;
	const h=host(async()=>{reads++;if(reads===1)return {message:{settings,state:'idle'}};return new Promise(done=>resolve=done);});
	const dialog=await create(h).openSync();
	const pending=dialog.secondary_action();
	dialog.hide();resolve({message:{settings,state:'queued'}});await pending;
	assert.equal(reads,2);
});

test("status failure disables synchronization instead of reusing stale eligibility", async () => {
	let fail=false;
	const dialog=await create(host(async()=>{
		if(fail)throw Error('offline');return {message:{settings,state:'idle'}};
	})).openSync();
	assert.equal(dialog.primary.properties.disabled,false);
	fail=true;await dialog.secondary_action();
	assert.equal(dialog.primary.properties.disabled,true);
	dialog.hide();
});

test("non-managers cannot open configuration or synchronization controls", async () => {
	const h=host(()=>assert.fail('unauthorized request'));h.frappe.session.user='Accounts User';
	const api=create(h);await api.openSettings();await api.openSync();
});

test("source transition verifies first and switches only after a second action", async () => {
	const requests = []; let refreshed = 0;
	const api = create(host(async req => {
		requests.push(req);
		return {message:req.method.endsWith("preview_shared_source") ? {token:"verified",matched:5,matched_quotas:7,additional:2} : {switched:true}};
	}));
	const dialog = api.openTransition(settings, () => refreshed++);
	assert.equal(requests.length,0);
	await dialog.primary_action();
	assert.equal(requests.length,1);
	assert.equal(refreshed,0);
	assert.equal(dialog.primary.content,"Confirm Source Switch");
	assert.match(dialog.fields_dict.status.$wrapper.content,/ledger entries and.*quotas/);
	await dialog.primary_action();
	assert.equal(requests.length,2);
	assert.deepEqual(requests[1].args,{token:"verified",expected_version:"v1"});
	assert.equal(refreshed,1);
	assert.equal(dialog.visible,false);
	assert.ok(requests.every(req => !req.method.includes("sync_from_oa")));
});

test("quota-only history offers verified source transition without starting sync", async () => {
	const requests=[];
	const api=create(host(async req=>{
		requests.push(req.method);
		return {message:{...settings,shared_oa_available:true,ledger_count:0,quota_count:2}};
	}));
	const dialog=await api.openSettings();
	assert.deepEqual(dialog.customActions.map(action=>action.label),["Verify and Switch OA Source"]);
	assert.deepEqual(requests,["budget.services.ledger_sync_settings.get_sync_settings"]);
});

test("failed source confirmation discards proof and requires a fresh verification", async () => {
	const requests = [];
	const api = create(host(async req => {
		requests.push(req.method);
		if(req.method.endsWith("confirm_shared_source")) throw Error("changed snapshot");
		return {message:{token:"one",matched:1,additional:0}};
	}));
	const dialog = api.openTransition(settings, () => assert.fail("must not report success"));
	await dialog.primary_action();await dialog.primary_action();
	assert.equal(dialog.primary.content,"Verify Source");
	assert.equal(dialog.visible,true);
	await dialog.primary_action();
	assert.ok(requests[2].endsWith("preview_shared_source"));
});

test("organization review keeps the source identity read-only and saves against the settings version", async () => {
	const requests = [];let refreshed = 0;
	const api = create(host(async req => {
		requests.push(req);
		return {message:req.method.endsWith("get_mapping_context") ? {department_id:"org1",source_name:"<old name>",version:"v2"} : {saved:true}};
	}));
	const dialog = await api.openOrganization("B1",()=>refreshed++);
	assert.equal(dialog.fields.find(f=>f.fieldname === "department_id").read_only,1);
	assert.match(dialog.fields[0].options,/&lt;old name&gt;/);
	await dialog.primary_action({company:"Company A",organization_level:"company"});
	assert.deepEqual(requests[1].args,{name:"B1",company:"Company A",department:"",organization_level:"company",expected_version:"v2"});
	assert.equal(refreshed,1);
});

test("new mapping and source-transition dialogs require manager access", async () => {
	const h=host(()=>assert.fail("unauthorized request"));h.frappe.session.user="Accounts User";
	const api=create(h);await api.openOrganization("B1");api.openTransition(settings,()=>{});
});
