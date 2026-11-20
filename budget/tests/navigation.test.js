const {test}=require('node:test');
const assert=require('node:assert/strict');
const vm=require('node:vm');
const fs=require('node:fs');
const path=require('node:path');
const script=fs.readFileSync(path.join(__dirname,'../public/js/budget_navigation.js'),'utf8');
const plain=value=>JSON.parse(JSON.stringify(value));
test('relocated budget pages discard obsolete sidebar ownership only',()=>{
    let stored=JSON.stringify({'Budget Ledger Entry':['Budget'],'budget-dashboard':['Budget','Personal'],'budget-config':['China Operating Expenses'],Budget:['Budget'],'Cost Center':['Budget']});
    const storage={getItem:()=>stored,setItem:(_,v)=>stored=v};
    vm.runInNewContext(script,{localStorage:storage});
    assert.deepEqual(JSON.parse(stored),{'budget-dashboard':['Personal'],'budget-config':['China Operating Expenses'],Budget:['Budget'],'Cost Center':['Budget']});
    const first=stored;vm.runInNewContext(script,{localStorage:storage});assert.equal(first,stored);
});
test('unavailable or corrupted browser storage does not block native navigation',()=>{
    assert.doesNotThrow(()=>vm.runInNewContext(script,{localStorage:{getItem(){throw Error('blocked')}}}));
    assert.doesNotThrow(()=>vm.runInNewContext(script,{localStorage:{getItem:()=>'{invalid'}}));
});

function navigation({enabled=true, search='', hash='', storage, tabs}={}) {
    const calls=[], nativeCalls=[];
    const listeners=new Map();
    const frappe={
        boot:{cn_budget_workbench:enabled},
        route_options:null, route_flags:{custom:true},
        set_route(route) {
            calls.push(JSON.parse(JSON.stringify({
                route, options:this.route_options, flags:this.route_flags, hash:this.route_hash,
            })));
        },
    };
    frappe.router={
        re_route(...args) { nativeCalls.push({receiver:this,args}); return 'native'; },
        set_route_options_from_url() {
            frappe.route_options ||= {};
            for (const [key,value] of new URLSearchParams(context.window.location.search)) {
                try { frappe.route_options[key]=JSON.parse(value); }
                catch { frappe.route_options[key]=value; }
            }
        },
    };
    const context=vm.createContext({
        frappe, window:{location:{search,hash},CustomFiltersDeskTabs:tabs,
            addEventListener(name,listener) { listeners.set(name,listener); }},
        localStorage:storage || {getItem:()=>null}, URLSearchParams,
    });
    vm.runInContext(script,context);
    return {context,frappe,calls,nativeCalls,
        restoreTabs(controller) { listeners.get('custom-filters:desk-tabs-restored')?.({detail:controller}); }};
}

test('retired CN URLs resolve to canonical pages before native Page rendering',()=>{
    const {frappe,calls,nativeCalls}=navigation();
    for (const route of ['budget-dashboard','budget-config/','china-personnel-report']) {
        assert.equal(frappe.router.re_route(route),true);
    }
    assert.deepEqual(calls.map(call=>call.route),[
        'china-budget-execution','china-budget-execution','china-management-reports',
    ]);
    assert.equal(calls[2].options.cn_report,'hr');
    assert.ok(calls.every(call=>call.flags.replace_route && call.flags.custom));
    assert.equal(nativeCalls.length,0);
});

test('initial legacy bookmarks preserve current filters, other parameters and hash',()=>{
    const filters={from_month:'2026-09',to_month:'2026-11',company:'A & B',
        reporting_currency:'USD',management_category:'payroll'};
    const params=new URLSearchParams({
        cn_approval_filters:JSON.stringify(filters),custom:'keep me',
        cn_filters:JSON.stringify({budget_tab:'quota'}),
    });
    const {frappe,calls}=navigation({search:'?'+params,hash:'#company'});
    frappe.router.re_route('budget-dashboard');
    assert.deepEqual(calls[0].options,{cn_approval_filters:filters,custom:'keep me'});
    assert.equal(calls[0].hash,'#company');
});

