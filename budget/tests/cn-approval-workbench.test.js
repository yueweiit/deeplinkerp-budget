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
	assert.deepEqual(data,[{month:"2026-11",values:[{kind:"budget",amount:100},{kind:"operating",amount:null},{kind:"purchase",amount:0}]}]);
});

test("late page responses cannot replace a newer selection",async()=>{
	const pending=[];const work=Object.create(ui.Workbench.prototype);
	Object.assign(work,{seq:0,active:true,filters:{kind:"budget"},view:"register",start:0,host:{querySelector:()=>null},render(){},call(method,args){return new Promise(resolve=>pending.push({resolve,args}));}});
	const first=work.load();work.filters.kind="purchase";const second=work.load();
	pending[1].resolve({filters:{kind:"purchase"},rows:["purchase"]});await second;
	pending[0].resolve({filters:{kind:"budget"},rows:["budget"]});await first;
	assert.deepEqual(work.data.rows,["purchase"]);assert.equal(work.filters.kind,"purchase");
});

test("leaving a page discards pending data and never reports an empty success",async()=>{
	let finish;const work=Object.create(ui.Workbench.prototype);
	Object.assign(work,{seq:0,active:true,filters:{},view:"overview",start:0,detailSeq:0,host:{querySelector:()=>null},render(){},call(){return new Promise(resolve=>finish=resolve);}});
	const request=work.load();work.hide();finish({filters:{},rows:[]});await request;
	assert.equal(work.data,undefined);assert.equal(work.active,false);
});

test("unknown currency never acquires a CNY symbol",()=>{
 assert.match(ui.formatMoney('125.00',null),/Unknown currency/);
 assert.doesNotMatch(ui.formatMoney('125.00',null),/CNY|¥/);
});

test("refresh calls the read-only refresh contract without pagination arguments",async()=>{
 const work=Object.create(ui.Workbench.prototype);let request;
 Object.assign(work,{seq:0,active:true,filters:{kind:'budget'},view:'overview',start:0,render(){},async call(method,args){request={method,args};return {filters:args.filters,rows:[]}}});
 await work.load(true);assert.equal(request.method,'refresh_data');assert.equal('start' in request.args,false);
});

test("an unrecognized currency remains unknown in the monthly chart",()=>{
 const result=ui.chartData([{month:'2026-11',kind:'operating',totals:[{currency:'unknown',amount:null}]}],'CNY');
 assert.equal(result[0].values.find(row=>row.kind==='operating').amount,null);
});

function deferredWorkbench() {
	const pending=[];
	const work=Object.create(ui.Workbench.prototype);
	Object.assign(work,{seq:0,detailSeq:0,active:true,loading:false,view:"register",start:0,filters:{kind:"budget",entity:"A"},render(){},call(method,args){return new Promise((resolve,reject)=>pending.push({method,args,resolve,reject}));}});
	return {work,pending};
}

for (const change of [
	work=>{work.filters.kind="purchase";},
	work=>{work.filters.entity="B";},
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

test("interrupted explicit refresh remains a refresh when returning to the page",async()=>{
	const {work,pending}=deferredWorkbench();
	const initial=work.load();pending[0].resolve({filters:pending[0].args.filters,rows:["old"]});await initial;
	const interrupted=work.load(true);work.hide();work.show();
	assert.equal(pending[2].method,"refresh_data");
	pending[1].resolve({filters:pending[1].args.filters,rows:["ignored"]});await interrupted;
	pending[2].resolve({filters:pending[2].args.filters,rows:["fresh"]});await new Promise(resolve=>setImmediate(resolve));
	assert.deepEqual(work.data.rows,["fresh"]);assert.equal(work.pendingRefresh,false);
});
