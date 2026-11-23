const test=require("node:test");
const assert=require("node:assert/strict");
const ui=require("../public/js/cn_approval_workbench.js");

test("source labels are escaped in list rows and totals preserve unknown and zero",()=>{
	const row={key:"a".repeat(64),kind:"operating",number:'<img src=x onerror="evil()">',entity:"<script>bad</script>",applicant:"A",company:"",month:"2026-11",application_date:"2026-11-01",category:"",classification:[],amount:"0.00",currency:"CNY",status:"approved",issues:[]};
	const html=ui.tableHTML([row],{});
	assert.ok(!html.includes("<script>") && !html.includes("<img"));
	assert.match(html,/&lt;script&gt;/);
	assert.notEqual(ui.formatMoney("0.00","CNY"),ui.formatMoney(null,"CNY"));
	assert.match(ui.totalsHTML([{currency:"CNY",amount:null,incomplete:1}]),/Needs review/);
});

test("only official approval protocol links are clickable",()=>{
	for(const value of ["javascript:alert(1)","https://evil.test/","data:text/html,evil",null])assert.equal(ui.safeApprovalURL(value),"");
	assert.equal(ui.safeApprovalURL("dingtalk://dingtalkclient/action/openapp?x=1"),"dingtalk://dingtalkclient/action/openapp?x=1");
});

test("monthly chart never merges currencies or budget and expenditure",()=>{
	const data=ui.chartData([{month:"2026-11",kind:"budget",totals:[{currency:"CNY",amount:"100.00"}]},{month:"2026-11",kind:"operating",totals:[{currency:"USD",amount:"500.00"},{currency:"CNY",amount:null}]}],"CNY");
	assert.deepEqual(data,[{month:"2026-11",values:[{kind:"budget",amount:"100.00"},{kind:"operating",amount:null},{kind:"purchase",amount:0}]}]);
});

test("late page responses cannot replace a newer selection",async()=>{
	const pending=[];const work=Object.create(ui.Workbench.prototype);
	Object.assign(work,{seq:0,active:true,filters:{kind:"budget"},view:"register",start:0,host:{querySelector:()=>null},render(){},call(method,args){if(method==="get_filter_options")return Promise.resolve({companies:[],reporting_currencies:["CNY","USD","MXN"]});return new Promise(resolve=>pending.push({resolve,args}));}});
	const first=work.load();work.filters.kind="purchase";const second=work.load();
	pending[1].resolve({filters:{kind:"purchase"},rows:["purchase"]});await second;
	pending[0].resolve({filters:{kind:"budget"},rows:["budget"]});await first;
	assert.deepEqual(work.data.rows,["purchase"]);assert.equal(work.filters.kind,"purchase");
});

test("leaving a page discards pending data and never reports an empty success",async()=>{
	let finish;const work=Object.create(ui.Workbench.prototype);
	Object.assign(work,{seq:0,active:true,filters:{},view:"overview",start:0,detailSeq:0,host:{querySelector:()=>null},render(){},call(method){if(method==="get_filter_options")return Promise.resolve({companies:[],reporting_currencies:["CNY","USD","MXN"]});return new Promise(resolve=>finish=resolve);}});
	const request=work.load();work.hide();finish({filters:{},rows:[]});await request;
	assert.equal(work.data,undefined);assert.equal(work.active,false);
});

test("unknown currency never acquires a CNY symbol",()=>{
 assert.match(ui.formatMoney('125.00',null),/Unknown currency/);
 assert.doesNotMatch(ui.formatMoney('125.00',null),/CNY|¥/);
});

test("refresh queues background work without sending filters or pagination",async()=>{
 const work=Object.create(ui.Workbench.prototype);let request;
 Object.assign(work,{seq:0,active:true,filters:{kind:'budget'},view:'overview',start:0,render(){},async call(method,args){if(method==="get_filter_options")return Promise.resolve({companies:[],reporting_currencies:["CNY","USD","MXN"]});request={method,args};return {filters:args.filters,rows:[]}}});
 await work.load(true);assert.equal(request.method,'enqueue_sync');assert.deepEqual(request.args,{if_stale:0});work.hide();
});

test("an unrecognized currency remains unknown in the monthly chart",()=>{
 const result=ui.chartData([{month:'2026-11',kind:'operating',totals:[{currency:'unknown',amount:null}]}],'CNY');
 assert.equal(result[0].values.find(row=>row.kind==='operating').amount,null);
});

function deferredWorkbench() {
	const pending=[];
	const work=Object.create(ui.Workbench.prototype);
	Object.assign(work,{seq:0,detailSeq:0,active:true,loading:false,view:"register",start:0,filters:{kind:"budget",company:"A"},render(){},call(method,args){if(method==="get_filter_options")return Promise.resolve({companies:[],reporting_currencies:["CNY","USD","MXN"]});return new Promise((resolve,reject)=>pending.push({method,args,resolve,reject}));}});
	return {work,pending};
}

