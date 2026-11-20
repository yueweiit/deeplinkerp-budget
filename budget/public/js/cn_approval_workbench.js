(function (root, factory) {
	const api = factory(root);
	if (typeof module === "object" && module.exports) module.exports = api;
	root.CNApprovalWorkbench = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function (root) {
	"use strict";
	const ENDPOINT = "budget.api.cn_approvals.";
	const KINDS = {budget:"Budget declaration", operating:"Operating expenditure", purchase:"Purchase expenditure"};
	const STATUSES = {approved:"Approved", pending:"In progress", rejected:"Rejected", terminated:"Terminated", withdrawn:"Withdrawn", unknown:"Unknown"};
	const VIEWS = {overview:"Overview", register:"Approval register", analysis:"Classification analysis", review:"Data review"};
	const ROUTES = {"china-expenses":"overview", "china-expense-details":"register", "china-expense-reports":"analysis", "china-personnel-report":"analysis", "china-expense-review":"review"};
	const t = (s, args) => root.__ ? root.__(s, args) : s.replace(/\{(\d+)\}/g, (_, n) => args?.[Number(n)] ?? "");
	const esc = value => String(value ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
	function monthNow() {return root.frappe?.datetime?.get_today?.().slice(0, 7) || new Date().toISOString().slice(0, 7);}
	function formatMoney(value, currency) {
		if (value === null || value === undefined || value === "" || !Number.isFinite(Number(value))) return t("Needs review");
		const locale = (root.frappe?.boot?.lang || "en").replace("_", "-");
		if (!currency || currency==="unknown") return `${new Intl.NumberFormat(locale,{minimumFractionDigits:2,maximumFractionDigits:2}).format(Number(value))} · ${t("Unknown currency")}`;
		try {return new Intl.NumberFormat(locale, {style:"currency",currency,minimumFractionDigits:2,maximumFractionDigits:2}).format(Number(value));}
		catch {return `${currency || ""} ${Number(value).toFixed(2)}`;}
	}
	function totalsHTML(totals, empty = "No approved amounts") {
		if (!totals?.length) return `<span class="cnaw-muted">${esc(t(empty))}</span>`;
		return totals.map(row => `<span class="cnaw-money-line"><small>${esc(row.currency === "unknown" ? t("Unknown currency") : row.currency)}</small> ${esc(formatMoney(row.amount,row.currency))}${row.incomplete ? `<span class="cnaw-incomplete">${esc(t("Incomplete amounts"))}</span>` : ""}</span>`).join("");
	}
	function safeApprovalURL(value) {
		return typeof value === "string" && /^(?:dingtalk:\/\/dingtalkclient\/|https:\/\/(?:aflow\.dingtalk\.com|aliwork\.com)\/)/.test(value) ? value : "";
	}
	function chartData(months, unit) {
		const labels = [...new Set(months.map(row=>row.month).filter(Boolean))].sort();
		return labels.map(month => ({month, values:Object.keys(KINDS).map(kind=>{
			const totals=months.find(row=>row.month===month && row.kind===kind)?.totals || [];
			if (totals.some(row=>row.currency==="unknown")) return {kind,amount:null};
			const total=totals.find(row=>row.currency===unit);
			return {kind,amount:total ? total.amount === null ? null : Number(total.amount) : 0};
		})}));
	}
	function badge(status) {return `<span class="cnaw-badge ${esc(status)}">${esc(t(STATUSES[status] || "Unknown"))}</span>`;}
	function typeLabel(kind) {return `<span class="cnaw-type ${esc(kind)}"><i></i>${esc(t(KINDS[kind] || kind))}</span>`;}
	function tableHTML(rows, labels, compact = false) {
		if (!rows.length) return `<div class="cnaw-empty"><span>⌕</span><strong>${esc(t("No approvals match these filters"))}</strong><p>${esc(t("Adjust the period or filters, then try again."))}</p></div>`;
		return `<div class="cnaw-table-wrap"><table class="cnaw-table"><thead><tr>${["Approval / applicant","Source organization","Reporting period","Business classification","Application amount","Approval status","Review"].map(label=>`<th>${esc(t(label))}</th>`).join("")}</tr></thead><tbody>${rows.slice(0, compact ? 8 : 25).map(row=>`<tr>
			<td><button class="cnaw-link" data-detail="${esc(row.key)}">${esc(row.number)}</button><div class="cnaw-subline">${typeLabel(row.kind)}<span>${esc(row.applicant || "—")}</span></div></td>
			<td><div class="cnaw-cell-title">${esc(row.entity || t("Unspecified organization"))}</div><small>${esc(row.company || t("Company not matched"))}</small></td>
			<td>${esc(row.month || t("Unassigned period"))}<small>${esc(row.application_date || "—")}</small></td>
			<td><div class="cnaw-cell-title">${esc(row.category || t("Unclassified"))}</div><small>${esc(row.classification?.slice(1).join(" / ") || "—")}</small></td>
			<td class="cnaw-number">${esc(formatMoney(row.amount,row.currency))}<small>${esc(row.currency || t("Unknown currency"))}</small></td>
			<td>${badge(row.status)}</td><td><button class="cnaw-review-link ${row.issues.length ? "has-issues" : ""}" data-detail="${esc(row.key)}" title="${esc(row.issues.map(key=>t(labels[key] || key)).join(" · "))}">${row.issues.length ? esc(t("{0} issues",[row.issues.length])) : esc(t("No detected issues"))}</button></td>
		</tr>`).join("")}</tbody></table></div>`;
	}
	class Workbench {
		constructor(wrapper, route) {
			this.wrapper=wrapper; this.route=route; this.view=ROUTES[route] || "overview"; this.seq=0; this.detailSeq=0;
			this.filters={from_month:monthNow(),to_month:monthNow(),kind:"",status:"",entity:"",currency:"",keyword:"",category:"",issue:""};
			this.start=0; this.data=null; this.active=true; this.loading=false;
			this.page=root.frappe.ui.make_app_page({parent:wrapper,title:t("CN Approval Workspace"),single_column:true});
			this.host=this.page.main?.[0] || wrapper.querySelector(".layout-main-section") || wrapper;
			this.host.classList.add("cnaw-host");
			this.host.addEventListener("click",e=>this.click(e));
			this.host.addEventListener("change",e=>this.change(e));
			this.host.addEventListener("input",e=>{
				if (e.target.dataset.filter!=="keyword") return;
				this.search(e.target.value);
			});
			this.routeHandler=()=>{if (root.frappe.get_route?.()?.[0] !== this.route) this.hide();};
			root.frappe.router?.on?.("change",this.routeHandler);
			this.show();
		}
		show() {
			this.active=true;
			if (!this.loading && (!this.data || this.needsReload || this.resultKey!==this.selectionKey())) this.load();
			else if (this.data) this.render();
		}
		selectionKey() {
			return JSON.stringify([this.start,this.view==="review",Object.entries(this.filters).filter(([key])=>key!=="review").sort(([a],[b])=>a.localeCompare(b))]);
		}
		search(value) {
			this.filters.keyword=value;this.start=0;this.needsReload=true;this.seq++;
			root.clearTimeout(this.timer);
			this.timer=root.setTimeout(()=>this.load(),350);
		}
		hide() {this.active=false;this.seq++;this.loading=false;root.clearTimeout(this.timer);this.closeDetail();}
		async call(method,args) {return (await root.frappe.call({method:ENDPOINT+method,args})).message;}
		async load(refresh=false) {
			root.clearTimeout(this.timer);
			this.needsReload=true;
			this.pendingRefresh=!!(refresh || this.pendingRefresh);
			refresh=this.pendingRefresh;
			const seq=++this.seq;this.loading=true;this.error="";
			this.render();
			try {
				const args={filters:{...this.filters,review:this.view==="review"}};
				if (!refresh) args.start=this.start;
				const data=await this.call(refresh ? "refresh_data" : "get_data",args);
				if (seq!==this.seq || !this.active) return;
				this.data=data;this.filters={...this.filters,...data.filters};
				this.resultKey=this.selectionKey();this.needsReload=false;this.pendingRefresh=false;
			} catch {if (seq===this.seq && this.active) this.error=t("Approvals could not be loaded. Retry or ask an administrator to check the source.");}
			finally {
				if (seq===this.seq && this.active) {
					this.loading=false;this.render();
				}
			}
		}
		option(value,label,current) {return `<option value="${esc(value)}" ${value===current ? "selected" : ""}>${esc(label)}</option>`;}
		selectFilter(name,label,options) {
			return `<label><span>${esc(t(label))}</span><select data-filter="${name}">${this.option("",t("All"),this.filters[name])}${options.map(row=>this.option(row.value,row.label,this.filters[name])).join("")}</select></label>`;
		}
		render() {
			if (!this.active) return;
			const focused=root.document?.activeElement;
			const focusKey=this.host.contains?.(focused) ? focused?.dataset?.filter : null;
			const selection=focusKey==="keyword" ? focused.selectionStart : null;
			const data=this.data, summary=data?.summary, options=data?.options || {}, f=this.filters;
			this.host.innerHTML=`<div class="cnaw" aria-busy="${this.loading}">
				<header class="cnaw-header"><div><div class="cnaw-eyebrow">CN <span>/</span> ${esc(t("APPROVAL WORKSPACE"))}</div><h1>${esc(t("Budgets and expenditure"))}</h1><p>${esc(t("Budget declarations, operating expenditure and purchase expenditure in one place."))}</p></div>
				<div class="cnaw-header-actions"><span class="cnaw-updated">${data ? esc(t("Updated {0}",[data.fetched_at])) : esc(t("Read-only approval data"))}</span><button class="cnaw-button primary" data-action="refresh" ${this.loading ? "disabled" : ""}>↻ ${esc(t(this.loading ? "Loading approvals…" : "Refresh approvals"))}</button></div></header>
				<div class="cnaw-nav" role="tablist" aria-label="${esc(t("Workspace views"))}">${Object.entries(VIEWS).map(([key,label])=>`<button role="tab" aria-selected="${key===this.view}" data-view="${key}" class="${key===this.view ? "active" : ""}">${esc(t(label))}</button>`).join("")}<span>${esc(t("Approval does not confirm payment"))}</span></div>
				<section class="cnaw-filters" aria-label="${esc(t("Approval filters"))}"><div class="cnaw-kind-switch" role="group" aria-label="${esc(t("Approval form"))}">${[["","All approvals"],...Object.entries(KINDS)].map(([key,label])=>`<button data-kind="${key}" aria-pressed="${key===f.kind}" class="${key===f.kind ? "active" : ""}">${key ? `<i class="${key}"></i>` : ""}${esc(t(label))}</button>`).join("")}</div>
				<div class="cnaw-filter-grid"><label><span>${esc(t("From month"))}</span><input type="month" data-filter="from_month" value="${esc(f.from_month)}"></label><label><span>${esc(t("To month"))}</span><input type="month" data-filter="to_month" value="${esc(f.to_month)}"></label>
				${this.selectFilter("entity","Source organization",(options.entities || []).map(value=>({value,label:value})))}
				${this.selectFilter("status","Approval status",Object.entries(STATUSES).map(([value,label])=>({value,label:t(label)})))}
				${this.selectFilter("currency","Currency",(options.currencies || []).map(value=>({value,label:value})))}
				<label class="cnaw-search"><span>${esc(t("Search approvals"))}</span><input type="search" data-filter="keyword" value="${esc(f.keyword)}" placeholder="${esc(t("Number, applicant or description"))}"></label><button class="cnaw-reset" data-action="reset">${esc(t("Reset"))}</button></div>
				<div class="cnaw-period-note">${esc(t("Budgets use the budget month; operating and purchase approvals use the application date. Amounts stay in their original currencies."))}${f.category ? `<button class="cnaw-filter-chip" data-action="clear-category">${esc(f.category)} ×</button>` : ""}</div></section>
				${this.error ? `<div class="cnaw-error" role="alert">${esc(this.error)} ${data ? esc(t("The previous result is still displayed. Its filters may differ from your current selection.")) : ""}<button data-action="retry">${esc(t("Retry"))}</button></div>` : ""}
				<div class="cnaw-body ${this.loading && data ? "is-loading" : ""}">${!data ? this.loading ? `<div class="cnaw-skeleton"><div></div><div></div><div></div><p>${esc(t("Reading approval data…"))}</p></div>` : `<div class="cnaw-empty"><strong>${esc(t("No data loaded"))}</strong></div>` : this.content(summary)}</div>
				<footer class="cnaw-footer">${esc(t("Only approvals explicitly marked for China are included. Correct source discrepancies in the original approval, then refresh."))}</footer></div>`;
			if (focusKey) {const field=this.host.querySelector(`[data-filter="${focusKey}"]`);field?.focus();if (focusKey==="keyword")field?.setSelectionRange(selection,selection);}
		}
		cards(summary) {
			return `<section class="cnaw-cards">${summary.cards.map(card=>`<button class="cnaw-card ${card.kind}" data-kind="${card.kind}"><span class="cnaw-card-heading">${typeLabel(card.kind)}<b>↗</b></span><span class="cnaw-card-amount">${totalsHTML(card.totals)}</span><span class="cnaw-card-caption">${esc(t("Approved application amount"))}</span><span class="cnaw-card-bottom"><strong>${card.count}</strong> ${esc(t("approvals"))}<span>${esc(t("{0} in progress",[card.pending]))}</span></span></button>`).join("")}
			<button class="cnaw-card review" data-view="review"><span class="cnaw-card-heading">${esc(t("To review"))}<b>↗</b></span><span class="cnaw-review-number">${summary.review_count}<small> / ${summary.count}</small></span><span class="cnaw-card-caption">${esc(t("Approvals with detected issues"))}</span><span class="cnaw-card-bottom">${esc(t("Inspect amounts, periods and organizations"))}</span></button></section>`;
		}
		content(summary) {
			if (this.view==="review") return this.review(summary)+this.register();
			if (this.view==="register") return this.cards(summary)+this.register();
			const panels=`<section class="cnaw-panels">${this.trend(summary)}${this.categories(summary)}</section>`;
			if (this.view==="analysis") return this.cards(summary)+panels+this.monthTable(summary);
			return this.cards(summary)+panels+`<section class="cnaw-panel"><div class="cnaw-panel-title"><div><h2>${esc(t("Recent applications"))}</h2><p>${esc(t("One row per approval; detail tables are not counted again."))}</p></div><button class="cnaw-link" data-view="register">${esc(t("View register"))} →</button></div>${tableHTML(this.data.rows,this.data.issue_labels,true)}</section>`;
		}
		trend(summary) {
			const unit=this.filters.currency || (this.data.options.currencies.includes("CNY") ? "CNY" : this.data.options.currencies[0]) || "CNY";
			const groups=chartData(summary.months,unit), max=Math.max(1,...groups.flatMap(group=>group.values.map(row=>row.amount || 0)));
			return `<article class="cnaw-panel"><div class="cnaw-panel-title"><div><h2>${esc(t("Monthly approved applications"))}</h2><p>${esc(t("Separate amounts by approval form"))} · ${esc(unit)}</p></div><span class="cnaw-tag">${esc(this.filters.from_month)} — ${esc(this.filters.to_month)}</span></div>
			<div class="cnaw-chart-legend">${Object.keys(KINDS).map(typeLabel).join("")}</div>${!groups.length ? `<div class="cnaw-chart-empty">${esc(t("No approved amounts in this period"))}</div>` : `<div class="cnaw-chart" role="img" aria-label="${esc(t("Monthly approved applications"))}">${groups.map(group=>`<div class="cnaw-chart-group"><div class="cnaw-bars">${group.values.map(row=>`<div class="cnaw-bar ${row.kind} ${row.amount===null ? "unknown" : ""}" style="height:${row.amount===null ? 12 : Math.max(2, row.amount/max*140)}px" title="${esc(t(KINDS[row.kind]))}: ${esc(formatMoney(row.amount,unit))}"><span>${esc(row.amount===null ? "?" : "")}</span></div>`).join("")}</div><span>${esc(group.month)}</span></div>`).join("")}</div>`}</article>`;
		}
		categories(summary) {
			const groups=this.view==="analysis" ? summary.categories : summary.categories.slice(0,6);
			return `<article class="cnaw-panel"><div class="cnaw-panel-title"><div><h2>${esc(t("Approved amounts by category"))}</h2><p>${esc(t("Select a category to inspect its approvals"))}</p></div><span class="cnaw-tag">${summary.categories.length} ${esc(t("categories"))}</span></div><div class="cnaw-category-list">${groups.length ? groups.map((group,index)=>`<button data-category="${esc(group.label)}" data-category-kind="${group.kind}"><span class="cnaw-rank">${String(index+1).padStart(2,"0")}</span><span class="cnaw-category-name"><strong>${esc(group.label || t("Unclassified"))}</strong><small>${esc(t(KINDS[group.kind]))} · ${esc(t("{0} approvals",[group.count]))}</small></span><span class="cnaw-category-amount">${totalsHTML(group.totals)}</span><span>›</span></button>`).join("") : `<div class="cnaw-chart-empty">${esc(t("No approved amounts in this period"))}</div>`}</div></article>`;
		}
		register() {
			const data=this.data, page=Math.floor(data.start/25)+1, pages=Math.max(1,Math.ceil(data.total/25));
			return `<section class="cnaw-panel"><div class="cnaw-panel-title"><div><h2>${esc(t(this.view==="review" ? "Review queue" : "Approval register"))} <span class="cnaw-count">${data.total}</span></h2><p>${esc(t("One row per approval; detail tables are not counted again."))}</p></div>${data.can_export ? `<button class="cnaw-button" data-action="export">↓ ${esc(t("Export Excel"))}</button>` : ""}</div>${tableHTML(data.rows,data.issue_labels)}<div class="cnaw-pagination"><span>${esc(t("{0} approvals",[data.total]))}</span><button data-page="${Math.max(0,this.start-25)}" ${this.start===0 || this.loading ? "disabled" : ""}>← ${esc(t("Previous"))}</button><span>${page} / ${pages}</span><button data-page="${this.start+25}" ${this.start+25>=data.total || this.loading ? "disabled" : ""}>${esc(t("Next"))} →</button></div></section>`;
		}
		review(summary) {
			return `<section class="cnaw-review-summary"><div><span class="cnaw-eyebrow">${esc(t("DATA QUALITY"))}</span><h2>${esc(t("{0} approvals need attention",[summary.review_count]))}</h2><p>${esc(t("Review source discrepancies without changing approval or accounting records."))}</p><small>${esc(t("Undated approvals appear here only and are excluded from period totals."))}</small></div><div class="cnaw-issue-chips"><button data-issue="" class="${!this.filters.issue ? "active" : ""}">${esc(t("All issues"))}</button>${Object.entries(summary.issues).map(([key,count])=>`<button data-issue="${key}" class="${this.filters.issue===key ? "active" : ""}">${esc(t(this.data.issue_labels[key]))}<b>${count}</b></button>`).join("")}</div></section>`;
		}
		monthTable(summary) {
			return `<section class="cnaw-panel"><div class="cnaw-panel-title"><h2>${esc(t("Monthly breakdown"))}</h2></div><div class="cnaw-table-wrap"><table class="cnaw-table"><thead><tr>${["Reporting month","Approval form","Approvals","Approved application amount"].map(label=>`<th>${esc(t(label))}</th>`).join("")}</tr></thead><tbody>${summary.months.map(row=>`<tr><td>${esc(row.month)}</td><td>${typeLabel(row.kind)}</td><td>${row.count}</td><td>${totalsHTML(row.totals)}</td></tr>`).join("") || `<tr><td colspan="4">${esc(t("No approved amounts in this period"))}</td></tr>`}</tbody></table></div></section>`;
		}
		change(event) {
			const key=event.target.dataset.filter;if (!key || key==="keyword") return;
			this.filters[key]=event.target.value;this.start=0;this.load();
		}
		click(event) {
			const el=event.target.closest("button");if (!el || el.disabled) return;
			if (el.dataset.detail) {this.openDetail(el.dataset.detail);return;}
			if (el.dataset.page!==undefined) {this.start=Number(el.dataset.page);this.load();return;}
			if (el.dataset.kind!==undefined) {this.filters.kind=el.dataset.kind;this.filters.category="";this.filters.issue="";this.start=0;this.load();return;}
			if (el.dataset.view) {this.closeDetail();this.view=el.dataset.view;this.start=0;this.filters.issue="";this.load();return;}
			if (el.dataset.category!==undefined) {this.filters.category=el.dataset.category;this.filters.kind=el.dataset.categoryKind;this.filters.status="approved";this.filters.issue=el.dataset.category ? "" : "classification";this.view="register";this.start=0;this.load();return;}
			if (el.dataset.issue!==undefined) {this.filters.issue=el.dataset.issue;this.start=0;this.load();return;}
			if (el.dataset.action==="refresh") {this.start=0;this.load(true);}
			if (el.dataset.action==="retry") this.load();
			if (el.dataset.action==="clear-category") {this.filters.category="";this.start=0;this.load();}
			if (el.dataset.action==="reset") {this.filters={from_month:monthNow(),to_month:monthNow(),kind:"",status:"",entity:"",currency:"",keyword:"",category:"",issue:""};this.start=0;this.load();}
			if (el.dataset.action==="export") this.export(el);
		}
		async export(button) {
			button.disabled=true;
			try {
				const file=await this.call("export_data",{filters:{...this.filters,review:this.view==="review"}});
				const bytes=Uint8Array.from(root.atob(file.content),c=>c.charCodeAt(0));
				const url=root.URL.createObjectURL(new Blob([bytes],{type:"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"}));
				const link=root.document.createElement("a");link.href=url;link.download=file.filename;link.click();root.setTimeout(()=>root.URL.revokeObjectURL(url),1000);
			} catch {root.frappe.msgprint(t("Export failed. Retry with a smaller selection."));}
			finally {button.disabled=false;}
		}
		closeDetail() {this.detailSeq++;this.overlay?.remove();this.overlay=null;this.previousFocus?.focus?.();}
		async openDetail(key) {
			this.closeDetail();const seq=++this.detailSeq;this.previousFocus=root.document.activeElement;
			const overlay=root.document.createElement("div");overlay.className="cnaw-overlay";this.overlay=overlay;
			overlay.innerHTML=`<section class="cnaw-drawer" role="dialog" aria-modal="true" aria-label="${esc(t("Approval detail"))}"><div class="cnaw-drawer-header"><h2>${esc(t("Approval detail"))}</h2><button class="cnaw-close" aria-label="${esc(t("Close"))}">×</button></div><div class="cnaw-drawer-body">${esc(t("Loading approvals…"))}</div></section>`;
			root.document.body.appendChild(overlay);overlay.querySelector("button").focus();
			overlay.addEventListener("click",event=>{if(event.target===overlay || event.target.closest(".cnaw-close"))this.closeDetail();});
			overlay.addEventListener("keydown",event=>{
				if(event.key==="Escape")this.closeDetail();
				if(event.key==="Tab") {const nodes=overlay.querySelectorAll('button,a[href],summary');const first=nodes[0],last=nodes[nodes.length-1];if(event.shiftKey && root.document.activeElement===first){event.preventDefault();last.focus();}else if(!event.shiftKey && root.document.activeElement===last){event.preventDefault();first.focus();}}
			});
			try {const data=await this.call("get_detail",{key});if(seq===this.detailSeq && this.active)overlay.querySelector(".cnaw-drawer-body").innerHTML=this.detailHTML(data);}
			catch {if(seq===this.detailSeq)overlay.querySelector(".cnaw-drawer-body").textContent=t("This approval is unavailable or you do not have permission to view it.");}
		}
		detailHTML(row) {
			const url=safeApprovalURL(row.approval_url), labels=this.data?.issue_labels || {};
			const fact=(label,value,translate=true)=>`<div><dt>${esc(translate ? t(label) : label)}</dt><dd>${esc(value || "—")}</dd></div>`;
			return `<div class="cnaw-detail-top">${typeLabel(row.kind)}${badge(row.status)}<h3>${esc(row.number)}</h3><div class="cnaw-detail-amount">${esc(formatMoney(row.amount,row.currency))}<small>${esc(t("Application amount"))} · ${esc(row.currency || "—")}</small></div>${url ? `<a class="cnaw-button primary" href="${esc(url)}" target="_blank" rel="noopener noreferrer">${esc(t("Open original approval"))} ↗</a>` : `<span class="cnaw-muted">${esc(t("Original approval link is unavailable"))}</span>`}</div>
			${row.issues.length ? `<section class="cnaw-detail-issues"><h4>${esc(t("Review issues"))}</h4><ul>${row.issues.map(issue=>`<li>${esc(t(labels[issue] || issue))}</li>`).join("")}</ul></section>` : ""}
			<dl class="cnaw-facts">${fact("Source organization",row.entity)}${fact("Company",row.company)}${fact("Applicant",row.applicant)}${fact("Application date",row.application_date)}${fact("Reporting month",row.month)}${fact("Business classification",row.classification.join(" / "))}${row.facts.map(item=>fact(item.label,item.value)).join("")}</dl>
			${row.description ? `<section class="cnaw-detail-section"><h4>${esc(t("Description"))}</h4><p class="cnaw-prose">${esc(row.description)}</p></section>` : ""}
			${row.budget_snapshot.budget!==null || row.budget_snapshot.used!==null ? `<section class="cnaw-budget-snapshot"><h4>${esc(t("Budget snapshot in the approval"))}</h4><p>${esc(t("Values entered when applying; these are not the current budget balance."))}</p><dl class="cnaw-facts">${fact("Budget amount in form",row.budget_snapshot.budget)}${fact("Used amount in form",row.budget_snapshot.used)}</dl></section>` : ""}
			${row.tables.map(table=>`<details class="cnaw-source-table" open><summary>${esc(table.label)} <span>${table.rows.length} ${esc(t("rows"))}</span></summary>${!table.is_amount ? `<p>${esc(t("Supporting details; not added to the application amount."))}</p>` : ""}${table.rows.map(detail=>`<div class="cnaw-source-row"><strong>${String(detail.index).padStart(2,"0")} · ${esc(detail.label || t("Detail row"))}</strong><dl>${detail.cells.map(cell=>fact(cell.label,cell.value,false)).join("")}</dl></div>`).join("")}</details>`).join("")}
			${row.detail_total!==null ? `<p class="cnaw-detail-total">${esc(t("Detail total"))}<strong>${esc(formatMoney(row.detail_total,row.detail_currency || row.currency))}</strong></p>` : ""}
			${row.attachments.length ? `<section class="cnaw-detail-section"><h4>${esc(t("Supporting files"))}</h4><ul>${row.attachments.map(name=>`<li>${esc(name)}</li>`).join("")}</ul><small>${esc(t("Open the original approval to view its attachments."))}</small></section>` : ""}`;
		}
	}
	return {Workbench,esc,formatMoney,safeApprovalURL,chartData,tableHTML,totalsHTML,ROUTES};
});
