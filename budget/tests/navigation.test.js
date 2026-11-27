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

const unifiedRoutes = {
    'china-expenses':'expense-management', 'mexico-expenses':'expense-management',
    'china-budget-execution':'expense-budget', 'budget-config':'expense-budget',
    'china-expense-details':'expense-details', 'mexico-expense-details':'expense-details',
    'china-expense-review':'expense-review', 'mexico-expense-review':'expense-review',
    'china-personnel-report':'expense-analysis', 'mexico-personnel-report':'expense-analysis',
    'mexico-expense-reports':'expense-analysis',
};
function navigation({enabled=true, unified=false, journal=false, search='', hash='', storage, tabs, retiredSidebars=[]}={}) {
    const calls=[], nativeCalls=[], replacements=[];
    const listeners=new Map();
    const frappe={
        boot:{cn_budget_workbench:enabled,unified_expense_management:unified,expense_cash_journal:journal,
            expense_legacy_routes:unified?unifiedRoutes:{},expense_retired_sidebars:retiredSidebars},
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
        frappe, window:{location:new URL('https://erp.example/desk/china-cash-journal/'+search+hash),
            history:{state:{tab:'journal'},replaceState(state,title,url) {
                replacements.push({state,url});
                context.window.location=new URL(url,context.window.location);
            }},CustomFiltersDeskTabs:tabs,
            addEventListener(name,listener) { listeners.set(name,listener); }},
        localStorage:storage || {getItem:()=>null}, URLSearchParams,
    });
    vm.runInContext(script,context);
    return {context,frappe,calls,nativeCalls,replacements,
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

test('unified expenses retire CN and MX URLs before rendering and avoid redirect chains',()=>{
    const {frappe,calls,nativeCalls}=navigation({unified:true});
    for (const [old,target] of Object.entries(unifiedRoutes)) {
        assert.equal(frappe.router.re_route(old+'/'),true);
        assert.equal(calls.at(-1).route,target);
        assert.equal(calls.at(-1).options.cn_report,undefined);
        assert.equal(calls.at(-1).flags.replace_route,true);
    }
    assert.equal(nativeCalls.length,0);
    for (const route of ['expense-management','expense-analysis','operating-expenses',
        'china-cash-journal','budget-ledger-entry','china-expenses/other']) {
        assert.equal(frappe.router.re_route(route),'native');
    }
});

test('regional bookmarks retain company and last month but not old accounting filters',()=>{
    for (const [route,key,filters] of [
        ['china-expenses','cn_approval_filters',{from_month:'2026-09',to_month:'2026-11',company:'A & B'}],
        ['mexico-expense-reports','mx_filters',{from_date:'2026-09-01',to_date:'2026-11-30',company:'A & B'}],
    ]) {
        for (const serialized of [JSON.stringify(filters),JSON.stringify(JSON.stringify(filters))]) {
            const params=new URLSearchParams({[key]:serialized,cn_report:'hr',mx_group:'trend',
                custom:'keep',expense_filters_unused:'unused'});
            const {frappe,calls}=navigation({unified:true,search:'?'+params,hash:'#company'});
            frappe.route_options={expense_filters:{company:'Stale',month:'2025-01'},mx_dashboard:1};
            frappe.router.re_route(route);
            assert.deepEqual(calls[0].options.expense_filters,{month:'2026-11',company:'A & B'});
            assert.equal(calls[0].options.cn_report,undefined);
            assert.equal(calls[0].options.mx_group,undefined);
            assert.equal(calls[0].options.mx_dashboard,undefined);
            assert.equal(calls[0].options.custom,'keep');
            assert.equal(calls[0].hash,'#company');
        }
    }
});

test('malformed old filters and bare links cannot reuse previous tab company or month',()=>{
    for (const value of ['{broken','[]','null','false','{"month":"2026-99","company":5}']) {
        const {frappe,calls}=navigation({unified:true,search:'?'+new URLSearchParams({mx_filters:value})});
        frappe.route_options={mx_filters:{company:'Old'},expense_filters:{company:'Stale'}};
        frappe.router.re_route('mexico-expenses');
        assert.deepEqual(calls[0].options.expense_filters,{company:''});
    }
    const {frappe,calls}=navigation({unified:true});
    frappe.route_options={mx_filters:{company:'Old'},expense_filters:{company:'Stale'}};
    frappe.router.re_route('mexico-expenses');
    assert.deepEqual(calls[0].options.expense_filters,{company:''});
});

test('explicit unified filters survive old bookmarks with only supported keys',()=>{
    const values={month:'2026-10',company:'A',region:'cn',category:'service',source_category:'missing',measure:'approved',country:'Mexico'};
    const {frappe,calls}=navigation({unified:true,search:'?'+new URLSearchParams({expense_filters:JSON.stringify(values)})});
    frappe.router.re_route('china-expense-details');
    delete values.country;
    assert.deepEqual(calls[0].options.expense_filters,values);
});

test('retired CN and MX tabs merge into unified tabs without losing pins or unrelated forms',()=>{
    const native=tab('Customer/CUST-1',{route:['Form','Customer','CUST-1']});
    const canonical=tab('expense-management',{title:'Current title',last_accessed:10});
    const controller=tabController([native,tab('mexico-expenses',{pinned:true,last_accessed:30}),
        tab('china-expenses'),canonical,tab('budget-config')],'mexico-expenses');
    const env=navigation({unified:true});
    env.restoreTabs(controller);
    assert.deepEqual(plain(controller.state.tabs),[native,{...canonical,pinned:true,last_accessed:30},
        {...tab('expense-budget'),title:'translated:expense-budget',doctype:null,docname:null}]);
    assert.equal(controller.state.active_route_key,'expense-management');
    assert.equal(controller.last_route_key,'expense-management');
    env.restoreTabs(controller);
    assert.equal(controller.persistCount,1);
});

test('unified navigation clears only retired remembered sidebars and remains idempotent',()=>{
    let stored=JSON.stringify({'expense-management':['Mexico Expenses','Personal'],
        'mexico-expenses':['Mexico Expenses'],Customer:['China Operating Expenses','Selling'],Budget:['Budget']});
    const {frappe,context,calls}=navigation({unified:true,storage:{getItem:()=>stored,setItem:(_,v)=>stored=v}});
    assert.deepEqual(JSON.parse(stored),{'expense-management':['Personal'],Customer:['Selling'],Budget:['Budget']});
    const handler=frappe.router.re_route;
    vm.runInContext(script,context);
    assert.equal(frappe.router.re_route,handler);
    frappe.router.re_route('mexico-expenses');
    assert.equal(calls.length,1);
});

test('legacy desktop links use the unified sidebar after native sidebar dispatch',()=>{
    for (const [route,sidebar] of [['mexico-expenses','Mexico Expenses'],
        ['china-expenses','China Operating Expenses'],['mexico-expense-details','My Expense Shortcuts']]) {
        for (const value of [sidebar,JSON.stringify(sidebar)]) {
            const {frappe,calls}=navigation({unified:true,search:'?'+new URLSearchParams({sidebar:value,custom:'keep'})});
            frappe.router.re_route(route);
            const options=calls[0].options;
            // Sidebar.setup_events gives route_options.sidebar precedence over
            // route-based resolution; only the unified sidebar is available.
            const sidebars={'expense management':{items:[{link_to:calls[0].route}]}};
            assert.deepEqual(sidebars[options.sidebar.toLowerCase()].items,[{link_to:calls[0].route}]);
            assert.equal(options.sidebar,'Expense Management');
            assert.equal(options.custom,'keep');
        }
    }
});

test('sidebar overrides stay unchanged outside unified legacy redirects',()=>{
    const old=navigation({search:'?'+new URLSearchParams({sidebar:'China Operating Expenses'})});
    old.frappe.router.re_route('budget-config');
    assert.equal(old.calls[0].options.sidebar,'China Operating Expenses');
    const current=navigation({unified:true});
    current.frappe.route_options={sidebar:'Personal'};
    assert.equal(current.frappe.router.re_route('expense-management'),'native');
    assert.equal(current.frappe.route_options.sidebar,'Personal');
});

test('sidebar choices emptied by retirement are removed without clearing personal preferences',()=>{
    const initial=JSON.stringify({'expense-management':['MY Expense Shortcuts','Personal'],
        Customer:['My Expense Shortcuts','Selling'],'china-expenses':['China Operating Expenses'],
        'operating-expenses':['Mixed Expenses'],'my workspaces':['My Workspaces']});
    for (const unified of [false,true]) {
        let stored=initial;
        navigation({unified,retiredSidebars:['my expense shortcuts'],
            storage:{getItem:()=>stored,setItem:(_,v)=>stored=v}});
        if (!unified) assert.equal(stored,initial);
        else assert.deepEqual(JSON.parse(stored),{'expense-management':['Personal'],Customer:['Selling'],
            'operating-expenses':['Mixed Expenses'],'my workspaces':['My Workspaces']});
    }
});

test('authorized journal bookmarks repair retired sidebar without changing the journal route or filters',()=>{
    for (const sidebar of ['', 'China Operating Expenses', JSON.stringify('China Operating Expenses'), 'Expense Shortcuts']) {
        const params=new URLSearchParams({company:'A & B',currency:'MXN'});
        if (sidebar) params.set('sidebar',sidebar);
        const {frappe,calls,context,nativeCalls,replacements}=navigation({unified:true,journal:true,
            search:'?'+params,hash:'#row',retiredSidebars:['expense shortcuts']});
        assert.equal(frappe.router.re_route('china-cash-journal/'),'native');
        const {location, history}=context.window;
        assert.equal(location.pathname,'/desk/china-cash-journal/');
        assert.deepEqual(Object.fromEntries(location.searchParams),
            {company:'A & B',currency:'MXN',sidebar:'Expense Management'});
        assert.equal(location.hash,'#row');
        assert.equal(replacements[0].state,history.state);
        // Native push_state reenters routing synchronously and can drop route options.
        assert.equal(calls.length,0);
        assert.equal(frappe.router.re_route('china-cash-journal/'),'native');
        assert.equal(nativeCalls.length,2);
        assert.equal(replacements.length,1);
    }
});

test('unavailable journal and intentional custom sidebars retain native routing',()=>{
    for (const options of [{unified:false,journal:true},{unified:true,journal:false},
        {unified:true,journal:true,search:'?sidebar=Personal'}]) {
        const {frappe,calls}=navigation(options);
        assert.equal(frappe.router.re_route('china-cash-journal'),'native');
        assert.equal(frappe.router.re_route('china-cash-journal/other'),'native');
        assert.equal(calls.length,0);
    }
});