test('returning to a cached destination still applies the incoming legacy URL filters',()=>{
    const {frappe,calls,context}=navigation();
    frappe.router.current_sub_path='china-budget-execution';
    frappe.route_options={cn_approval_filters:{from_month:'2026-10',to_month:'2026-10'}};
    context.window.location.search='?'+new URLSearchParams({
        cn_approval_filters:JSON.stringify({from_month:'2026-11',to_month:'2026-11'}),
    });
    frappe.router.re_route('budget-dashboard');
    assert.equal(calls.length,1);
    assert.equal(calls[0].options.cn_approval_filters.from_month,'2026-11');
    assert.equal(calls[0].flags.replace_route,true);
});

test('canonical, MX, native ledger and form routes keep native navigation',()=>{
    const {frappe,calls,nativeCalls}=navigation();
    for (const route of ['china-budget-execution','china-expenses','mexico-expenses',
        'budget-ledger-entry','budget-ledger-entry/BL-001','Budget Ledger Entry','budget-config/other']) {
        assert.equal(frappe.router.re_route(route),'native');
    }
    assert.equal(calls.length,0);
    assert.equal(nativeCalls.length,7);
    assert.ok(nativeCalls.every(call=>call.receiver===frappe.router));
});

test('standalone Budget pages retain their native implementation',()=>{
    const {frappe,calls,nativeCalls}=navigation({enabled:false});
    assert.equal(frappe.router.cn_legacy_routes_installed,undefined);
    assert.equal(frappe.router.re_route('budget-dashboard'),'native');
    assert.equal(calls.length,0);
    assert.equal(nativeCalls.length,1);
});

test('navigation registration is idempotent and independent of sidebar storage',()=>{
    const {frappe,context,calls,nativeCalls}=navigation({
        storage:{getItem(){throw Error('blocked');}},
    });
    const handler=frappe.router.re_route;
    vm.runInContext(script,context);
    assert.equal(frappe.router.re_route,handler);
    frappe.router.re_route('budget-config');
    assert.equal(calls.length,1);
    frappe.boot.cn_budget_workbench=false;
    assert.equal(frappe.router.re_route('budget-config'),'native');
    assert.equal(nativeCalls.length,1);
});

test('Desk anchor serialization preserves filter objects through retired URLs',()=>{
    const filters={from_month:'2026-10',to_month:'2026-10',company:'A & B',reporting_currency:'USD'};
    // Native body click reads each href query value as text; set_route then
    // JSON-stringifies it into the URL before re_route sees it.
    const href=new URLSearchParams({cn_approval_filters:JSON.stringify(filters),custom:'keep'});
    const pushed=new URLSearchParams([...href].map(([key,value])=>[key,JSON.stringify(value)]));
    const {frappe,calls}=navigation({search:'?'+pushed});
    frappe.router.re_route('budget-dashboard');
    assert.deepEqual(calls[0].options,{cn_approval_filters:filters,custom:'keep'});
});

test('malformed serialized filters are not passed to the workbench as hidden state',()=>{
    for (const value of ['{broken','null','[]','false','12','"text"']) {
        const {frappe,calls}=navigation({search:'?'+new URLSearchParams({
            cn_approval_filters:JSON.stringify(value),custom:'keep',
        })});
        assert.doesNotThrow(()=>frappe.router.re_route('budget-config'));
        assert.equal(calls[0].options.cn_approval_filters,undefined);
        assert.equal(calls[0].options.custom,'keep');
    }
});

