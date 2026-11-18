const {test}=require('node:test');
const assert=require('node:assert/strict');
const vm=require('node:vm');
const fs=require('node:fs');
const path=require('node:path');
const script=fs.readFileSync(path.join(__dirname,'../public/js/budget_navigation.js'),'utf8');
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