for (const change of [
	work=>{work.filters.kind="purchase";},
	work=>{work.filters.company="B";},
	work=>{work.filters.from_month="2026-10";},
	work=>{work.start=25;},
	work=>{work.view="review";},
]) {
	test(`returning after an interrupted selection reloads: ${change}`,async()=>{
		const {work,pending}=deferredWorkbench();
		const initial=work.load();pending[0].resolve({filters:pending[0].args.filters,rows:["old"]});await initial;
		change(work);
		const interrupted=work.load();work.hide();work.show();
		assert.equal(pending.length,3);assert.equal(work.loading,true);
		assert.deepEqual(pending[2].args,pending[1].args);
		pending[1].resolve({filters:pending[1].args.filters,rows:["discarded"]});await interrupted;
		assert.deepEqual(work.data.rows,["old"]);
		pending[2].resolve({filters:pending[2].args.filters,rows:["current"]});await new Promise(resolve=>setImmediate(resolve));
		assert.deepEqual(work.data.rows,["current"]);assert.equal(work.needsReload,false);
		work.hide();work.show();assert.equal(pending.length,3,"completed matching result can be reused");
	});
}

test("returning before the search debounce fires loads the changed keyword",async()=>{
	const {work,pending}=deferredWorkbench();
	const initial=work.load();pending[0].resolve({filters:pending[0].args.filters,rows:["old"]});await initial;
	work.start=25;work.search("new number");work.hide();work.show();
	assert.equal(pending.length,2);assert.equal(pending[1].args.filters.keyword,"new number");
	assert.equal(pending[1].args.start,0,"search resets pagination even when its debounce is interrupted");
	pending[1].resolve({filters:pending[1].args.filters,rows:["matched"]});await new Promise(resolve=>setImmediate(resolve));
	assert.deepEqual(work.data.rows,["matched"]);
});

test("returning after an interrupted enqueue checks status instead of repeating the command",async()=>{
 const {work,pending}=deferredWorkbench();
 const state={source_id:"source",snapshot_id:"old",status:"success",active:false,auto_sync_due:false,enabled:true};
 const initial=work.load();pending[0].resolve({filters:pending[0].args.filters,rows:["old"],snapshot_id:"old",sync:state});await initial;
 const interrupted=work.load(true);work.hide();work.show();
 assert.equal(pending[2].method,"get_sync_status");
 pending[1].resolve({...state,status:"queued",active:true});await interrupted;
 assert.deepEqual(work.data.rows,["old"]);
 pending[2].resolve({...state,status:"running",active:true});await flushRequests();
 assert.equal(work.syncState.status,"running");assert.equal(work.syncSubmitting,false);work.hide();
});

test("view navigation changes the Page route and carries filters to the destination",t=>{
	const previous=global.frappe; t.after(()=>{global.frappe=previous;});
	let destination;
	global.frappe={set_route(route){destination=route;}};
	const work=Object.create(ui.Workbench.prototype);
	Object.assign(work,{route:"china-expense-reports",view:"analysis",detailSeq:0,filters:{kind:"budget",from_month:"2026-10",issue:"amount",category:""}});
	work.navigate("register",{category:"Office",status:"approved"});
	assert.equal(destination,"china-expense-details");
	assert.equal(work.view,"analysis","source instance stays associated with its own route");
	assert.deepEqual(global.frappe.route_options.cn_approval_filters,{kind:"budget",from_month:"2026-10",issue:"",category:"Office",status:"approved"});
	const target=Object.create(ui.Workbench.prototype);let loads=0;
	Object.assign(target,{filters:{kind:"",from_month:"",issue:"",category:"",status:""},start:25,view:"register",load(){loads++;}});
	global.frappe.route_options.cn_approval_filters.unauthorized="ignored";
	target.show();
	assert.equal(loads,1);assert.equal(target.start,0);assert.equal(target.filters.category,"Office");
	assert.equal(target.filters.from_month,"2026-10");assert.equal(target.filters.unauthorized,undefined);
	assert.equal(global.frappe.route_options.cn_approval_filters,undefined);
});

test("navigation within the current register resets paging without a second route",t=>{
	const previous=global.frappe;t.after(()=>{global.frappe=previous;});
	global.frappe={set_route(){assert.fail("must remain on the same Page");}};
	let loads=0;const work=Object.create(ui.Workbench.prototype);
	Object.assign(work,{route:"china-expense-details",view:"register",filters:{issue:"amount"},start:25,detailSeq:0,load(){loads++;}});
	work.navigate("register",{category:"Travel"});
	assert.equal(work.start,0);assert.equal(work.filters.issue,"");assert.equal(work.filters.category,"Travel");assert.equal(loads,1);
});

test("CN navigation installs once and breadcrumbs delegate unrelated routes to the existing renderer",()=>{
	const vm=require("node:vm"),fs=require("node:fs");
	let route="china-expense-reports",delegated=0;const crumbs=[],listeners=[];
	const breadcrumbs={set_custom_breadcrumbs(){delegated++;},append_breadcrumb_element(...args){crumbs.push(args);},$breadcrumbs:{append(item){crumbs.push(item);}}};
	const context={frappe:{boot:{cn_budget_workbench:true,page_info:{"china-expense-reports":{title:"Old report"}}},get_route:()=>[route],breadcrumbs},document:{addEventListener(...args){listeners.push(args);},createElement(tag){return {tag,children:[],setAttribute(k,v){this[k]=v;},appendChild(node){this.children.push(node);}};}}};
	vm.runInNewContext(fs.readFileSync(require.resolve("../public/js/cn_approval_workbench.js"),"utf8"),context);
	context.CNApprovalWorkbench.installNavigation();const installed=breadcrumbs.set_custom_breadcrumbs;
	context.CNApprovalWorkbench.installNavigation();assert.equal(breadcrumbs.set_custom_breadcrumbs,installed);
	assert.equal(listeners.length,1);assert.equal(listeners[0][0],"click");assert.equal(listeners[0][2],true);
	assert.equal(context.frappe.boot.page_info[route].title,"Expenditure Analysis");
	breadcrumbs.set_custom_breadcrumbs({label:"Old report"});
	assert.equal(crumbs[0][0],"/desk/china-expenses");assert.equal(crumbs[1][1],"Management Analysis");
	assert.equal(crumbs[2].children[0].textContent,"Expenditure Analysis");assert.equal(crumbs[2].children[0]["aria-current"],"page");
	route="mexico-expenses";breadcrumbs.set_custom_breadcrumbs({});assert.equal(delegated,1);
	route="china-expenses";context.frappe.boot.cn_budget_workbench=false;breadcrumbs.set_custom_breadcrumbs({});assert.equal(delegated,2);
});

