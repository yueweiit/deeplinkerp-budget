(function(root,factory) {
	const api=factory(root);
	if(typeof module==="object" && module.exports)module.exports=api;
	root.CNApprovalManagement=api;
})(typeof globalThis!=="undefined"?globalThis:this,function(root) {
	"use strict";
	const t=(text,args)=>root.__?root.__(text,args):text.replace(/\{(\d+)\}/g,(_,n)=>args?.[Number(n)]??"");
	const esc=value=>String(value??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
	const ui=()=>root.CNApprovalWorkbench;
	const FLAGS={over_budget:"Over budget",forecast_over:"Pending applications exceed the budget",near_limit:"Near the budget limit",no_budget:"Expenditure without a budget",incomplete:"Incomplete data"};
	const REPORTS={company:["Company budget execution report","Compare budgets and expenditure across subsidiaries."],
		budget:["Budget category variance report","Find the categories responsible for budget differences."],
		trend:["Monthly budget and expenditure report","Review monthly budgets, expenditure and pending applications."],
		hr:["Personnel expenditure report","Compare salaries, insurance, bonuses and employee benefits."],
		purchase:["Purchase expenditure report","Review purchases by subsidiary, category and project."]};
	const LABELS={budget:"Effective budget",approved:"Approved budget occupation",pending:"Pending expenditure applications",balance:"Budget difference",forecast_balance:"Difference if all pending applications pass",operating:"Approved operating expenditure",purchase:"Approved purchase expenditure"};
	const navigation=(label,view,filters={},className="cnaw-link")=>`<button class="${className}" data-mgmt-nav="${view}" data-mgmt-filters="${esc(JSON.stringify(filters))}">${label}</button>`;
	function money(value,unit) {return value===null||value===undefined?`<span class="cnam-na">— <small>${esc(t("Not available"))}</small></span>`:esc(ui().formatMoney(value,unit));}
	const category=(code,options)=>esc(t(options.management_categories?.[code]||"Unclassified"));
	const company=name=>esc(name||t("Company not matched"));
	const percentage=value=>value===null||value===undefined?"—":`${new Intl.NumberFormat((root.frappe?.boot?.lang||"en").replace("_","-"),{maximumFractionDigits:1}).format(Number(value))}%`;
	function flags(row) {
		return row.flags?.length?row.flags.map(flag=>`<span class="cnam-status ${esc(flag)}">${esc(t(FLAGS[flag]||flag))}</span>`).join(""):
			`<span class="cnam-status ${row.budget!=null?"normal":"empty"}">${esc(t(row.budget!=null?"Within budget":row.count?"No effective budget":"No activity"))}</span>`;
	}
	function progress(row) {
		const percent=row.execution_rate;
		if(percent===null||percent===undefined)return '<span class="cnam-na">—</span>';
		return `<div class="cnam-progress ${Number(percent)>100?"over":""}"><span>${percentage(percent)}</span><i><b style="width:${Math.max(0,Math.min(100,Number(percent)))}%"></b></i></div>`;
	}
	function scope(row) {return {company:row.company||"",unassigned:row.company?"":"1",...(row.month?{from_month:row.month,to_month:row.month}:{})};}
	function amountLink(row,key,unit,extra={}) {
		return navigation(money(row[key],unit),"register",{...scope(row),measure:key,...extra},"cnaw-link cnam-amount-link");
	}
	function panel(title,subtitle,body,action="") {
		return `<section class="cnaw-panel cnam-panel"><div class="cnaw-panel-title"><div><h2>${esc(t(title))}</h2>${subtitle?`<p>${esc(subtitle)}</p>`:""}</div>${action}</div>${body}</section>`;
	}
	function qualityBanner(work) {
		const q=work.data.management?.quality||work.data.quality;
		const history=work.data.retained_history||[],missing=work.data.missing_history_templates||0;
		const notice=missing?`<div class="cnam-quality attention">${esc(t("{0} disabled templates have no verified local snapshot. Amounts may be incomplete; budget differences are unavailable.",[missing]))}</div>`:history.length?`<div class="cnam-history-note">${esc(t("Disabled templates use their last synchronized data; later OA changes are not included."))} ${esc(t("Data updated at"))}: ${esc(history.map(item=>String(item.fetched_at).slice(0,19)).join(" / "))}</div>`:"";
		if(!q)return notice;
		const actions=work.data.options?.can_manage_settings?`<button class="cnaw-button" data-action="settings">${esc(t("Complete company and category mappings"))}</button>`:navigation(esc(t("View data issues")),"review");
		return notice+`<div class="cnam-quality ${q.unassigned||q.unclassified?"attention":""}"><div><strong>${esc(t("Data coverage"))}</strong><span>${esc(t("{0} approvals; {1} without a company; {2} without a management category.",[q.count,q.unassigned,q.unclassified]))}</span>${q.unassigned?`<small>${esc(t("Unassigned amounts are listed separately. Company budget differences are incomplete until allocation is confirmed."))}</small>`:""}</div>${q.unassigned||q.unclassified?actions:""}</div>`;
	}
	function kpis(work) {
		const m=work.data.management.totals,unit=work.data.filters.reporting_currency;
		return `<section class="cnam-kpis">${["budget","approved","pending","balance"].map(key=>`<article class="cnam-kpi ${key}"><span>${esc(t(LABELS[key]))}</span><strong>${money(m[key],unit)}</strong><small>${esc(t({budget:"Latest approved version per company and month",approved:"Operating and purchase applications",pending:"Not yet included in approved occupation",balance:"Effective budget minus approved occupation"}[key]))}</small></article>`).join("")}<article class="cnam-kpi risk"><span>${esc(t("Companies over budget"))}</span><strong>${m.over_budget_companies}</strong><small>${esc(t("{0} more may exceed the budget if pending applications pass.",[m.forecast_over_companies]))}</small></article></section>`;
	}
	function companyTable(work) {
		const unit=work.data.filters.reporting_currency;
		const rows=work.data.management.companies.filter(row=>work.showEmptyCompanies||row.count);
		const empty=work.data.management.companies.filter(row=>!row.count).length;
		const cols=["Subsidiary","Effective budget","Approved operating expenditure","Approved purchase expenditure","Approved budget occupation","Pending expenditure applications","Budget difference","Budget execution rate","Attention"];
		const body=`<div class="cnaw-table-wrap"><table class="cnaw-table cnam-company-table"><thead><tr>${cols.map(label=>`<th>${esc(t(label))}</th>`).join("")}</tr></thead><tbody>${rows.map(row=>`<tr class="${row.company?"":"cnam-unassigned"}"><td>${navigation(company(row.company),"budget",scope(row))}<small>${esc(t("{0} approvals",[row.count]))}</small></td>${["budget","operating","purchase","approved","pending"].map(key=>`<td class="cnaw-number">${amountLink(row,key,unit)}</td>`).join("")}<td class="cnaw-number ${Number(row.balance)<0?"cnam-negative":""}">${money(row.balance,unit)}</td><td>${progress(row)}</td><td><div class="cnam-flags">${flags(row)}</div></td></tr>`).join("")||`<tr><td colspan="9" class="cnam-empty-row">${esc(t("No company activity in this period"))}</td></tr>`}</tbody></table></div><div class="cnam-table-note"><span>${esc(t("Select a company to inspect its monthly budget; select an amount to view the underlying detail lines."))}</span>${empty?`<button class="cnaw-link" data-action="toggle-empty-companies">${esc(t(work.showEmptyCompanies?"Hide companies without activity":"Show {0} companies without activity",[empty]))}</button>`:""}</div>`;
		return panel("Company budget execution",`${work.data.filters.from_month} — ${work.data.filters.to_month} · ${unit}`,body,`<button class="cnaw-button" data-export-report="company">↓ ${esc(t("Export report"))}</button>`);
	}
	function monthlyChart(work,spendingOnly=false) {
		const rows=work.data.management.months,unit=work.data.filters.reporting_currency;
		const series=spendingOnly?["operating","purchase"]:["budget","approved"];
		const max=Math.max(1,...rows.flatMap(row=>series.map(key=>Number(row[key]||0))));
		const legend=series.map(key=>`<span><i class="${key}"></i>${esc(t(LABELS[key]))}</span>`).join("");
		const body=`<div class="cnam-legend">${legend}</div><div class="cnam-month-chart">${rows.map(row=>`<div class="cnam-month"><div class="cnam-month-bars">${series.map(key=>navigation(`<span class="cnam-bar-value">${row[key]===null?"—":esc(ui().formatMoney(row[key],unit))}</span><i class="${key} ${row[key]===null?"unknown":""}" style="height:${row[key]===null?8:Math.max(1,Number(row[key])/max*140)}px"></i>`,"register",{measure:key,from_month:row.month,to_month:row.month},"cnam-bar-button")).join("")}</div><span>${esc(row.month)}</span></div>`).join("")||`<p class="cnam-empty-row">${esc(t("No activity"))}</p>`}</div>`;
		return panel(spendingOnly?"Monthly expenditure":"Budget and approved occupation",`${work.data.filters.from_month} — ${work.data.filters.to_month} · ${unit}`,body);
	}
	function attention(work) {
		const rows=work.data.management.companies.filter(row=>row.company&&row.flags.length);
		const rank={incomplete:0,over_budget:1,forecast_over:2,no_budget:3,near_limit:4};
		rows.sort((a,b)=>Math.min(...a.flags.map(x=>rank[x]))-Math.min(...b.flags.map(x=>rank[x])));
		const body=`<div class="cnam-attention">${rows.slice(0,8).map(row=>`<div><div>${navigation(company(row.company),"budget",scope(row))}<div class="cnam-flags">${flags(row)}</div></div><strong class="${Number(row.balance)<0?"cnam-negative":""}">${money(row.balance,work.data.filters.reporting_currency)}</strong></div>`).join("")||`<p class="cnam-empty-row">${esc(t(work.data.management.quality.unassigned?"Complete company mappings before assessing company budget risks.":"No budget alerts in the selected period."))}</p>`}</div>`;
		return panel("Management attention",t("Budget risks are separate from data quality issues."),body);
	}
	function overview(work) {return qualityBanner(work)+kpis(work)+companyTable(work)+`<div class="cnaw-panels">${monthlyChart(work)}${attention(work)}</div>`;}
	function categoryTable(work,rows,{companyName,month}={}) {
		const unit=work.data.filters.reporting_currency,options=work.data.options;
		const headers=["Management category","Effective budget","Approved operating expenditure","Approved purchase expenditure","Pending expenditure applications","Budget difference","Budget execution rate"];
		return `<div class="cnaw-table-wrap"><table class="cnaw-table cnam-budget-table"><thead><tr>${headers.map(label=>`<th>${esc(t(label))}</th>`).join("")}</tr></thead><tbody>${rows.map(row=>{
			const filters={...(companyName!==undefined?scope({company:companyName,month}):{}),management_category:row.management_category||"unclassified"};
			return `<tr><td>${navigation(category(row.management_category,options),"register",filters)}</td>${["budget","operating","purchase","pending"].map(key=>`<td class="cnaw-number">${navigation(money(row[key],unit),"register",{...filters,measure:key},"cnaw-link cnam-amount-link")}</td>`).join("")}<td class="cnaw-number ${Number(row.balance)<0?"cnam-negative":""}">${money(row.balance,unit)}</td><td>${progress(row)}</td></tr>`;
		}).join("")||`<tr><td colspan="7" class="cnam-empty-row">${esc(t("No activity"))}</td></tr>`}</tbody></table></div>`;
	}
	function budget(work) {
		const data=work.data.management,unit=work.data.filters.reporting_currency;
		const body=data.company_months.map((row,index)=>{
			const categories=data.category_months.filter(item=>item.company===row.company&&item.month===row.month);
			return `<details class="cnam-budget-group" ${index===0||work.data.filters.company?"open":""}><summary><div><strong>${company(row.company)}</strong><span>${esc(row.month)}</span></div><div class="cnam-budget-metric"><small>${esc(t("Effective budget"))}</small><b>${money(row.budget,unit)}</b></div><div class="cnam-budget-metric"><small>${esc(t("Approved budget occupation"))}</small><b>${money(row.approved,unit)}</b></div><div class="cnam-budget-metric"><small>${esc(t("Budget difference"))}</small><b class="${Number(row.balance)<0?"cnam-negative":""}">${money(row.balance,unit)}</b></div><div class="cnam-flags">${flags(row)}</div></summary>${categoryTable(work,categories,{companyName:row.company,month:row.month})}<div class="cnam-table-note">${navigation(esc(t("View effective budget")),"register",{...scope(row),measure:"budget"})}${navigation(esc(t("View superseded budgets ({0})",[row.superseded_count])),"register",{...scope(row),measure:"superseded"})}<span>${esc(t("Pending applications are shown separately and do not reduce the approved budget difference."))}</span></div></details>`;
		}).join("")||`<div class="cnam-empty-row">${esc(t("No budget or expenditure activity in this period"))}</div>`;
		return qualityBanner(work)+panel("Budget execution by company and month",t("Only the latest approved budget for each company and month is effective. Older versions remain available for review."),body,`<button class="cnaw-button" data-export-report="budget">↓ ${esc(t("Export report"))}</button>`);
	}
	function ranking(work,dimension) {
		const source=dimension==="company"?work.data.management.companies:work.data.management.categories;
		const rows=source.filter(row=>row.approval_count).slice().sort((a,b)=>Number(b.approved||0)-Number(a.approved||0));
		const max=Math.max(1,...rows.map(row=>Number(row.approved||0))),unit=work.data.filters.reporting_currency;
		return panel(dimension==="company"?"Expenditure by company":"Expenditure by management category",t("Approved operating and purchase applications"),`<div class="cnam-rankings">${rows.map((row,index)=>{
			const label=dimension==="company"?company(row.company):category(row.management_category,work.data.options);
			const filters=dimension==="company"?scope(row):{management_category:row.management_category||"unclassified"};
			return `<div><span class="cnam-rank">${String(index+1).padStart(2,"0")}</span><div>${navigation(label,"register",{...filters,measure:"approved"})}<i><b style="width:${Math.max(0,Number(row.approved||0)/max*100)}%"></b></i></div><strong>${money(row.approved,unit)}</strong></div>`;
		}).join("")||`<p class="cnam-empty-row">${esc(t("No approved amounts in this period"))}</p>`}</div>`);
	}
	function comparisons(work) {
		const data=work.data.comparisons,unit=work.data.filters.reporting_currency;
		if(!data)return "";
		const content=data.reason?`<p class="cnam-empty-row">${esc(t(data.reason==="open_period"?"Select completed months to compare periods. An incomplete current month is not compared with a full prior month.":"Comparable history is unavailable."))}</p>`:
			`<div class="cnam-comparisons">${data.items.map(item=>`<div><span>${esc(t(item.code==="year"?"Year-on-year expenditure change":"Previous-period expenditure change"))}</span><strong class="${Number(item.change)>0?"cnam-negative":""}">${money(item.change,unit)} <small>${percentage(item.percent)}</small></strong><p>${esc(t("Comparison period"))}: ${esc(item.from_month)} — ${esc(item.to_month)}</p><small>${esc(t("Approved budget occupation"))}: ${money(item.previous,unit)}</small>${item.reason==="exchange_rate"?`<p>${esc(t("Historical exchange rates are unavailable. Current-period amounts remain available."))}</p>`:""}</div>`).join("")}</div><div class="cnam-change-drivers">${data.drivers.map(row=>`<div><span>${esc(t(row.dimension==="company"?"Subsidiary":"Management category"))}</span><strong>${row.dimension==="company"?company(row.value):category(row.value,work.data.options)}</strong><b class="${Number(row.change)>0?"cnam-negative":""}">${money(row.change,unit)}</b></div>`).join("")}</div>`;
		return panel("Expenditure changes and contributors",t("Comparisons use the current approval status in completed periods. Missing historical data is not treated as zero."),content);
	}

	function analysis(work) {
		const data=work.data.management,unit=work.data.filters.reporting_currency;
		const top=data.top_spending;
		const body=`<div class="cnaw-table-wrap"><table class="cnaw-table"><thead><tr>${["Approval number","Subsidiary","Reporting month","Management category","Approved budget occupation"].map(label=>`<th>${esc(t(label))}</th>`).join("")}</tr></thead><tbody>${top.map(row=>`<tr><td><button class="cnaw-link" data-mgmt-detail="${esc(row.key)}">${esc(row.number)}</button><small>${esc(row.line_label)}</small></td><td>${company(row.company)}</td><td>${esc(row.month)}</td><td>${category(row.management_category,work.data.options)}</td><td class="cnaw-number">${money(row.converted_amount,unit)}</td></tr>`).join("")||`<tr><td colspan="5" class="cnam-empty-row">${esc(t("No approved amounts in this period"))}</td></tr>`}</tbody></table></div>`;
		return qualityBanner(work)+`<div class="cnam-analysis-total"><div><span>${esc(t("Approved expenditure in this period"))}</span><strong>${money(data.totals.approved,unit)}</strong></div><p>${esc(t("Compare companies and expense categories, then open the underlying applications."))}</p></div><div class="cnaw-panels">${ranking(work,"company")}${ranking(work,"category")}</div>`+monthlyChart(work,true)+comparisons(work)+panel("Largest expenditure lines",t("Allocated detail lines are shown once; header totals are not added again."),body);
	}
	function reports(work) {
		const data=work.data,selected=data.report_id||work.reportId||"company",unit=data.filters.reporting_currency;
		const catalog=`<section class="cnam-report-catalog">${Object.entries(REPORTS).map(([id,[label,description]])=>`<button data-report-id="${id}" aria-pressed="${id===selected}"><strong>${esc(t(label))}</strong><span>${esc(t(description))}</span></button>`).join("")}</section>`;
		const body=`<div class="cnaw-table-wrap"><table class="cnaw-table cnam-report-table"><thead><tr>${data.columns.map(col=>`<th>${esc(t(col.label))}</th>`).join("")}</tr></thead><tbody>${data.rows.map(row=>`<tr>${data.columns.map(col=>{
			let value=row[col.fieldname];
			if(col.fieldtype==="Currency")return `<td class="cnaw-number">${["budget","operating","purchase","approved","pending"].includes(col.fieldname)?navigation(money(value,unit),"register",{...scope(row),...("management_category" in row?{management_category:row.management_category||"unclassified"}:{}),...(selected==="purchase"?{kind:"purchase",project:row.project||"",no_project:row.project?"":"1"}:{}),measure:col.fieldname},"cnaw-link cnam-amount-link"):money(value,unit)}</td>`;
			if(col.fieldtype==="Percent")value=percentage(value);
			else if(col.fieldname==="company")return `<td>${company(value)}</td>`;
			else if(col.fieldname==="management_category")return `<td>${esc(t(data.categories[value]||"Unclassified"))}</td>`;
			return `<td>${esc(value??"—")}</td>`;
		}).join("")}</tr>`).join("")||`<tr><td colspan="${data.columns.length}" class="cnam-empty-row">${esc(t("No activity"))}</td></tr>`}</tbody></table></div>`;
		return catalog+qualityBanner(work)+panel(data.title,`${data.filters.from_month} — ${data.filters.to_month} · ${unit}`,body,`<button class="cnaw-button primary" data-export-report="${selected}">↓ ${esc(t("Export Excel"))}</button>`)+`<p class="cnam-report-note">${esc(t("Reports and drilldowns use the same calculation. A dash means unavailable, not zero. Approval does not confirm payment."))}</p>`;
	}
	function render(work) {return ({overview,budget,analysis,reports}[work.view]||overview)(work);}
	return {render,comparisons,overview,budget,analysis,reports,companyTable,qualityBanner,flags,progress,navigation,REPORTS,FLAGS};
});