function tab(route,extra={}) {
    return {id:route,route:[route],route_key:route,title:route,type:'page',
        pinned:false,last_accessed:1,...extra};
}
function tabController(tabs,active=null,{rendered=false}={}) {
    return {
        state:{tabs,active_route_key:active},last_route_key:active,
        container:rendered?{}:null,renderCount:0,persistCount:0,
        update_tab_from_route(tab,route) {
            Object.assign(tab,{id:route.join('/'),route,route_key:route.join('/'),
                title:'translated:'+route[0],type:'page',doctype:null,docname:null});
        },
        render() { this.renderCount++; },
        persist() { this.persistCount++;this.saved=JSON.parse(JSON.stringify(this.state)); },
    };
}

test('restored old tabs merge into canonical tabs and retain pinned state and ordering',()=>{
    const unrelated=tab('Form/Purchase Order/new-order',{
        route:['Form','Purchase Order','new-order'],type:'form_new',title:'Unsaved order',pinned:true,
    });
    const mx=tab('mexico-expenses');
    const canonical=tab('china-budget-execution',{title:'Current title',last_accessed:10});
    const controller=tabController([
        unrelated,tab('budget-dashboard',{pinned:true,last_accessed:30}),mx,
        tab('budget-config',{last_accessed:20}),canonical,
        tab('china-personnel-report',{pinned:true}),
    ],'budget-config');
    const env=navigation();
    env.restoreTabs(controller);
    assert.deepEqual(plain(controller.state.tabs.map(t=>t.route_key)),[
        unrelated.route_key,'china-budget-execution','mexico-expenses','china-management-reports',
    ]);
    assert.equal(controller.state.tabs[0],unrelated);
    assert.equal(controller.state.tabs[2],mx);
    assert.deepEqual(plain(controller.state.tabs[1]),{...canonical,pinned:true,last_accessed:30});
    assert.equal(controller.state.tabs[3].title,'translated:china-management-reports');
    assert.equal(controller.state.tabs[3].pinned,true);
    assert.equal(controller.state.active_route_key,'china-budget-execution');
    assert.equal(controller.last_route_key,'china-budget-execution');
    assert.equal(controller.renderCount,0); // restore precedes initial DOM creation
    assert.equal(controller.persistCount,1);
    const first=JSON.stringify(controller.saved);
    env.restoreTabs(controller);
    assert.equal(controller.persistCount,1);
    assert.equal(JSON.stringify(controller.state),first);
});

test('already loaded tabs migrate once and preserve canonical metadata when aliases follow',()=>{
    const canonical=tab('china-budget-execution',{title:'Budget',pinned:true,last_accessed:20});
    const controller=tabController([
        canonical,tab('budget-config',{last_accessed:35}),tab('budget-dashboard',{pinned:true}),
    ],'budget-dashboard',{rendered:true});
    const {frappe}=navigation({tabs:controller});
    assert.deepEqual(plain(controller.state.tabs),[{...canonical,last_accessed:35}]);
    assert.equal(controller.renderCount,1);
    assert.equal(controller.persistCount,1);
    frappe.router.re_route('budget-dashboard');
    assert.equal(controller.persistCount,1);
});

test('retired personnel and existing report-center tabs are merged without touching other routes',()=>{
    const native=tab('Budget Ledger Entry',{route:['List','Budget Ledger Entry'],type:'list'});
    const detail=tab('budget-config/detail',{route:['budget-config','detail']});
    const report=tab('china-management-reports',{pinned:true});
    const controller=tabController([native,report,detail,tab('china-personnel-report')],'china-personnel-report');
    navigation().restoreTabs(controller);
    assert.deepEqual(plain(controller.state.tabs),[native,report,detail]);
    assert.equal(controller.state.active_route_key,'china-management-reports');
});

test('standalone Budget never migrates its native tabs',()=>{
    const controller=tabController([tab('budget-dashboard'),tab('budget-config')],'budget-dashboard');
    const before=JSON.stringify(controller.state);
    const env=navigation({enabled:false,tabs:controller});
    env.restoreTabs(controller);
    assert.equal(JSON.stringify(controller.state),before);
    assert.equal(controller.persistCount,0);
});