function browserHistory(t) {
	const previous={frappe:global.frappe,history:global.history,location:global.location};
	t.after(()=>Object.assign(global,previous));
	const replacements=[],state={desk:"preserved"};
	global.location={href:'https://erp.test/desk/china-expense-details?other=keep#details'};
	global.history={state,replaceState(value,title,url){assert.equal(value,state);replacements.push(url);global.location.href=url;}};
	global.frappe={get_route:()=>["china-expense-details"],datetime:{get_today:()=>"2026-11-20"}};
	return {replacements,filters:()=>JSON.parse(new URL(global.location.href).searchParams.get("cn_approval_filters"))};
}

test("filter changes, reset and interrupted search replace the current history entry",async t=>{
	const history=browserHistory(t),work=Object.create(ui.Workbench.prototype);
	Object.assign(work,{active:true,route:"china-expense-details",view:"register",seq:0,detailSeq:0,start:0,
		filters:{kind:"budget",status:"",company:"",keyword:"",category:"Office",issue:"",review:false},render(){},
		async call(method,args){if(method==="get_filter_options")return Promise.resolve({companies:[],reporting_currencies:["CNY","USD","MXN"]});return {filters:args.filters,rows:[]};}});
	t.after(()=>work.hide());
	work.change({target:{dataset:{filter:"status"},value:"approved"}});
	await new Promise(resolve=>setImmediate(resolve));assert.equal(history.filters().status,"approved");
	work.click({target:{closest:()=>({dataset:{kind:"purchase"}})}});
	await new Promise(resolve=>setImmediate(resolve));
	assert.equal(history.filters().kind,"purchase");assert.equal(history.filters().category,"");
	assert.equal(history.filters().review,undefined,"review remains a property of the view");
	work.search("latest keyword");work.hide();
	assert.equal(history.filters().keyword,"latest keyword","save before the debounce or navigation interrupts it");
	global.frappe.route_options={cn_approval_filters:history.filters()};
	work.show();await new Promise(resolve=>setImmediate(resolve));
	assert.equal(work.filters.kind,"purchase");assert.equal(work.filters.keyword,"latest keyword");
	work.click({target:{closest:()=>({dataset:{action:"reset"}})}});
	await new Promise(resolve=>setImmediate(resolve));
	assert.equal(history.filters().kind,"");assert.equal(history.filters().keyword,"");
	assert.equal(history.filters().from_month,"2026-11");
	const url=new URL(global.location.href);assert.equal(url.searchParams.get("other"),"keep");assert.equal(url.hash,"#details");
	assert.ok(history.replacements.length>0);
});

test("normalized server filters are saved without touching a different Page history",async t=>{
	const history=browserHistory(t),{work,pending}=deferredWorkbench();work.route="china-expense-details";
	work.filters.keyword=" term ";const load=work.load();
	pending[0].resolve({filters:{...pending[0].args.filters,keyword:"term"},rows:[]});await load;
	assert.equal(history.filters().keyword,"term");
	const before=history.replacements.length;
	work.hide();work.filters.keyword="hidden";work.syncHistory();
	work.active=true;global.location.href='https://erp.test/desk/china-expense-reports';work.syncHistory();
	assert.equal(history.replacements.length,before,"outgoing instance cannot overwrite the destination URL");
});

for (const stale of ["success-first","failure-last"]) {
	test(`same-route selection invalidates its pending request: ${stale}`,async t=>{
		browserHistory(t);const {work,pending}=deferredWorkbench();work.route="china-expense-details";
		const old=work.load();global.frappe.route_options={cn_approval_filters:{...work.filters,kind:"purchase"}};
		work.show();assert.equal(pending.length,2);assert.equal(pending[1].args.filters.kind,"purchase");
		if (stale==="success-first") {
			pending[0].resolve({filters:pending[0].args.filters,rows:["budget"]});await old;
			assert.equal(work.filters.kind,"purchase");assert.equal(work.loading,true);assert.equal(work.data,undefined);
		}
		pending[1].resolve({filters:pending[1].args.filters,rows:["purchase"]});
		await new Promise(resolve=>setImmediate(resolve));
		if (stale==="failure-last") {pending[0].reject(new Error("old request failed"));await old;}
		assert.equal(work.filters.kind,"purchase");assert.deepEqual(work.data.rows,["purchase"]);
		assert.equal(work.needsReload,false);assert.equal(work.error,"");
		assert.equal(JSON.parse(new URL(global.location.href).searchParams.get("cn_approval_filters")).kind,"purchase");
	});
}

