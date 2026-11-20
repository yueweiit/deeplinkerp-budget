const test=require('node:test');
const assert=require('node:assert/strict');
const ui=require('../public/js/cn_approval_workbench.js');
const management=require('../public/js/cn_approval_management.js');
function fixture(view='overview') {
 const metrics={budget:'100.00',operating:'60.00',purchase:'20.00',approved:'80.00',pending:'30.00',balance:'20.00',forecast_balance:'-10.00',execution_rate:'80',flags:['forecast_over'],count:4,approval_count:2,pending_count:1,budget_count:1,review_count:0,superseded_count:1};
 const company={company:'A',...metrics},month={...company,month:'2026-10'};
 return {view,filters:{},data:{filters:{company:'',from_month:'2026-10',to_month:'2026-10',reporting_currency:'CNY'},options:{management_categories:{salary:'Salaries'}},management:{totals:{...metrics,over_budget_companies:0,forecast_over_companies:1},companies:[company],company_months:[month],category_months:[{...month,management_category:'salary'}],categories:[{...metrics,management_category:'salary'}],months:[month],top_spending:[],quality:{count:4,unassigned:0,unclassified:0}}}};
}
test('each management page has distinct content instead of repeated approval cards',()=>{
 for(const view of ['overview','budget','analysis']) {
  const html=management.render(fixture(view));
  assert.doesNotMatch(html,/cnaw-card/);
  assert.equal(html.includes('cnam-kpis'),view==='overview');
  assert.equal(html.includes('cnam-budget-group'),view==='budget');
  assert.equal(html.includes('cnam-rankings'),view==='analysis');
 }
});
test('company labels are escaped and money links carry exact company and measure',()=>{
 const work=fixture();work.data.management.companies[0].company='<img src=x onerror=evil>';
 const html=management.companyTable(work);
 assert.doesNotMatch(html,/<img/);assert.match(html,/&lt;img/);
 assert.match(html,/&quot;measure&quot;:&quot;purchase&quot;/);
});
test('no effective budget never receives a normal budget status',()=>{
 assert.match(management.flags({count:1,budget:null,flags:[]}),/No effective budget/);
 assert.doesNotMatch(management.flags({count:1,budget:null,flags:[]}),/Within budget/);
 assert.match(management.flags({count:1,budget:'0.00',flags:[]}),/Within budget/);
});
test('purchase report pending drilldowns preserve kind, exact project and category',()=>{
 const work=fixture('reports');Object.assign(work.data,{report_id:'purchase',title:'Purchase expenditure report',categories:{},quality:{count:2,unassigned:0,unclassified:0},columns:[{fieldname:'pending',fieldtype:'Currency',label:'Pending'}],rows:[{company:'A',month:'2026-10',management_category:'it',project:'<A>',pending:'4'}]});
 const html=management.reports(work);
 assert.match(html,/&quot;kind&quot;:&quot;purchase&quot;/);
 assert.match(html,/&quot;project&quot;:&quot;&lt;A&gt;&quot;/);
 assert.match(html,/&quot;management_category&quot;:&quot;it&quot;/);
 work.data.rows[0].project='';assert.match(management.reports(work),/&quot;no_project&quot;:&quot;1&quot;/);
});
test('drilldown preserves current category and unassigned scope and displays filter chips',()=>{
 const work=Object.create(ui.Workbench.prototype);let selection;
 Object.assign(work,{view:'analysis',data:{filters:{management_category:'salary',unassigned:'1',company:''}},navigate(view,filters){selection={view,filters};}});
 work.click({target:{closest:()=>({dataset:{mgmtNav:'register',mgmtFilters:'{"measure":"approved"}'}})}});
 assert.equal(selection.filters.management_category,'salary');assert.equal(selection.filters.unassigned,'1');
});
test('reports ignore stale response after switching report selection',async()=>{
 const requests=[],work=Object.create(ui.Workbench.prototype);
 Object.assign(work,{view:'reports',active:true,seq:0,start:0,filters:work.defaultFilters(),reportId:'company',render(){},call(method,args){if(method==='get_filter_options')return Promise.resolve({});return new Promise(resolve=>requests.push({resolve,args,method}));}});
 const first=work.load();work.reportId='purchase';const second=work.load();
 assert.equal(requests[1].method,'get_management_report');assert.equal(requests[1].args.report_id,'purchase');assert.equal(requests[1].args.start,undefined);
 requests[1].resolve({report_id:'purchase',filters:work.filters});await second;requests[0].resolve({report_id:'company',filters:work.filters});await first;
 assert.equal(work.data.report_id,'purchase');
});
test('unverified and retired template history is visibly distinguished',()=>{
 const work=fixture();work.data.missing_history_templates=2;
 assert.match(management.qualityBanner(work),/2 disabled templates have no verified/);
 work.data.missing_history_templates=0;work.data.retained_history=[{fetched_at:'2026-10-01'}];
 assert.match(management.qualityBanner(work),/later OA changes are not included/);
});
test('incomplete current months are not presented as a decline against a complete month',()=>{
 const work=fixture('analysis');work.data.comparisons={reason:'open_period'};
 assert.match(management.comparisons(work),/Select completed months/);
 assert.doesNotMatch(management.comparisons(work),/NaN|Infinity/);
});

for (const completion of ['old-first','old-last','old-error']) {
 test(`same-page report route invalidates an in-flight request: ${completion}`,async(t)=>{
  const prior=global.frappe;t.after(()=>{global.frappe=prior;});global.frappe={route_options:{}};
  const requests=[],work=Object.create(ui.Workbench.prototype);
  Object.assign(work,{view:'reports',active:true,seq:0,start:0,filters:work.defaultFilters(),reportId:'company',
   render(){},updateNavigation(){},syncHistory(){},loadFilterOptions(){},
   call(method,args){return new Promise((resolve,reject)=>requests.push({resolve,reject,args,method}));}});
  const first=work.load();
  global.frappe.route_options.cn_report='purchase';work.show();
  assert.deepEqual(requests.map(r=>r.args.report_id),['company','purchase']);
  const old=()=>completion==='old-error'?requests[0].reject(new Error('Old request failed')):requests[0].resolve({report_id:'company',filters:work.filters});
  if(completion==='old-first'){old();await first;assert.equal(work.data,undefined);assert.equal(work.loading,true);}
  requests[1].resolve({report_id:'purchase',filters:work.filters});await new Promise(resolve=>setImmediate(resolve));
  if(completion!=='old-first'){old();await first;}
  assert.equal(work.data.report_id,'purchase');assert.equal(work.reportId,'purchase');
  assert.equal(work.resultKey,work.selectionKey());assert.equal(work.error,'');assert.equal(work.loading,false);
  global.frappe.route_options.cn_report='purchase';work.show();assert.equal(requests.length,2);
 });
}

test('unclassified comparisons cannot display a green within-budget status',()=>{
 const html=management.flags({count:2,budget:'1000.00',balance:null,flags:['incomplete']});
 assert.match(html,/Incomplete data/);assert.doesNotMatch(html,/Within budget/);
});
