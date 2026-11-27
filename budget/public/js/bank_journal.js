// Frappe appends this page hook after the native China cash journal script.
(function (root, factory) {
    const api = factory(root);
    if (typeof module !== "undefined" && module.exports) module.exports = api;
    if (root.frappe?.pages?.["china-cash-journal"]) api.install(root.frappe.pages["china-cash-journal"]);
})(globalThis, function (root) {
    "use strict";
    const columns = [["date","日期 / 钉钉单号"],["category","类别"],["level2","二级明细"],["level3","三级明细"],
        ["project","所属项目"],["summary","摘要"],["income","收入"],["expense","支出"],["balance","余额"]];
    const esc = v => String(v ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
    const amountText = value => {
        if (value == null || value === "") return "—";
        const parts = String(value).split(".");
        return parts[0].replace(/\B(?=(\d{3})+(?!\d))/g,",")+"."+(parts[1] || "").padEnd(2,"0");
    };
    const positive = value => /^(?:0|[1-9]\d*)(?:\.\d{1,2})?$/.test(String(value)) && /[1-9]/.test(String(value));
    const validMonth = m => /^\d{4}-(?:0[1-9]|1[0-2])$/.test(String(m));
    function monthArgs(book, month) {
        if (!book || !validMonth(month)) throw Error("请选择账簿和月份。");
        return {book, month};
    }
    function scopeBooks(books,company) { return books.filter(b=>b.company===company); }
    function extractRefs(text) {
        const values = String(text || "").trim().split(/[\s,，、;；/]+/).filter(Boolean);
        if (values.some(v => !/^[\w-]{1,100}$/.test(v) || /^\d+e[+-]?\d+$/i.test(v))) throw Error("请填写完整的钉钉单号，多个单号可分行输入。");
        return [...new Set(values)];
    }
    function clientLink(official) {
        try {
            const url = new URL(official);
            if (url.protocol !== "https:" || url.hostname !== "aflow.dingtalk.com" || url.username || url.password || url.port) return "";
            const ids = url.searchParams.getAll("procInstId");
            const query = url.hash.indexOf("?");
            if (query !== -1) ids.push(...new URLSearchParams(url.hash.slice(query+1)).getAll("procInstId"));
            if (ids.length !== 1 || !/^[\w-]{1,200}$/.test(ids[0])) return "";
            const mobile = "https://aflow.dingtalk.com/dingtalk/mobile/homepage.htm?showmenu=false&dd_progress=false#/approval?procInstId="+encodeURIComponent(ids[0]);
            return "dingtalk://dingtalkclient/page/link?url="+encodeURIComponent(mobile)+"&pc_slide=true";
        } catch { return ""; }
    }
    function editCell(key, label, row) {
        const input = (name, title, value, type = "text") =>
            '<input name="'+name+'" aria-label="'+esc(title)+'" type="'+type+'" value="'+esc(value)+'" '+
            (["income","expense"].includes(name)?'inputmode="decimal"':'')+'>';
        const textarea = (name, title, value) =>
            '<textarea name="'+name+'" aria-label="'+esc(title)+'" rows="2">'+esc(value)+'</textarea>';
        if (key === "date") return input("date","日期",row.date,"date")+
            textarea("refs","钉钉单号",(row.refs || []).join("\n"));
        if (key === "summary") return textarea("summary",label,row.summary)+
            '<div class="bj-edit-actions"><button type="button" class="btn btn-primary btn-xs" data-act="save-edit">保存</button>'+
            '<button type="button" class="btn btn-default btn-xs" data-act="cancel-edit">取消</button></div>'+
            '<p class="bj-error bj-edit-error" role="alert" tabindex="-1" hidden></p>';
        if (key === "balance") return esc(amountText(row.balance))+
            '<small class="bj-balance-note">保存后重算</small>';
        return input(key,label,row[key]);
    }
    function rowMarkup(row, {canEdit = false, editing = false, editBusy = false} = {}) {
        return (editing?'<tr class="bj-editing">':'<tr>')+columns.map(([key,label])=>{
            let value;
            if (editing) value=editCell(key,label,row);
            else if (key === "date") {
                value = '<time>'+esc(row.date)+'</time>'+(row.refs || []).map(ref=>{
                    const link = clientLink(row.links?.[ref]);
                    return link ? '<a class="bj-ref" href="'+esc(link)+'" title="在钉钉打开原单">'+esc(ref)+'</a>' :
                        '<span class="bj-ref" title="未找到当前用户可打开的原单">'+esc(ref)+'</span>';
                }).join("");
            } else if (key === "summary") {
                value='<div class="bj-summary-content"><button type="button" class="bj-rowlink" data-row="'+esc(row.name)+
                    '" title="查看这笔流水">'+esc(row.summary || "查看流水")+'</button>'+
                    (canEdit?'<button type="button" class="btn btn-default btn-xs bj-edit-link" data-edit="'+esc(row.name)+
                        '" '+(editBusy?'disabled':'')+'>编辑</button>':'')+'</div>';
            } else if (["income","expense","balance"].includes(key)) value=esc(amountText(row[key]));
            else value=esc(row[key] || "—");
            return '<td class="bj-'+key+'" data-label="'+esc(label)+'">'+value+'</td>';
        }).join("")+'</tr>';
    }
    function syncSelection(rows, month) {
        if (!rows.length) throw Error("请勾选本月已经实际支付的支出。");
        return rows.map(({source_key,date,expense})=>{
            if (!source_key || !/^\d{4}-\d{2}-\d{2}$/.test(date) || date.slice(0,7)!==month || !positive(expense))
                throw Error("每笔选中支出都需要本月的实际付款日期和大于零的实付金额。");
            return {source_key,date,expense:String(expense)};
        });
    }
    function syncPreview(rows, month) {
        const groups = {ready:[], existing:[], review:[]};
        rows.forEach((row, index)=>{
            const group = row.already_recorded ? "existing" : row.selectable === true && row.source_key ? "ready" : "review";
            groups[group].push({row, index});
        });
        const tabs = [["ready","可导入"],["existing","已存在"]];
        if (groups.review.length) tabs.push(["review","需核对"]);
        const section = (group, label) => {
            const editable = group === "ready";
            const entries = groups[group];
            const rowHTML = entries.map(({row, index})=>{
                const refs = row.refs?.join?.(" / ") || row.number || "";
                const summary = row.summary || row.title || refs || "支出申请";
                const metadata = [refs===summary?"":refs, row.application_date].filter(Boolean).join(" · ");
                const title = '<span class="bj-sync-title" title="'+esc(summary)+'">'+esc(summary)+'</span>';
                const identity = editable
                    ? '<label class="bj-sync-pick"><input type="checkbox" data-pick="'+index+'">'+title+'</label>'
                    : title;
                const note = group === "review" && row.reason
                    ? '<span class="bj-sync-meta">'+esc(row.reason)+'</span>' : "";
                return '<tr><td>'+identity+'<span class="bj-sync-meta">'+esc(metadata)+'</span>'+note+'</td>'+
                    '<td class="bj-sync-money" data-label="申请金额">'+esc(amountText(row.application_amount))+'</td>'+
                    (editable?
                        '<td data-label="实付日期"><input type="date" name="date-'+index+'" aria-label="实付日期" value="'+esc(row.date || row.cashier_date)+'"></td>'+
                        '<td data-label="实付金额"><input type="text" inputmode="decimal" name="expense-'+index+
                            '" aria-label="实付金额" value="'+esc(row.expense ?? row.application_amount)+'"></td>':"")+'</tr>';
            }).join("");
            const table = '<table class="bj-sync-table'+(editable?' bj-sync-ready':'')+'"><thead><tr>'+
                '<th scope="col">支出</th><th scope="col" class="bj-sync-money">申请金额</th>'+
                (editable?'<th scope="col">实付日期</th><th scope="col">实付金额</th>':"")+'</tr></thead>'+
                '<tbody>'+rowHTML+'</tbody></table>';
            const empty = '<p class="text-muted bj-sync-empty">本月没有'+(editable?"可导入":"已录入")+'的支出。</p>';
            return '<section data-sync-panel="'+group+'" '+(editable?"":"hidden ")+'aria-label="'+label+'">'+
                (entries.length?table:empty)+'</section>';
        };
        const buttons = tabs.map(([group,label])=>'<button type="button" class="btn btn-default btn-sm" '+
            'data-sync-tab="'+group+'" aria-pressed="'+(group==="ready")+'">'+label+' '+groups[group].length+'</button>').join("");
        return '<div class="bj-sync-heading"><nav aria-label="支出状态">'+buttons+'</nav><time>'+esc(month)+'</time></div>'+
            tabs.map(([group,label])=>section(group,label)).join("");
    }
    function retryKey(state, args) {
        const signature=JSON.stringify(args);
        if (state.signature!==signature) Object.assign(state,{signature,id:root.crypto.randomUUID()});
        return state.id;
    }
    const service = method => "budget.api."+(
        ["preview_import","apply_import","export_journal"].includes(method)?"bank_journal_files":
        ["preview_sync","apply_sync"].includes(method)?"bank_journal_actions":"bank_journal")+"."+method;
    async function call(method,args={}) {
        return (await root.frappe.call({method:service(method),args,type:"POST"})).message;
    }
    function reason(error) {
        if (error instanceof Error && error.message) return error.message;
        if (error?.responseJSON?.exception) return String(error.responseJSON.exception).split("\n")[0].replace(/^[\w.]+:\s*/,"");
        return "请求未完成，可保留输入后重试。";
    }
    function optionsList(choices) {
        return choices.map(([val,label])=>'<option value="'+esc(val)+'">'+esc(label)+'</option>').join("");
    }
    const field=(id,label,value="",type="text",attributes="")=>'<label>'+esc(label)+'<input name="'+id+'" type="'+type+'" value="'+esc(value)+'" '+attributes+'></label>';
    const multi=(id,label,value)=>'<label class="bj-wide">'+esc(label)+'<textarea name="'+id+'" rows="2">'+esc(value)+'</textarea></label>';
    const formValue=(el,name)=>el.querySelector('[name="'+name+'"]').value.trim();
    function movementValues(el, base) {
        const fields=["date","category","level2","level3","project","dept","summary","income","expense","invoice"];
        const values=Object.fromEntries(fields.map(key=>[
            key,el.querySelector('[name="'+key+'"]')?formValue(el,key):(base[key]??"")
        ]));
        values.refs=extractRefs(formValue(el,"refs"));
        if (!values.date || Number(positive(values.income))+Number(positive(values.expense))!==1 ||
            [values.income,values.expense].some(v=>v&&!positive(v)&&!/^0(?:\.0{1,2})?$/.test(v))) {
            throw Error("请填写日期，并在收入或支出一栏输入正金额。");
        }
        values.income=positive(values.income)?values.income:null;
        values.expense=positive(values.expense)?values.expense:null;
        if(base.name)values.name=base.name;
        return values;
    }
    function saveMovement(book, version, receipt, row) {
        const args={book,version,row};
        return call("save_row",{...args,request_id:retryKey(receipt,args)});
    }
    function dialog(title, html) {
        const d = new root.frappe.ui.Dialog({title, size:"large", fields:[{fieldname:"body",fieldtype:"HTML"}]});
        const el=d.fields_dict.body.$wrapper[0];
        el.classList.add("bj-dialog"); el.innerHTML=html+'<p class="bj-error" role="alert"></p>';
        const message=el.lastElementChild;
        const error=text=>{message.textContent=text};
        async function run(fn) {
            if (d.__busy) return;
            d.__busy=true;
            const button=d.get_primary_btn();button.prop("disabled",true);error("");
            try {await fn();} catch(e){error(reason(e));}
            finally {d.__busy=false;button.prop("disabled",false);}
        }
        const action=(label,fn)=>d.set_primary_action(label,()=>run(fn));
        d.set_secondary_action(()=>{if(!d.__busy)d.hide()});d.set_secondary_action_label("取消");
        d.show();return {d,el,action,error,run};
    }
    const importFields=[["date","日期"],["summary","摘要"],["income","收入"],["expense","支出"],["balance","余额"]];
    function importMapping(preview, mapping, signed=mapping.amount!=null, open=false) {
        if (!preview.headers?.length || (!preview.needs_mapping && preview.mapping_used===null)) return "";
        const choices=[["","未使用"],...preview.headers.map((header,i)=>[i,(i+1)+" · "+header])];
        const fields=signed ? [...importFields.slice(0,2),["amount","交易金额"],["direction","收支方向"],importFields[4]] : [...importFields];
        for (const [key,label] of [["bank_ref","银行流水号"],["currency","币种"],["refs","钉钉单号"]]) {
            if (preview.errors?.some(issue=>issue.field===key)) fields.push([key,label]);
        }
        return '<details class="bj-import-mapping"'+(open?' open':'')+'><summary>列对应'+(preview.needs_mapping?' · 待设置':'')+'</summary>'+
            '<div class="bj-import-columns"><label>收支列形式<select data-layout>'+[
                ["split","收入、支出分列"],["signed","收支同列"]].map(([value,label])=>
                '<option value="'+value+'"'+((value==="signed")===signed?' selected':'')+'>'+label+'</option>').join("")+
            '</select></label>'+fields.map(([key,label])=>'<label>'+esc(label)+'<select data-map="'+key+'">'+choices.map(([index,name])=>
                '<option value="'+esc(index)+'"'+(String(mapping[key]??"")===String(index)?' selected':'')+'>'+esc(name)+'</option>').join("")+
            '</select></label>').join("")+'</div></details>';
    }
    function importTable(rows, errors) {
        const issues=new Map();
        for (const issue of errors || []) {
            if (!issues.has(issue.row)) issues.set(issue.row,[]);
            issues.get(issue.row).push(issue);
        }
        const head='<th scope="col">原行</th>'+importFields.map(([,label])=>'<th scope="col">'+label+'</th>').join("");
        const body=rows.map(row=>{
            const problems=issues.get(row.source_row)||[];
            const cells=importFields.map(([key,label])=>{
                const value=row[key]??"";
                const issue=problems.find(item=>item.field===key || (["income","expense"].includes(key) && ["amount","direction"].includes(item.field)));
                const attrs='data-field="'+key+'" aria-label="原表第 '+esc(row.source_row)+' 行 '+label+'" title="'+esc(issue?.message || value)+'"'+
                    (issue?' aria-invalid="true"':'');
                return '<td>'+(key==="summary"?'<textarea rows="1" maxlength="12000" '+attrs+'>'+esc(value)+'</textarea>':
                    '<input type="text" maxlength="80" '+(key==="date"?'':'inputmode="decimal" ')+attrs+' value="'+esc(value)+'">')+'</td>';
            }).join("");
            const note=problems.map(issue=>issue.message).join("；");
            return '<tr data-source-row="'+esc(row.source_row)+'"><th scope="row"'+(note?' class="bj-import-row-issue" title="'+esc(note)+'"':'')+'>'+esc(row.source_row)+'</th>'+cells+'</tr>';
        }).join("");
        return '<div class="bj-import-grid" tabindex="0" role="region" aria-label="银行流水预览"><table class="bj-preview"><thead><tr>'+head+
            '</tr></thead><tbody>'+body+'</tbody></table></div>';
    }
    function errorList(errors) {
        if (!errors?.length) return "";
        return '<div class="bj-error">'+errors.slice(0,40).map(e=>esc(typeof e==="string"?e:((e.row?"第 "+e.row+" 行 · ":"")+(e.message||e.reason||"资料有误")))).join("<br>")+(errors.length>40?"<br>另有 "+(errors.length-40)+" 项":"")+'</div>';
    }
    function fileData(file) {
        return new Promise((resolve,reject)=>{
            const reader=new FileReader();reader.onerror=()=>reject(Error("读取文件失败，请重新选择文件。"));
            reader.onload=()=>resolve(String(reader.result).split(",")[1]);reader.readAsDataURL(file);
        });
    }
    class Workbench {
        constructor(wrapper) {
            root.DeepLinkERPCNCashJournal?.installNavigation?.();
            this.page=root.frappe.ui.make_app_page({parent:wrapper,title:"银行日记账",single_column:true});
            this.page.add_menu_item("原有手工日记账",()=>switchClassic(true));
            this.el=document.createElement("section");
            this.el.className="bj-workbench";
            this.page.main[0].append(this.el);
            this.seq=0;this.books=[];this.data=null;
            this.el.addEventListener("click",e=>this.click(e));
            this.el.addEventListener("change",e=>this.change(e));
            this.initialize();
        }
        async initialize() {
            const seq=++this.seq;
            this.el.textContent="读取账簿…";
            try {
                const data=await call("get_options");if(seq!==this.seq)return;
                this.opts=data;this.books=data.books || [];
                this.companies=(data.companies||[]).map(c=>typeof c==="string"?c:c.name);
                this.company=this.companies.includes(this.company)?this.company:this.companies.length===1?this.companies[0]:"";
                this.chooseBook();
                this.month=validMonth(this.month)?this.month:data.month;
                if(!validMonth(this.month))throw Error("未取得有效的当前月份，请刷新重试。");
                this.loaded=true;this.render();if(this.book)await this.load();
            } catch(e){if(seq===this.seq)this.el.innerHTML='<p class="bj-error">'+esc(reason(e))+'</p><button class="btn btn-default" data-act="initialize">重试</button>';}
        }
        chooseBook(){
            const accounts=scopeBooks(this.books,this.company);
            this.book=accounts.find(b=>b.name===this.book)?.name || (accounts.length===1?accounts[0].name:"");
        }
        openDialog(title,html){
            const company=this.company,book=this.book,period=this.month;
            const caption=[company,this.books.find(b=>b.name===book)?.account].filter(Boolean).map(esc).join(" · ");
            const instance=dialog(title,'<p class="text-muted">'+caption+'</p>'+html);
            this.activeDialog=instance.d;
            const check=()=>{
                if(company!==this.company||book!==this.book||period!==this.month)throw Error("公司、账簿或月份已变化，请重新打开操作。");
            };
            return {...instance,check,action:(label,fn)=>instance.action(label,async()=>{check();await fn();})};
        }
        async load(scrollTop) {
            const seq=++this.seq;this.data=null;this.failed="";this.render();
            try{
                const result=await call("get_month",monthArgs(this.book,this.month));
                if(seq!==this.seq)return;this.data=result;
            }catch(e){if(seq!==this.seq)return;this.failed=reason(e);}
            this.render(scrollTop);
        }
        render(scrollTop) {
            const b=this.books.find(x=>x.name===this.book),data=this.data,rows=data?.rows || [];
            const use=(id,label,disable=false,primary=false)=>'<button data-act="'+id+'" class="btn btn-'+(primary?"primary":"default")+' btn-sm" '+(disable?"disabled":"")+'>'+label+'</button>';
            const editing=this.editing, canEdit=Boolean(this.opts.can_write);
            const accounts=scopeBooks(this.books,this.company);
            const companySelect='<label>公司<select name="company" aria-label="公司" '+(editing?"disabled":"")+'>'+optionsList([["","请选择公司"],...this.companies.map(c=>[c,c])])+'</select></label>';
            const bookSelect='<label>银行账户<select name="book" aria-label="银行账户" '+(!accounts.length||editing?"disabled":"")+'>'+optionsList([["","请选择银行账户"],...accounts.map(b=>[b.name,b.account])])+'</select></label>';
            const allowed=this.opts.can_create!==false;
            const summary=[["opening","月初余额"],["income","本月收入"],["expense","本月支出"],["closing","月末余额"]];
            const table='<table class="bj-ledger"><colgroup>'+columns.map(([key])=>'<col class="bj-width-'+key+'">').join("")+'</colgroup><thead><tr>'+
                columns.map(([key,label])=>'<th scope="col" class="bj-'+key+'">'+esc(label)+'</th>').join("")+'</tr></thead><tbody>'+rows.map(row=>rowMarkup(row,{canEdit,editing:editing?.row.name===row.name,editBusy:Boolean(editing)})).join("")+'</tbody></table>';
            const toolbar='<div class="bj-toolbar">'+companySelect+bookSelect+'<label>月份<input name="month" aria-label="月份" type="month" value="'+esc(this.month)+'" '+(editing?"disabled":"")+'></label>'+
                (allowed&&this.company?use("book","新建账簿",Boolean(editing)):"")+'</div>';
            if(!this.book) {
                this.el.innerHTML=toolbar+'<p class="text-muted">'+(!this.company?'请选择流水所属公司。':accounts.length?'请选择本次操作的银行账户。':'该公司还没有银行账簿。')+'</p>'+
                    (this.opts.legacy_count?use("classic","查看原有手工流水"):"");
                this.el.querySelector('[name="company"]').value=this.company;return;
            }
            this.el.innerHTML=toolbar+'<dl class="bj-totals">'+summary.map(([k,label])=>'<div><dt>'+label+'</dt><dd>'+amountText(data?.[k])+'</dd></div>').join("")+'</dl>'+
                '<div class="bj-actions">'+(allowed?use("add","新增行",!data||editing,true)+use("import","导入银行流水",!data||editing)+(this.opts.can_sync!==false?use("sync","同步钉钉",!data||editing):""):"")+(this.opts.can_export!==false?use("export","导出",!data||editing):"")+
                (data?.undo_operation?use("undo","撤回上次操作",Boolean(editing)):"")+'<span class="text-muted">'+(data?esc(this.month)+" · "+rows.length+" 笔":"")+'</span></div>'+
                (this.failed?'<p class="bj-error">'+esc(this.failed)+'</p>'+use("refresh","重试"):!data?'<p role="status">正在读取本月流水…</p>':
                '<div class="bj-grid">'+table+(rows.length?"":'<p class="text-muted bj-empty">本月暂无流水。</p>')+'</div>');
            this.el.querySelector('[name="company"]').value=this.company;
            if(b)this.el.querySelector('[name="book"]').value=b.name;
            const grid=this.el.querySelector('.bj-grid');
            if(grid&&scrollTop!=null)grid.scrollTop=scrollTop;
        }
        change(e){if(e.target.name==="company"){this.company=e.target.value;this.book="";this.chooseBook();}
            else if(e.target.name==="book")this.book=e.target.value;else if(e.target.name==="month"){
            if(!validMonth(e.target.value)){e.target.value=this.month;return;}this.month=e.target.value;
        }else return;
            this.activeDialog?.hide();this.activeDialog=null;
            if(this.book)this.load();else {++this.seq;this.data=null;this.failed="";this.render();}
        }
        click(e) {
            const key=e.target.closest("[data-act]")?.dataset.act;
            if (key==="save-edit") {this.saveEdit();return;}
            if (key==="cancel-edit") {this.closeEdit();return;}
            if (this.editing) return;
            const edit=e.target.closest("[data-edit]");
            if(edit){this.startEdit(edit.dataset.edit);return;}
            const row=e.target.closest("[data-row]");
            if(row){const r=this.data?.rows.find(x=>x.name===row.dataset.row);if(r)this.rowDialog(r);return;}
            if(key==="initialize")this.initialize();if(key==="refresh")this.load();
            if(key==="book")this.bookDialog();if(key==="classic")switchClassic(true);
            if(!this.data)return;
            if(key==="add")this.rowDialog(null);if(key==="export")this.exportDialog();
            if(key==="import")this.importDialog();if(key==="sync")this.syncDialog();if(key==="undo")this.undoDialog();
        }
        startEdit(name) {
            const row=this.data?.rows.find(entry=>entry.name===name);
            if (!row || !this.opts.can_write) return;
            this.editing={row,version:this.data.version,book:this.book,receipt:{},busy:false};
            this.render(this.el.querySelector(".bj-grid")?.scrollTop);
            this.el.querySelector(".bj-editing input")?.focus({preventScroll:true});
        }
        closeEdit() {
            if (!this.editing || this.editing.busy) return;
            const name=this.editing.row.name;
            this.editing=null;
            this.render(this.el.querySelector(".bj-grid")?.scrollTop);
            [...this.el.querySelectorAll("[data-edit]")].find(el=>el.dataset.edit===name)?.focus({preventScroll:true});
        }
        async saveEdit() {
            const edit=this.editing, form=this.el.querySelector(".bj-editing");
            if (!edit || edit.busy || !form) return;
            const error=form.querySelector(".bj-edit-error");
            const controls=form.querySelectorAll("input,textarea,button");
            try {
                const values=movementValues(form,edit.row);
                edit.busy=true;
                controls.forEach(el=>el.disabled=true);
                error.hidden=true;
                await saveMovement(edit.book,edit.version,edit.receipt,values);
                this.editing=null;
                const nextMonth=values.date.slice(0,7);
                const scrollTop=nextMonth===this.month?this.el.querySelector(".bj-grid")?.scrollTop:0;
                this.month=nextMonth;
                await this.load(scrollTop);
                [...this.el.querySelectorAll("[data-edit]")].find(el=>el.dataset.edit===edit.row.name)?.focus({preventScroll:true});
                root.frappe.show_alert({message:"流水已保存，余额已更新",indicator:"green"});
            } catch (e) {
                error.textContent=reason(e);
                error.hidden=false;
                error.focus();
            } finally {
                edit.busy=false;
                controls.forEach(el=>el.disabled=false);
            }
        }
        bookDialog() {
            const companies=[[this.company,this.company]];
            const {d,el,action}=this.openDialog("新建银行账簿",'<div class="bj-form"><label>公司<select name="company" disabled>'+optionsList(companies)+'</select></label>'+
                field("account","银行账户名称")+field("opening_date","启用日期",this.month+"-01","date")+field("opening","期初余额","","text",'inputmode="decimal"')+'</div>');
            action("创建账簿",async()=>{
                const args=Object.fromEntries(["company","account","opening_date","opening"].map(k=>[k,formValue(el,k)]));
                if(Object.values(args).some(v=>!v))throw Error("请填写公司、账户、启用日期和期初余额。");
                const r=await call("create_book",args);this.book=r.book || r.name;d.hide();await this.initialize();
            });
        }
        rowDialog(row) {
            const base=row || {},book=this.book,version=this.data.version,receipt={};
            const write=row?this.opts.can_write!==false:this.opts.can_create!==false;
            const today=root.frappe.datetime.get_today();
            const {d,el,action,error,check}=this.openDialog(row?"流水明细":"新增流水",'<div class="bj-form">'+
                field("date","日期",base.date || (today.startsWith(this.month)?today:this.month+"-01"),"date")+
                field("category","类别",base.category)+field("level2","二级明细",base.level2)+field("level3","三级明细",base.level3)+
                field("project","所属项目",base.project)+field("dept","归属部门",base.dept)+multi("summary","摘要",base.summary)+
                field("income","收入",base.income,"text",'inputmode="decimal"')+field("expense","支出",base.expense,"text",'inputmode="decimal"')+
                multi("refs","钉钉单号（多个可分行填写）",(base.refs||[]).join("\n"))+multi("invoice","发票寄送信息",base.invoice)+'</div>'+
                (row&&this.opts.can_delete!==false?'<button type="button" class="btn btn-danger btn-sm" data-cancel>取消这笔流水</button>':""));
            if(!write){el.querySelectorAll("input,textarea").forEach(x=>x.readOnly=true);d.get_primary_btn().addClass("hide");}
            else action("保存",async()=>{
                const values=movementValues(el,base);
                await saveMovement(book,version,receipt,values);d.hide();
                this.month=values.date.slice(0,7);await this.load();
            });
            el.querySelector("[data-cancel]")?.addEventListener("click",()=>root.frappe.confirm("取消此笔流水后，余额将重新汇总。确定取消？",async()=>{
                if(d.__busy)return;d.__busy=true;
                try{check();const args={book,name:row.name,version};await call("cancel_row",{...args,request_id:retryKey(receipt,args)});d.hide();await this.load();}
                catch(e){error(reason(e));}finally{d.__busy=false;}
            }));
        }
        undoDialog() {
            const args={book:this.book,version:this.data.version,operation:this.data.undo_operation},receipt={};
            const {d,action}=this.openDialog("撤回上次操作","<p>将撤回此账簿最近一次可撤回的操作，同批导入或同步的流水将一起撤回。</p>");
            action("确认撤回",async()=>{await call("undo",{...args,request_id:retryKey(receipt,args)});d.hide();await this.load();});
        }
        exportDialog() {
            const {book,month}=this, {d,el,action}=this.openDialog("导出日记账",'<label>导出范围<select name="scope">'+optionsList([["month",month+" 当月"],["all","整本账簿"]])+'</select></label><p class="text-muted">CSV 沿用原表的双语列名和字段顺序，可用 Excel 打开。</p>');
            action("下载 CSV",async()=>{
                const args={book,month:formValue(el,"scope")==="month"?month:"all"};
                const file=await call("export_journal",args);
                const bytes=Uint8Array.from(atob(file.content_base64),c=>c.charCodeAt(0));
                const href=URL.createObjectURL(new Blob([bytes],{type:"text/csv;charset=utf-8"}));
                const a=document.createElement("a");a.href=href;a.download=file.filename;a.click();setTimeout(()=>URL.revokeObjectURL(href),15000);d.hide();
            });
        }
        importDialog() {
            const book=this.book,receipt={};
            const {d,el,action,error,check,run}=this.openDialog("导入银行流水",'<div class="bj-import-toolbar">'+
                '<label class="bj-import-file"><input type="file" accept=".csv,.xlsx" name="statement" aria-label="选择 CSV 或 XLSX 文件"></label>'+
                '<div data-mapping></div></div><div data-result></div>');
            el.classList.add("bj-import-dialog");
            el.closest(".modal-dialog")?.classList.add("bj-import-modal");
            const fileInput=el.querySelector("[name=statement]");
            const mappingBox=el.querySelector("[data-mapping]"),resultBox=el.querySelector("[data-result]");
            let preview=null, data="", revision=0, offset=0, selected=null, headerRow=null, edits={};
            let layoutPreview=null;
            const validate=()=>read(offset);
            const invalidate=label=>{
                ++revision;
                preview=null;
                error("");
                const status=resultBox.querySelector("[data-validation]");
                if(status)status.textContent=label;
                action("重新校验",validate);
            };
            fileInput.onchange=()=>{
                ++revision;
                data=""; preview=null; layoutPreview=null; selected=null; headerRow=null; offset=0; edits={};
                error("");
                mappingBox.replaceChildren();
                resultBox.replaceChildren();
                action("预览流水",validate);
            };
            mappingBox.onchange=event=>{
                const input=event.target;
                if(input.matches("[data-layout]")){
                    selected={...selected};
                    const signed=input.value==="signed";
                    for(const field of signed?["income","expense"]:["amount","direction"])delete selected[field];
                    mappingBox.innerHTML=importMapping(layoutPreview,selected,signed,true);
                    mappingBox.querySelector("[data-layout]").focus();
                }else if(input.matches("[data-map]")){
                    selected={...selected};
                    if(input.value==="")delete selected[input.dataset.map];
                    else selected[input.dataset.map]=Number(input.value);
                }else return;
                invalidate("列对应尚未校验");
            };
            resultBox.oninput=event=>{
                const input=event.target;
                if(!input.matches("[data-field]"))return;
                const tr=input.closest("[data-source-row]"),key=input.dataset.field;
                const edited=edits[tr.dataset.sourceRow] ||= {};
                edited[key]=input.value;
                if(key==="income" || key==="expense"){
                    const other=key==="income"?"expense":"income";
                    edited[other]=tr.querySelector('[data-field="'+other+'"]').value;
                }
                input.classList.add("bj-import-edited");
                invalidate("修改尚未校验");
            };
            resultBox.onclick=event=>{
                const button=event.target.closest("[data-review-offset]");
                if(button)run(async()=>{check();await read(Number(button.dataset.reviewOffset));});
            };
            function fileArgs(file){
                return {book,filename:file.name,content_base64:data,edits:JSON.stringify(edits)};
            }
            const apply=async()=>{
                if(!preview?.preview_id)throw Error("请先重新校验流水。");
                const args={...fileArgs(fileInput.files[0]),mapping:preview.mapping_used,header_row:preview.header_row,
                    preview_id:preview.preview_id,version:preview.version};
                const controls=Array.from(el.querySelectorAll("input,textarea,select,button")).filter(input=>!input.disabled);
                controls.forEach(input=>input.disabled=true);
                try{
                    await call("apply_import",{...args,request_id:retryKey(receipt,{book,preview_id:preview.preview_id,version:preview.version})});
                }finally{controls.forEach(input=>input.disabled=false);}
                d.hide();await this.load();root.frappe.show_alert({message:"流水已导入",indicator:"green"});
            };
            async function read(nextOffset){
                const file=fileInput.files[0];
                if(!file)throw Error("请先选择银行流水文件。");
                if(file.size>6*1024*1024)throw Error("请选择 6 MB 以内的银行流水文件。");
                const v=++revision,bytes=data||await fileData(file);if(v!==revision)return;
                data=bytes;
                const res=await call("preview_import",{...fileArgs(file),mapping:selected,header_row:headerRow,
                    use_ai:layoutPreview===null?1:0,review_offset:nextOffset});
                if(v!==revision)return;
                headerRow=res.header_row ?? null;
                selected=res.mapping_used || selected || res.suggested_mapping || null;
                const mappingOpen=mappingBox.querySelector("details")?.open;
                const signed=selected?.amount!=null || (res.needs_mapping && mappingBox.querySelector("[data-layout]")?.value==="signed");
                mappingBox.innerHTML=importMapping(res,selected||{},signed,mappingOpen);
                layoutPreview=res;
                preview=res;
                const grid=resultBox.querySelector(".bj-import-grid");
                const position=nextOffset===offset?[grid?.scrollTop||0,grid?.scrollLeft||0]:[0,0];
                const rows=res.review_rows || [], total=res.review_total||0, pageSize=res.review_page_size||100;
                const problems=res.error_count||0;
                const start=nextOffset+1, end=nextOffset+rows.length;
                const paging=total>pageSize?'<nav aria-label="流水预览分页"><button type="button" class="btn btn-default btn-xs" data-review-offset="'+
                    Math.max(0,nextOffset-pageSize)+'" '+(nextOffset===0?'disabled':'')+'>上一页</button> '+
                    '<span>'+start+'–'+end+' / '+total+'</span> <button type="button" class="btn btn-default btn-xs" data-review-offset="'+
                    (nextOffset+pageSize)+'" '+(end>=total?'disabled':'')+'>下一页</button></nav>':"";
                resultBox.innerHTML=(rows.length?importTable(rows,res.review_errors):'<p class="text-muted">'+
                    esc(res.needs_mapping?"展开列对应，设置日期和收支列。":"没有可预览的流水。")+'</p>')+
                    '<div class="bj-import-footer"><div class="bj-import-stats" role="status">'+
                    (total?total+' 笔 · 收入 '+esc(amountText(res.income_total))+' · 支出 '+esc(amountText(res.expense_total)):"")+
                    '<span data-validation>'+(rows.length?(problems?"请核对标记行":"可直接编辑单元格"):"")+'</span></div>'+paging+'</div>'+
                    (problems?'<details class="bj-import-issues"><summary>'+problems+' 处需核对</summary>'+errorList(res.errors)+'</details>':"")+
                    (res.needs_mapping&&res.ai_notice?'<p class="text-muted">'+esc(res.ai_notice)+'</p>':"");
                resultBox.querySelector(".bj-import-grid")?.scrollTo({top:position[0],left:position[1]});
                offset=nextOffset;
                if(!res.needs_mapping&&!problems&&res.preview_id&&rows.length)action("确认导入",apply);
                else action("重新校验",validate);
            }
            action("预览流水",validate);
        }
        async syncDialog(){
            const {book,month}=this,receipt={};
            const {d,el,action,error}=this.openDialog("同步钉钉支出",'<div data-result><p role="status">正在读取钉钉支出…</p></div>');
            el.classList.add("bj-sync-dialog");
            const panel=el.querySelector("[data-result]");
            const primary=d.get_primary_btn();
            primary.addClass("hide");
            const read=async()=>{
                const p=await call("preview_sync",{book,month});
                const rows=p.rows || [];
                panel.innerHTML=errorList(p.errors)+syncPreview(rows,month);
                const picks=[...panel.querySelectorAll("[data-pick]")];
                const canApply=Boolean(p.preview_id && picks.length);
                let viewed="ready";
                const updateAction=()=>{
                    primary.toggleClass("hide",!canApply || viewed!=="ready");
                    primary.prop("disabled",d.__busy || !picks.some(input=>input.checked));
                };
                panel.onclick=e=>{
                    const tab=e.target.closest("[data-sync-tab]");
                    if (!tab || d.__busy) return;
                    viewed=tab.dataset.syncTab;
                    panel.querySelectorAll("[data-sync-tab]").forEach(button=>{
                        button.setAttribute("aria-pressed",String(button===tab));
                    });
                    panel.querySelectorAll("[data-sync-panel]").forEach(group=>{
                        group.hidden=group.dataset.syncPanel!==viewed;
                    });
                    updateAction();
                };
                panel.onchange=e=>{if (e.target.matches("[data-pick]")) updateAction();};
                if (canApply) action("导入所选",async()=>{
                    const selection=syncSelection(picks.filter(input=>input.checked).map(input=>{
                        const i=Number(input.dataset.pick);
                        return {source_key:rows[i].source_key,date:formValue(el,"date-"+i),expense:formValue(el,"expense-"+i)};
                    }),month);
                    const args={book,month,preview_id:p.preview_id,version:p.version,selection};
                    await call("apply_sync",{...args,request_id:retryKey(receipt,args)});d.hide();
                    await this.load();root.frappe.show_alert({message:"钉钉支出已录入",indicator:"green"});
                });
                updateAction();
            };
            try{await read();}catch(e){panel.replaceChildren();error(reason(e));action("重试",read);}
        }
    }
    function switchClassic(flag) {
        const url=new URL(root.location.href);
        if(flag)url.searchParams.set("journal_view","classic");else url.searchParams.delete("journal_view");
        root.location.assign(url.href);
    }
    function install(page) {
        const nativeLoad=page.on_page_load,nativeShow=page.on_page_show;
        page.on_page_load=wrapper=>{
            if(new URLSearchParams(root.location.search).get("journal_view")==="classic"&&nativeLoad){
                nativeLoad(wrapper);wrapper.cashJournal?.page?.add_menu_item("银行日记账",()=>switchClassic(false));
                return;
            }
            root.frappe.require("/assets/budget/css/bank_journal.css",()=>{wrapper.budgetBankJournal=new Workbench(wrapper)});
        };
        page.on_page_show=wrapper=>{
            const app=wrapper.budgetBankJournal;
            if(app?.loaded && app.book && !app.editing)app.load();
            else if(wrapper.cashJournal&&nativeShow)nativeShow(wrapper);
        };
    }
    return {columns,monthArgs,clientLink,rowMarkup,syncSelection,syncPreview,importMapping,importTable,extractRefs,retryKey,scopeBooks,Workbench,install};
});