test("showing identical route filters preserves the in-flight request and completed page",async t=>{
	browserHistory(t);const {work,pending}=deferredWorkbench();work.route="china-expense-details";work.start=25;
	const load=work.load();global.frappe.route_options={cn_approval_filters:{...work.filters}};
	work.show();assert.equal(pending.length,1);assert.equal(work.start,25);
	pending[0].resolve({filters:pending[0].args.filters,rows:["page two"]});await load;
	global.frappe.route_options={cn_approval_filters:{...work.filters}};
	work.show();assert.equal(pending.length,1);assert.equal(work.start,25);assert.equal(work.needsReload,false);
});

test("returning to a cached Page restores its URL filters without reloading or resetting paging",t=>{
	const history=browserHistory(t),work=Object.create(ui.Workbench.prototype);let renders=0;
	Object.assign(work,{route:"china-expense-details",view:"register",active:false,start:25,
		filters:{from_month:"2026-09",to_month:"2026-10",kind:"operating",company:"A",keyword:"travel",issue:""},
		data:{rows:["cached"]},render(){renders++;},load(){assert.fail("cached result must be reused");}});
	work.resultKey=work.selectionKey();
	work.show();
	assert.equal(renders,1);assert.equal(work.start,25);assert.deepEqual(history.filters(),work.filters);
	assert.equal(new URL(global.location.href).searchParams.get("other"),"keep");
	assert.equal(new URL(global.location.href).hash,"#details");
	assert.equal(history.replacements.length,1);
	work.show();assert.equal(history.replacements.length,1,"repeated show does not rewrite matching URL");
});

function sidebarClick(href="https://erp.test/desk/china-expense-reports",attributes={},properties={}) {
	const link={href,target:attributes.target || "",hasAttribute:key=>Object.hasOwn(attributes,key)};
	return {button:0,defaultPrevented:false,target:{closest:()=>link},
		preventDefault(){assert.fail("native link handling must continue");},
		stopPropagation(){assert.fail("native sidebar events must continue");},...properties};
}

test("CN sidebar navigation carries current filters into cold and cached destination Pages",t=>{
	const history=browserHistory(t),work=Object.create(ui.Workbench.prototype);
	Object.assign(work,{active:true,route:"china-expense-details",view:"register",start:25,
		filters:{from_month:"2026-09",to_month:"2026-10",kind:"operating",company:"A",status:"approved",currency:"CNY",keyword:"travel",category:"Office",issue:"structure",review:false}});
	Object.assign(global.frappe,{boot:{cn_budget_workbench:true},pages:{"china-expense-details":{cn_expenses:work}},app:{sidebar:{editor:{edit_mode:false}}}});
	for (const cached of [false,true]) {
		global.location.href="https://erp.test/desk/china-expense-details";
		global.frappe.get_route=()=>["china-expense-details"];
		ui.prepareSidebarNavigation(sidebarClick());
		const passed=global.frappe.route_options.cn_approval_filters;
		assert.deepEqual(passed,{...work.routeFilters(),issue:""});
		assert.equal(history.filters().issue,"structure","source history keeps its own review selection");
		const target=Object.create(ui.Workbench.prototype);let loads=0;
		Object.assign(target,{route:"china-expense-reports",view:"analysis",seq:0,start:cached ? 25 : 0,
			filters:Object.fromEntries(Object.keys(passed).map(key=>[key,""])),data:cached ? {rows:["old"]} : null,load(){loads++;}});
		target.resultKey=target.selectionKey();
		global.location.href="https://erp.test/desk/china-expense-reports";
		global.frappe.get_route=()=>["china-expense-reports"];
		target.show();
		assert.equal(loads,1);assert.equal(target.start,0);assert.deepEqual(target.filters,{...passed,kind:"",status:"",category:"",keyword:""});
		assert.equal(global.frappe.route_options.cn_approval_filters,undefined);
	}
});

test("sidebar filter handoff leaves explicit links, browser gestures and unrelated navigation alone",t=>{
	browserHistory(t);
	const work={active:true,syncHistory(){assert.fail("must not change source URL");}};
	Object.assign(global.frappe,{boot:{cn_budget_workbench:true},pages:{"china-expense-details":{cn_expenses:work}},app:{sidebar:{editor:{edit_mode:false}}}});
	const original={existing:"preserved"};global.frappe.route_options=original;
	for (const properties of [{ctrlKey:true},{metaKey:true},{shiftKey:true},{altKey:true},{button:1},{defaultPrevented:true},{target:{closest:()=>null}}]) ui.prepareSidebarNavigation(sidebarClick(undefined,{},properties));
	for (const attributes of [{target:"_blank"},{target:"_top"},{download:""},{onclick:"custom()"}]) ui.prepareSidebarNavigation(sidebarClick(undefined,attributes));
	for (const href of ["https://other.test/desk/china-expense-reports","https://erp.test/desk/mexico-expenses","https://erp.test/desk/china-expense-details","https://erp.test/desk/china-expense-reports?cn_approval_filters=explicit","https://erp.test/desk/china-expense-reports#section","https://erp.test/other/china-expense-reports","http://["]) ui.prepareSidebarNavigation(sidebarClick(href));
	global.frappe.app.sidebar.editor.edit_mode=true;ui.prepareSidebarNavigation(sidebarClick());
	global.frappe.app.sidebar.editor.edit_mode=false;work.active=false;ui.prepareSidebarNavigation(sidebarClick());
	work.active=true;global.frappe.boot.cn_budget_workbench=false;ui.prepareSidebarNavigation(sidebarClick());
	global.frappe.boot.cn_budget_workbench=true;global.frappe.get_route=()=>["mexico-expenses"];ui.prepareSidebarNavigation(sidebarClick());
	assert.equal(global.frappe.route_options,original);
});

