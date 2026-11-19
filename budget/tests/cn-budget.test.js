const test = require('node:test');
const assert = require('node:assert/strict');
const factory = require('../public/js/cn_budget.js');
const shared = {monthRange:month => ({from_date:month+'-01',to_date:month+(month === '2024-02' ? '-29' : '-31')})};

test('budget normalizes to full months without mixing operating currency or status', () => {
    const api=factory({});
    const result=api.normalize({from_date:'2024-02-21',to_date:'2024-03-04',currency:'USD',status:'pending',company:'A'},shared);
    assert.deepEqual(result,{from_date:'2024-02-01',to_date:'2024-03-31',currency:'CNY',status:'approved',company:'A',basis:'period'});
});

test('budget configuration and sync reuse budget actions and refresh the current view', () => {
    const calls=[];const wb={refresh:()=>calls.push('refresh')};
    const api=factory({BudgetLedgerSync:{openSettings:cb=>{calls.push('settings');cb();},openSync:cb=>{calls.push('sync');cb();}}});
    api.sync(wb,true);api.sync(wb,false);
    assert.deepEqual(calls,['settings','refresh','sync','refresh']);
});

test('leaving a budget view discards delayed company results without painting or fetching records', async () => {
    let release; const requests=[];
    const api=factory({frappe:{call:args=>{requests.push(args.method);return new Promise(resolve=>release=resolve);}}});
    const wb={generation:1,isCurrentRoute:()=>true,disposeFilterControls(){},el:{innerHTML:''}};
    const pending=api.render(wb,1);wb.generation=2;wb.el.innerHTML='operating';release({message:{companies:[],can_read_quota:false}});await pending;
    assert.equal(wb.el.innerHTML,'operating');assert.equal(wb.budgetCompanies,undefined);
    assert.deepEqual(requests,['budget.api.cn_workbench.get_options']);
});