test("shared month picker keeps CN month-only filters and ignores changes after page leave",t=>{
	const previous={frappe:global.frappe,DeepLinkERPMexicoExpenseDateRange:global.DeepLinkERPMexicoExpenseDateRange};
	t.after(()=>Object.assign(global,previous));
	let settings,loads=0,destroyed=0;
	global.frappe={datetime:{get_today:()=>"2026-11-20"},get_route:()=>["china-expenses"]};
	global.DeepLinkERPMexicoExpenseDateRange={wholeMonths(from,to){assert.equal(from,"2026-11");assert.equal(to,"2026-11");return {from_date:"2026-11-01",to_date:"2026-11-30"};},RangePicker:class{constructor(options){settings=options;this.trigger={focus(){}};}destroy(){destroyed++;}}};
	const work=Object.create(ui.Workbench.prototype);
	Object.assign(work,{route:"china-expenses",active:true,start:25,seq:0,detailSeq:0,host:{querySelector:()=>({})},filters:{from_month:"2026-11",to_month:"2026-11",kind:"operating"},load(){loads++;}});
	work.mountRangePicker();assert.equal(settings.mode,"month");
	settings.onChange({from_date:"2026-12-01",to_date:"2027-02-28"});
	assert.deepEqual(work.filters,{from_month:"2026-12",to_month:"2027-02",kind:"operating"});assert.equal(work.start,0);assert.equal(loads,1);
	work.hide();assert.equal(destroyed,1);
	settings.onChange({from_date:"2024-01-01",to_date:"2024-02-29"});assert.equal(loads,1);assert.equal(work.filters.from_month,"2026-12");
});

test("subsidiary and reporting currency persist through navigation, refresh and reset",async t=>{
 const previous=global.frappe;t.after(()=>{global.frappe=previous;});
 global.frappe={route_options:{cn_approval_filters:{company:'Allowed Co',reporting_currency:'USD',currency:'MXN'}}};
 const work=Object.create(ui.Workbench.prototype);let request;
 Object.assign(work,{seq:0,active:true,filters:work.defaultFilters(),view:'overview',start:25,render(){},async call(method,args){if(method==="get_filter_options")return Promise.resolve({companies:[],reporting_currencies:["CNY","USD","MXN"]});request={method,args};return {filters:args.filters,rows:[]};}});
 work.show();await new Promise(resolve=>setImmediate(resolve));
 assert.equal(request.args.filters.company,'Allowed Co');assert.equal(request.args.filters.reporting_currency,'USD');assert.equal(request.args.filters.currency,'');assert.equal(work.start,0);assert.equal(request.args.start,undefined);
 assert.equal(work.navigationFilters().company,'Allowed Co');assert.equal(work.routeFilters().reporting_currency,'USD');
 await work.load(true);assert.equal(request.method,'enqueue_sync');assert.equal(work.filters.reporting_currency,'USD');assert.equal(work.filters.company,'Allowed Co');
 work.click({target:{closest(){return {dataset:{action:'reset'}};}}});await new Promise(resolve=>setImmediate(resolve));
 assert.equal(request.args.filters.company,'');assert.equal(request.args.filters.reporting_currency,'CNY');
});

test("legacy original-currency bookmarks become visible reporting-currency choices",async t=>{
 const previous=global.frappe;t.after(()=>{global.frappe=previous;});
 global.frappe={route_options:{cn_approval_filters:{currency:'MXN'}}};
 const work=Object.create(ui.Workbench.prototype);
 Object.assign(work,{seq:0,active:true,filters:work.defaultFilters(),view:'overview',start:0,render(){},async call(method,args){if(method==="get_filter_options")return Promise.resolve({companies:[],reporting_currencies:["CNY","USD","MXN"]});return {filters:args.filters,rows:[]};}});
 work.show();await new Promise(resolve=>setImmediate(resolve));
 assert.equal(work.filters.reporting_currency,'MXN');assert.equal(work.filters.currency,'');
 const html=work.selectFilter('reporting_currency','Reporting Currency',[{value:'MXN',label:'MXN'}],null);
 assert.doesNotMatch(html,/value=""/);assert.match(html,/MXN" selected/);
});

test("converted table shows both currencies and never replaces incomplete values with zero",()=>{
 const row={key:'a'.repeat(64),kind:'purchase',number:'A',entity:'Org',company:'Co',month:'2026-11',application_date:'2026-11-20',category:'Services',classification:[],amount:'100.00',currency:'USD',reporting_currency:'CNY',converted_amount:'700.00',status:'approved',issues:[]};
 const html=ui.tableHTML([row],{});assert.match(html,/Converted amount/);assert.match(html,/Original amount/);assert.match(html,/CNY/);assert.match(html,/USD/);assert.match(html,/700/);
 assert.match(ui.tableHTML([{...row,converted_amount:null}],{}),/Needs review/);
 assert.equal(ui.chartData([{month:'2026-11',kind:'purchase',totals:[{currency:'CNY',amount:'19999999999999.99'}]}],'CNY')[0].values[2].amount,'19999999999999.99');
});

test("failed currency changes keep the previous chart in its actual reporting currency",()=>{
 const work=Object.create(ui.Workbench.prototype);
 work.filters={reporting_currency:'MXN'};
 work.data={filters:{reporting_currency:'USD'},options:{currencies:['CNY']}};
 const html=work.trend({months:[]});assert.match(html,/USD/);assert.doesNotMatch(html,/MXN/);
});

test("validation errors retain the public rate message without exposing internal traces",()=>{
 const message='Month-start exchange rate unavailable: USD on 2026-11-01';
 assert.equal(ui.userError({responseJSON:{exc_type:'ValidationError',_server_messages:JSON.stringify([JSON.stringify({message})]),exception:'internal secret'}},'Fallback'),message);
 for(const responseJSON of [{exc_type:'ValidationError',_server_messages:'invalid',exception:'secret'},{exc_type:'RuntimeError',exception:'secret'}])assert.equal(ui.userError({responseJSON},'Fallback'),'Fallback');
});

test("CN large monetary labels reuse MX exact formatting without losing cents",t=>{
 const previous={frappe:global.frappe,document:global.document,money:global.DeepLinkERPMexicoExpenseMoney};
 t.after(()=>{global.frappe=previous.frappe;global.document=previous.document;global.DeepLinkERPMexicoExpenseMoney=previous.money;});
 global.frappe={boot:{lang:'en'}};
 global.document={createElement(){return {set innerHTML(value){this.textContent=value;}};}};
 const calls=[];global.DeepLinkERPMexicoExpenseMoney={format(value,currency){calls.push({value,currency});return (currency ? currency+' ' : '')+'19,999,999,999,999.99';}};
 assert.equal(ui.formatMoney('19999999999999.99','CNY'),'CNY 19,999,999,999,999.99');
 assert.equal(ui.formatMoney('19999999999999.99',null),'19,999,999,999,999.99 · Unknown currency');
 assert.deepEqual(calls,[{value:'19999999999999.99',currency:'CNY'},{value:'19999999999999.99',currency:null}]);
});

function independentOptionsWorkbench() {
 const requests=[];const work=Object.create(ui.Workbench.prototype);
 Object.assign(work,{seq:0,detailSeq:0,active:true,loading:false,view:'overview',start:0,filters:work.defaultFilters(),render(){},call(method,args){return new Promise((resolve,reject)=>requests.push({method,args,resolve,reject}));}});
 return {work,requests};
}
const flushRequests=()=>new Promise(resolve=>setImmediate(resolve));

for(const order of ['options-first','error-first'])test(`first FX failure retains independent subsidiaries: ${order}`,async()=>{
 const {work,requests}=independentOptionsWorkbench();const load=work.load();
 assert.deepEqual(requests.map(r=>r.method),['get_filter_options','get_management']);
 const options={companies:['Allowed Co'],reporting_currencies:['CNY','MXN','USD']};
 if(order==='options-first'){requests[0].resolve(options);await flushRequests();}
 requests[1].reject(new Error('rate unavailable'));await load;
 if(order==='error-first'){requests[0].resolve(options);await flushRequests();}
 assert.equal(work.data,undefined);assert.ok(work.error);assert.deepEqual(work.filterOptions,options);
 work.change({target:{dataset:{filter:'company'},value:'Allowed Co'}});
 assert.equal(requests.length,3,'successful options are reused when switching subsidiary');
 assert.equal(requests[2].args.filters.company,'Allowed Co');
 requests[2].resolve({filters:requests[2].args.filters,rows:[],options});await flushRequests();
 assert.equal(work.error,'');assert.equal(work.filters.company,'Allowed Co');
});

test('a successful overview supersedes an older options request',async()=>{
 const {work,requests}=independentOptionsWorkbench();const load=work.load();
 const options={companies:['Current Co'],reporting_currencies:['CNY','USD','MXN'],can_manage_settings:true};
 requests[1].resolve({filters:requests[1].args.filters,rows:[],options});await load;
 requests[0].resolve({companies:['Stale Co']});await flushRequests();
 assert.deepEqual(work.filterOptions,options);assert.equal(work.optionsLoading,false);assert.equal(work.optionsError,'');
});

test('leaving and returning discards stale options and retries an interrupted request',async()=>{
 const {work,requests}=independentOptionsWorkbench();const first=work.load();
 work.hide();work.show();assert.equal(requests.length,4);
 requests[0].resolve({companies:['Stale Co']});requests[1].reject(new Error('old data failure'));await first;await flushRequests();
 assert.equal(work.filterOptions,undefined);assert.equal(work.error,'');assert.equal(work.optionsLoading,true);
 requests[2].resolve({companies:['Current Co'],reporting_currencies:['CNY']});await flushRequests();
 requests[3].reject(new Error('current FX failure'));await flushRequests();
 assert.deepEqual(work.filterOptions.companies,['Current Co']);assert.equal(work.optionsLoading,false);
});

test('options failures can be retried without another approval or rate query',async()=>{
 const {work,requests}=independentOptionsWorkbench();const load=work.load();
 requests[0].reject(new Error('company query failed'));requests[1].reject(new Error('rate unavailable'));await load;await flushRequests();
 assert.ok(work.optionsError);assert.ok(work.error);
 work.click({target:{closest(){return {dataset:{action:'retry-options'}};}}});
 assert.deepEqual(requests.map(r=>r.method),['get_filter_options','get_management','get_filter_options']);
 requests[2].resolve({companies:['Allowed Co'],reporting_currencies:['CNY']});await flushRequests();
 assert.equal(work.optionsError,'');assert.ok(work.error,'retrying options must not hide the failed financial result');
 assert.deepEqual(work.filterOptions.companies,['Allowed Co']);
});

test('manual refresh reloads permission-scoped subsidiary options and ignores old failures',async()=>{
 const {work,requests}=independentOptionsWorkbench();const first=work.load();const refresh=work.load(true);
 assert.deepEqual(requests.map(r=>r.method),['get_filter_options','get_management','get_filter_options','enqueue_sync']);
 requests[2].resolve({companies:['Current Co'],reporting_currencies:['CNY']});await flushRequests();
 requests[3].reject(new Error('current rate unavailable'));await refresh;
 requests[0].reject(new Error('old options failure'));requests[1].reject(new Error('old data failure'));await first;await flushRequests();
 assert.deepEqual(work.filterOptions.companies,['Current Co']);assert.equal(work.optionsError,'');assert.equal(work.optionsLoading,false);
});

test('workbench calls retain error rejection while suppressing duplicate server message dialogs',async t=>{
 const previous=global.frappe;t.after(()=>{global.frappe=previous;});
 const failure={responseJSON:{exc_type:'ValidationError'}};let request;
 global.frappe={call:async options=>{request=options;throw failure;}};
 const work=Object.create(ui.Workbench.prototype);
 await assert.rejects(work.call('get_filter_options',{}),error=>error===failure);
 assert.equal(request.silent,true);assert.equal(request.method,'budget.api.cn_approvals.get_filter_options');
 global.frappe.call=async options=>{request=options;return {message:{companies:['Allowed Co']}};};
 assert.deepEqual(await work.call('get_filter_options',{}),{companies:['Allowed Co']});
});

function syncWorkbench() {
 const {work,pending}=deferredWorkbench();
 const state={source_id:'source',snapshot_id:'old',status:'success',active:false,enabled:true,auto_sync_due:false,has_snapshot:true};
 work.data={filters:{...work.filters},rows:['old'],sync:state,snapshot_id:'old'};
 work.syncState=state;work.resultKey=work.selectionKey();
 return {work,pending,state};
}

test('background refresh preserves local results and reads again only after a new publication',async()=>{
 const {work,pending,state}=syncWorkbench();
 const command=work.refreshApprovals();
 assert.equal(work.loading,false);assert.deepEqual(work.data.rows,['old']);
 pending[0].resolve({...state,status:'queued',active:true});await command;
 assert.equal(pending.length,1);assert.deepEqual(work.data.rows,['old']);
 const running=work.pollSync();pending[1].resolve({...state,status:'running',active:true,completed_templates:2,template_count:11,page_count:5});await running;
 assert.equal(pending.length,2);assert.match(work.syncHTML(),/2 of 11 templates, 5 pages/);
 const done=work.pollSync();pending[2].resolve({...state,snapshot_id:'new'});await done;
 assert.equal(pending[3].method,'get_management_lines');assert.deepEqual(pending[3].args.filters,{...work.filters,review:false});
 pending[3].resolve({filters:work.filters,rows:['new'],snapshot_id:'new',sync:{...state,snapshot_id:'new'}});await flushRequests();
 assert.deepEqual(work.data.rows,['new']);work.hide();
});

test('automatic stale refresh is a separate nonblocking command with retry backoff',async()=>{
 const {work,pending,state}=syncWorkbench();
 work.acceptSync({...state,auto_sync_due:true});
 assert.equal(pending[0].method,'enqueue_sync');assert.equal(pending[0].args.if_stale,1);
 assert.deepEqual(work.data.rows,['old']);
 pending[0].reject(new Error('offline'));await flushRequests();
 assert.ok(work.syncError);
 work.acceptSync({...state,auto_sync_due:true});assert.equal(pending.length,1);
 work.hide();assert.equal(work.syncTimer?._destroyed,true);
});

test('failed or delayed sync never clears the previous data or claims success',async()=>{
 const {work,pending,state}=syncWorkbench();
 const polling=work.pollSync();pending[0].resolve({...state,status:'failed',error_code:'sync_timeout'});await polling;
 assert.deepEqual(work.data.rows,['old']);assert.equal(pending.length,1);
 assert.match(work.syncHTML(),/timed out/);work.hide();
});

test('hiding discards a delayed status response and stops all polling',async()=>{
 const {work,pending,state}=syncWorkbench();
 const polling=work.pollSync();work.hide();
 pending[0].resolve({...state,snapshot_id:'new'});await polling;
 assert.deepEqual(work.data.rows,['old']);assert.equal(pending.length,1);assert.equal(work.syncState.snapshot_id,'old');
});

test('old overview responses cannot overwrite a newer background sync status',async()=>{
 const {work,pending,state}=syncWorkbench();
 const load=work.load();const refresh=work.refreshApprovals();
 pending[1].resolve({...state,status:'running',active:true});await refresh;
 pending[0].resolve({filters:work.filters,rows:['old'],snapshot_id:'old',sync:state});await load;
 assert.equal(work.syncState.status,'running');work.hide();
});

test('a published snapshot with a failed FX reread is not retried on every status poll',async()=>{
 const {work,pending,state}=syncWorkbench();
 const first=work.pollSync();pending[0].resolve({...state,snapshot_id:'new'});await first;
 pending[1].reject(new Error('rate unavailable'));await flushRequests();
 assert.ok(work.error);assert.deepEqual(work.data.rows,['old']);
 const second=work.pollSync();pending[2].resolve({...state,snapshot_id:'new'});await second;
 assert.equal(pending.length,3);work.hide();
});

test('a page left open can refresh again when the server reports stale data after the backoff',async()=>{
 const {work,pending,state}=syncWorkbench();
 work.autoSyncAttempted=Date.now()-16*60*1000;
 work.acceptSync({...state,auto_sync_due:true});
 assert.equal(pending.length,1);assert.equal(pending[0].method,'enqueue_sync');
 pending[0].resolve({...state,status:'queued',active:true});await flushRequests();work.hide();
});

test('polling updates progress and failure notices without rebuilding filters or the open picker',async()=>{
 const {work,pending,state}=syncWorkbench();
 const feedback={innerHTML:''},button={disabled:false,textContent:''};
 const popup={selectedStart:'2026-10'},picker={popup,destroy(){throw new Error('open picker destroyed by status update');}};
 work.rangePicker=picker;
 work.host={querySelector(selector){return selector==='.cnaw-sync-feedback' ? feedback : button;},set innerHTML(_){throw new Error('filters rebuilt by status update');}};
 work.render=()=>assert.fail('status changes must not render the entire page');
 const states=[
  {...state,status:'queued',active:true},
  {...state,status:'running',active:true,completed_templates:2,template_count:11,page_count:5},
  {...state,status:'failed',active:false,error_code:'sync_timeout'},state,
 ];
 for(const [index,next] of states.entries()) {
  const polling=work.pollSync();pending[index].resolve(next);await polling;
  assert.strictEqual(work.rangePicker,picker);assert.strictEqual(work.rangePicker.popup,popup);
  assert.equal(button.disabled,next.active);
  assert.equal(feedback.innerHTML,work.syncHTML());
 }
 assert.equal(feedback.innerHTML,'');assert.match(button.textContent,/Refresh approvals/);
 work.rangePicker=null;work.hide();
});

test('poll errors and automatic enqueue update only synchronization controls',async()=>{
 const {work,pending,state}=syncWorkbench();
 const feedback={innerHTML:''},button={};
 work.host={querySelector(selector){return selector==='.cnaw-sync-feedback' ? feedback : button;}};
 work.render=()=>assert.fail('synchronization must not render filters');
 const polling=work.pollSync();pending[0].reject(new Error('status offline'));await polling;
 assert.match(feedback.innerHTML,/Synchronization status could not be loaded/);
 const refresh=work.refreshApprovals(true);
 assert.equal(button.disabled,true);assert.equal(pending[1].args.if_stale,1);
 pending[1].resolve({...state,status:'queued',active:true});await refresh;
 assert.match(feedback.innerHTML,/queued/);assert.equal(button.disabled,true);assert.equal(work.syncError,'');
 work.hide();
});

test('a data render retains an open month draft until the committed month filters change',()=>{
 const {work}=syncWorkbench();
 Object.assign(work.filters,{from_month:'2026-10',to_month:'2026-11'});
 const parent={},popup={selectedStart:'2026-09'};
 let destroyed=0,mounted=0,reattached=0,positioned=0;
 const picker={parent,popup,value:{from_date:'2026-10-01',to_date:'2026-11-30'},trigger:{},destroy(){destroyed++;},position(){positioned++;}};
 work.rangePicker=picker;
 work.host={innerHTML:'',contains(){return false;},querySelector(){return {replaceWith(node){assert.strictEqual(node,parent);reattached++;}};}};
 work.mountRangePicker=()=>{mounted++;};work.mountApprovalSidebar=()=>{};work.content=()=>'<div>updated snapshot</div>';
 ui.Workbench.prototype.render.call(work);
 assert.strictEqual(work.rangePicker,picker);assert.strictEqual(work.rangePicker.popup,popup);
 assert.equal(destroyed,0);assert.equal(mounted,0);assert.equal(reattached,1);assert.equal(positioned,1);
 work.filters.from_month='2026-09';
 ui.Workbench.prototype.render.call(work);
 assert.equal(destroyed,1);assert.equal(mounted,1);assert.equal(reattached,1);
 work.hide();
});


test('a company mapping revision reloads local data without a new OA snapshot',()=>{
 const work=Object.create(ui.Workbench.prototype);let reads=0;
 Object.assign(work,{active:true,data:{snapshot_id:'same',sync:{source_id:'source',settings_version:'old'}},load(){reads++;},scheduleSyncPoll(){}});
 work.acceptSync({source_id:'source',snapshot_id:'same',settings_version:'new',auto_sync_due:false},true);
 assert.equal(reads,1);
 work.acceptSync({source_id:'source',snapshot_id:'same',settings_version:'new',auto_sync_due:false},true);
 assert.equal(reads,1);
});

test('automatic ownership rebuild waits locally and reloads once complete without starting OA synchronization',()=>{
 const work=Object.create(ui.Workbench.prototype);let reads=0,polls=0;
 Object.assign(work,{active:true,data:{snapshot_id:'same',sync:{source_id:'source',allocation_revision:'same'}},
  load(){reads++;},scheduleSyncPoll(){polls++;},refreshApprovals(){assert.fail('local ownership rebuild must not start OA synchronization');}});
 const state={source_id:'source',snapshot_id:'same',allocation_revision:'same'};
 work.acceptSync({...state,allocation_rebuilding:true,auto_sync_due:true},true);
 assert.equal(reads,0);assert.equal(polls,1);
 work.acceptSync({...state,allocation_rebuilding:false,auto_sync_due:false},true);
 assert.equal(reads,1);
 work.acceptSync({...state,allocation_rebuilding:false,auto_sync_due:false},true);
 assert.equal(reads,1);
});
