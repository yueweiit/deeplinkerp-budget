(function (root, factory) {
	const api = factory(root);
	if (typeof module === "object" && module.exports) module.exports = api;
	root.CNApprovalWorkbench = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function (root) {
	"use strict";
	const ENDPOINT = "budget.api.cn_approvals.";
	const AUTO_SYNC_BACKOFF = 15 * 60 * 1000;
	const KINDS = {budget:"Budget declaration", operating:"Operating expenditure", purchase:"Purchase expenditure"};
	const STATUSES = {approved:"Approved", pending:"In progress", rejected:"Rejected", terminated:"Terminated", withdrawn:"Withdrawn", unknown:"Unknown"};
	const VIEWS = {overview:"Management Overview", budget:"Budget Execution", register:"Approval Register", analysis:"Expenditure Analysis", reports:"Management Reports", review:"Data Review"};
	const ROUTES = {"china-expenses":"overview", "china-expense-details":"register", "china-expense-reports":"analysis", "china-expense-review":"review", "china-budget-execution":"budget", "china-management-reports":"reports"};
	const VIEW_ROUTES = {overview:"china-expenses", register:"china-expense-details", analysis:"china-expense-reports", review:"china-expense-review", budget:"china-budget-execution", reports:"china-management-reports"};
	const GROUPS = {overview:"Budget and Expenditure", budget:"Budget and Expenditure", analysis:"Management Analysis", reports:"Management Analysis", register:"Details and Review", review:"Details and Review"};
	const FX_BASES = {budget:"Budget month", application:"Application month"};
	const t = (s, args) => root.__ ? root.__(s, args) : s.replace(/\{(\d+)\}/g, (_, n) => args?.[Number(n)] ?? "");
	const esc = value => String(value ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
	function userError(error, fallback) {
		// Keep Frappe's translated validation message (including missing rates),
		// never expose traceback/exception fields. Callers render it as text.
		const response=error?.responseJSON;
		if (["ValidationError","PermissionError"].includes(response?.exc_type)) {
			try {
				const messages=JSON.parse(response._server_messages), last=messages.at(-1);
				const message=typeof last==="string" ? JSON.parse(last).message : last?.message;
				if (typeof message==="string" && message.length<=1500) return message;
			} catch { /* Invalid server message: use the local translated fallback. */ }
		}
		return t(fallback);
	}
	function prepareSidebarNavigation(event) {
		if (event.defaultPrevented || event.button!==0 || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
		const frappe=root.frappe, current=frappe?.get_route?.()?.[0];
		if (!frappe?.boot?.cn_budget_workbench || !ROUTES[current] || frappe.app?.sidebar?.editor?.edit_mode) return;
		const link=event.target.closest?.(".body-sidebar a[href]");
		if (!link || (link.target && link.target!=="_self") || link.hasAttribute("download") || link.hasAttribute("onclick")) return;
		let url;
		try {url=new URL(link.href,root.location.href);} catch {return;}
		const route=url.pathname.replace(/^\/desk\//, "").replace(/\/$/, "");
		if (url.origin!==new URL(root.location.href).origin || !url.pathname.startsWith("/desk/") || !ROUTES[route] || route===current || url.search || url.hash) return;
		const work=frappe.pages?.[current]?.cn_expenses;
		if (!work?.active) return;
		work.syncHistory();
		// Supply filters before Frappe's body click handler routes the native link.
		// Leave link handling, sidebar state and browser history to the framework.
		frappe.route_options={cn_approval_filters:work.navigationFilters()};
	}
	function installNavigation() {
		const frappe = root.frappe;
		if (root.document?.addEventListener && !frappe.cn_approval_sidebar_installed) {
			root.document.addEventListener("click",prepareSidebarNavigation,true);
			frappe.cn_approval_sidebar_installed=true;
		}
		for (const [route, view] of Object.entries(ROUTES)) {
			if (frappe.boot?.page_info?.[route]) frappe.boot.page_info[route].title = VIEWS[view];
			if (root.locals?.Page?.[route]) root.locals.Page[route].title = VIEWS[view];
		}
		const breadcrumbs = frappe.breadcrumbs;
		if (!breadcrumbs || breadcrumbs.cn_approval_installed) return;
		const original = breadcrumbs.set_custom_breadcrumbs;
		breadcrumbs.set_custom_breadcrumbs = function (options) {
			const view = ROUTES[frappe.get_route?.()?.[0]];
			if (!view || !frappe.boot?.cn_budget_workbench) return original.call(this, options);
			this.append_breadcrumb_element("/desk/china-expenses", esc(t("China Operating Expenses")));
			this.append_breadcrumb_element(null, esc(t(GROUPS[view])), "cnaw-breadcrumb-group");
			const item = root.document.createElement("li"), current = root.document.createElement("span");
			current.className = "cnaw-breadcrumb-current";
			current.setAttribute("aria-current", "page");
			current.textContent = t(VIEWS[view]);
			item.appendChild(current);this.$breadcrumbs.append(item);
		};
		breadcrumbs.cn_approval_installed = true;
	}
	function monthNow() {return root.frappe?.datetime?.get_today?.().slice(0, 7) || new Date().toISOString().slice(0, 7);}
	function formatMoney(value, currency) {
		if (value === null || value === undefined || value === "" || !Number.isFinite(Number(value))) return t("Needs review");
		const integer=String(value).match(/^-?(\d+)(?:\.\d+)?$/)?.[1];
		if (integer && BigInt(integer)>=1000000000000n && root.DeepLinkERPMexicoExpenseMoney && root.document?.createElement) {
			// The shared formatter retains decimal cents even above Number's safe
			// precision. Return plain text here; each HTML caller escapes it once.
			const holder=root.document.createElement("span"), known=currency && currency!=="unknown";
			holder.innerHTML=root.DeepLinkERPMexicoExpenseMoney.format(value,known ? currency : null);
			return holder.textContent + (known ? "" : ` · ${t("Unknown currency")}`);
		}
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
			return {kind,amount:total ? total.amount : 0};
		})}));
	}
	function badge(status) {return `<span class="cnaw-badge ${esc(status)}">${esc(t(STATUSES[status] || "Unknown"))}</span>`;}
	function typeLabel(kind) {return `<span class="cnaw-type ${esc(kind)}"><i></i>${esc(t(KINDS[kind] || kind))}</span>`;}
	function approvalMoney(row) {
		const original=esc(formatMoney(row.amount,row.currency));
		if (!row.reporting_currency) return `${original}<small>${esc(row.currency || t("Unknown currency"))}</small>`;
		return `${esc(formatMoney(row.converted_amount,row.reporting_currency))}<small>${esc(row.reporting_currency)}</small><small>${esc(t("Original amount"))}: ${original} · ${esc(row.currency || t("Unknown currency"))}</small>`;
	}
	function tableHTML(rows, labels, compact = false) {
		if (!rows.length) return `<div class="cnaw-empty"><span>⌕</span><strong>${esc(t("No approvals match these filters"))}</strong><p>${esc(t("Adjust the period or filters, then try again."))}</p></div>`;
		return `<div class="cnaw-table-wrap"><table class="cnaw-table"><thead><tr>${["Approval / applicant","Source organization","Reporting period","Business classification",rows[0].reporting_currency ? "Converted amount" : "Application amount","Approval status","Review"].map(label=>`<th>${esc(t(label))}</th>`).join("")}</tr></thead><tbody>${rows.slice(0, compact ? 8 : 25).map(row=>`<tr>
			<td><button class="cnaw-link" data-detail="${esc(row.key)}">${esc(row.number)}</button><div class="cnaw-subline">${typeLabel(row.kind)}<span>${esc(row.applicant || "—")}</span></div></td>
			<td><div class="cnaw-cell-title">${esc(row.company || t("Company not matched"))}</div><small>${esc(row.entity || t("Unspecified organization"))}</small></td>
			<td>${esc(row.month || t("Unassigned period"))}<small>${esc(row.application_date || "—")}</small></td>
			<td><div class="cnaw-cell-title">${esc(row.line_label || row.category || t("Unclassified"))}</div><small>${esc(row.source_path?.join(" / ") || row.classification?.slice(1).join(" / ") || "—")}${row.budget_state?` · ${esc(t({effective:"Effective budget",superseded:"Superseded budget",unresolved:"Needs review"}[row.budget_state]))}`:""}</small></td>
			<td class="cnaw-number">${approvalMoney(row)}</td>
			<td>${badge(row.status)}</td><td><button class="cnaw-review-link ${row.issues.length ? "has-issues" : ""}" data-detail="${esc(row.key)}" title="${esc(row.issues.map(key=>t(labels[key] || key)).join(" · "))}">${row.issues.length ? esc(t("{0} issues",[row.issues.length])) : esc(t("No detected issues"))}</button></td>
		</tr>`).join("")}</tbody></table></div>`;
	}
	class Workbench {
		constructor(wrapper, route) {
			this.wrapper=wrapper; this.route=route; this.view=ROUTES[route] || "overview"; this.seq=0; this.detailSeq=0;
			this.filters=this.defaultFilters();
			this.reportId="company";
			this.start=0; this.data=null; this.active=true; this.loading=false;
			installNavigation();
			this.page=root.frappe.ui.make_app_page({parent:wrapper,title:t(VIEWS[this.view]),single_column:true});
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
		defaultFilters() {return {from_month:monthNow(),to_month:monthNow(),kind:"",status:"",company:"",currency:"",reporting_currency:"CNY",keyword:"",category:"",issue:"",management_category:"",measure:"",unassigned:"",project:"",no_project:""};}
		managementView() {return ["overview","budget","analysis","reports"].includes(this.view);}
		normalizeViewFilters() {
			if(!this.managementView())return;
			let changed=false;
			for(const key of ["kind","status","category","keyword","issue","measure","project","no_project"]) {
				if(this.filters[key]){this.filters[key]="";changed=true;}
			}
			if(changed){this.seq++;this.loading=false;this.start=0;this.needsReload=true;}
		}
		show() {
			if (!this.active) this.autoSyncAttempted=false;
			this.active=true;
			// The removed source-organization control must not leave a hidden
			// filter behind when reopening old bookmarks or cached Pages.
			if (this.filters.entity) {this.seq++;this.loading=false;this.start=0;this.needsReload=true;}
			delete this.filters.entity;
			const incoming=root.frappe?.route_options?.cn_approval_filters;
			if (incoming && typeof incoming==="object" && !Array.isArray(incoming)) {
				const previous=this.selectionKey();
				for (const key of Object.keys(this.filters)) {
					if (typeof incoming[key]==="string") this.filters[key]=incoming[key];
				}
				// Old bookmarked URLs selected original currency. The new visible
				// control is reporting currency; never retain a hidden original filter.
				if ("reporting_currency" in this.filters) {
					if (typeof incoming.reporting_currency!=="string" || !incoming.reporting_currency.trim()) this.filters.reporting_currency=["CNY","USD","MXN"].includes(incoming.currency) ? incoming.currency : "CNY";
					this.filters.currency="";
				}
				if (this.selectionKey()!==previous) {
					// A same-Page route change does not hide this instance. Invalidate
					// its old request before allowing the new selection to load.
					this.seq++;this.loading=false;this.start=0;this.needsReload=true;
					root.clearTimeout(this.timer);
				}
				delete root.frappe.route_options.cn_approval_filters;
			}
			if(this.view==="reports") {
				const incomingReport=root.frappe?.route_options?.cn_report;
				if(["company","budget","trend","hr","purchase"].includes(incomingReport)&&incomingReport!==this.reportId) {
					this.reportId=incomingReport;
					this.seq++;this.loading=false;this.start=0;this.needsReload=true;
					root.clearTimeout(this.timer);
				}
				if(root.frappe?.route_options)delete root.frappe.route_options.cn_report;
			}
			this.normalizeViewFilters();
			this.syncHistory();
			this.updateNavigation();
			if (!this.loading && (!this.data || this.needsReload || this.resultKey!==this.selectionKey())) this.load();
			else if (this.data) {this.render();if (this.data.sync) this.pollSync();}
		}
		updateNavigation() {
			this.page?.set_title?.(t(VIEWS[this.view]));
			if (this.wrapper) this.wrapper.label=t(VIEWS[this.view]);
			root.frappe?.breadcrumbs?.add({type:"Custom", label:t(VIEWS[this.view])});
		}
		routeFilters() {
			return Object.fromEntries(Object.entries(this.filters).filter(([key,value])=>key!=="review" && key!=="entity" && typeof value==="string"));
		}
		navigationFilters(changes={}) {return {...this.routeFilters(),issue:"",...changes};}
		syncHistory() {
			if (!this.active || !root.history?.replaceState || !root.location?.href || root.frappe?.get_route?.()?.[0]!==this.route) return;
			const url=new URL(root.location.href);
			// set_route changes the URL before rendering the destination Page.
			if (url.pathname.replace(/\/$/,"")!==`/desk/${this.route}`) return;
			url.searchParams.set("cn_approval_filters",JSON.stringify(this.routeFilters()));
			if(this.view==="reports")url.searchParams.set("cn_report",this.reportId||"company");
			if (url.href!==root.location.href) root.history.replaceState(root.history.state,"",url.href);
		}
		navigate(view, changes={}) {
			const route=VIEW_ROUTES[view];if (!route) return;
			this.syncHistory();
			const filters=this.navigationFilters(changes);
			this.closeDetail();
			if (route===this.route) {
				this.view=view;this.filters=filters;this.normalizeViewFilters();this.start=0;this.updateNavigation();this.load();
			} else {
				root.frappe.route_options={cn_approval_filters:filters};
				root.frappe.set_route(route);
			}
		}
		selectionKey() {
			return JSON.stringify([this.start,this.view,this.view==="reports"?this.reportId:null,Object.entries(this.routeFilters()).sort(([a],[b])=>a.localeCompare(b))]);
		}
		search(value) {
			this.filters.keyword=value;this.start=0;this.needsReload=true;this.seq++;
			this.syncHistory();
			root.clearTimeout(this.timer);
			this.timer=root.setTimeout(()=>this.load(),350);
		}
		hide() {this.active=false;this.seq++;this.loading=false;this.optionsSeq=(this.optionsSeq || 0)+1;this.optionsLoading=false;this.syncSeq=(this.syncSeq || 0)+1;this.syncSubmitting=false;root.clearTimeout(this.syncTimer);root.clearTimeout(this.timer);this.disposeRangePicker();this.disposeApprovalSidebar();this.closeDetail();}
		disposeRangePicker() {this.rangePicker?.destroy();this.rangePicker=null;}
		mountRangePicker() {
			const today=()=>root.frappe.datetime.get_today();
			this.rangePicker=new root.DeepLinkERPMexicoExpenseDateRange.RangePicker({
				parent:this.host.querySelector(".cnaw-month-range"),mode:"month",
				value:root.DeepLinkERPMexicoExpenseDateRange.wholeMonths(this.filters.from_month,this.filters.to_month),
				today:today(),getToday:async()=>today(),
				onChange:range=>{
					if (!this.active || root.frappe.get_route?.()?.[0]!==this.route) return;
					this.filters.from_month=range.from_date.slice(0,7);this.filters.to_month=range.to_date.slice(0,7);
					this.start=0;this.load();this.rangePicker?.trigger.focus();
				},
			});
		}
		disposeApprovalSidebar() {
			root.clearTimeout(this.sidebarTimer);
			if (this.sidebarSetupHandler) root.$(root.document).off("sidebar_setup.cn-approvals",this.sidebarSetupHandler);
			this.sidebarSetupHandler=null;this.approvalSidebar?.remove();this.approvalSidebar=null;
		}
		mountApprovalSidebar() {
			if (!this.active || root.frappe.get_route?.()?.[0]!==this.route) return;
			if(this.managementView()){this.disposeApprovalSidebar();return;}
			if (!this.sidebarSetupHandler) {
				this.sidebarSetupHandler=()=>{
					root.clearTimeout(this.sidebarTimer);
					this.sidebarTimer=root.setTimeout(()=>this.mountApprovalSidebar(),0);
				};
				root.$(root.document).on("sidebar_setup.cn-approvals",this.sidebarSetupHandler);
			}
			const top=root.document.querySelector(".body-sidebar-top");if (!top) return;
			if (!this.approvalSidebar?.isConnected) {
				top.querySelector(".cnaw-approval-sidebar")?.remove();
				const group=root.document.createElement("div");group.className="mx-approval-sidebar cnaw-approval-sidebar";
				group.setAttribute("role","group");group.setAttribute("aria-label",t("Approval form"));
				top.prepend(group);this.approvalSidebar=group;
				group.addEventListener("click",event=>{
					if (!this.active || root.frappe.get_route?.()?.[0]!==this.route) return;
					this.click(event);this.approvalSidebar?.querySelector('[aria-pressed="true"]')?.focus();
				});
			}
			const focused=this.approvalSidebar.contains(root.document.activeElement);
			this.approvalSidebar.innerHTML=`<div class="mx-approval-heading">${esc(t("Approval form"))}</div>${[["","All approvals"],...Object.entries(KINDS)].map(([kind,label])=>`<button type="button" class="mx-approval-item" data-kind="${kind}" aria-pressed="${this.filters.kind===kind}"><span>${esc(t(label))}</span><span class="mx-approval-check" aria-hidden="true">✓</span></button>`).join("")}`;
			if (focused) this.approvalSidebar.querySelector('[aria-pressed="true"]')?.focus();
		}
		async call(method,args) {
			// Each caller displays its validation error. Suppress duplicate server
			// message dialogs when options and financial data fail concurrently.
			return (await root.frappe.call({method:ENDPOINT+method,args,silent:true})).message;
		}
		async loadFilterOptions(force=false) {
			if (!this.active || !force && (this.filterOptions || this.optionsLoading)) return;
			const seq=this.optionsSeq=(this.optionsSeq || 0)+1;
			this.optionsLoading=true;this.optionsError="";
			try {
				const options=await this.call("get_filter_options",{});
				if (seq!==this.optionsSeq || !this.active) return;
				this.filterOptions=options;
				if(options.allocation_rebuilding){this.syncState={...this.syncState,allocation_rebuilding:true};this.scheduleSyncPoll();}
			} catch (error) {
				if (seq===this.optionsSeq && this.active) this.optionsError=userError(error,"Subsidiaries could not be loaded. Retry.");
			} finally {
				if (seq===this.optionsSeq && this.active) {this.optionsLoading=false;this.render();}
			}
		}
		async load(refresh=false) {
			if (refresh) return this.refreshApprovals();
			root.clearTimeout(this.timer);
			this.syncHistory();
			this.needsReload=true;
			const syncSeq=this.syncSeq || 0;
			const seq=++this.seq;this.loading=true;this.error="";
			// This request never reads approvals or rates. Do not wait for either
			// service before allowing a user to choose an accessible subsidiary.
			this.loadFilterOptions();
			this.render();
			try {
				const args={filters:{...this.routeFilters(),review:this.view==="review"}};
				args.start=this.start;
				let method=this.managementView()?"get_management":"get_management_lines";
				if(this.view==="reports"){method="get_management_report";args.report_id=this.reportId||"company";}
				if(this.managementView())delete args.start;
				const data=await this.call(method,args);
				if (seq!==this.seq || !this.active) return;
				this.data=data;this.filters={...this.filters,...data.filters};
				delete this.filters.entity;
				this.syncReloadRevision=JSON.stringify([data.sync?.source_id,data.snapshot_id,data.sync?.settings_version,data.sync?.allocation_revision]);
				if (data.options?.companies) {
					this.filterOptions={companies:data.options.companies,reporting_currencies:data.options.reporting_currencies,...(data.options.management_categories?{management_categories:data.options.management_categories}:{}),can_manage_settings:data.options.can_manage_settings};
					// A successful overview carries options matching its data;
					// discard an older independent request still in flight.
					this.optionsSeq=(this.optionsSeq || 0)+1;this.optionsLoading=false;this.optionsError="";
				}
				this.resultKey=this.selectionKey();this.needsReload=false;
				this.syncHistory();
				if (data.sync && syncSeq===(this.syncSeq || 0)) this.acceptSync(data.sync);
			} catch (error) {if (seq===this.seq && this.active) this.error=userError(error,"Approvals could not be loaded. Retry or ask an administrator to check the source.");}
			finally {
				if (seq===this.seq && this.active) {
					this.loading=false;this.render();
				}
			}
		}
		scheduleSyncPoll() {
			root.clearTimeout(this.syncTimer);
			if (!this.active) return;
			this.syncTimer=root.setTimeout(()=>this.pollSync(),this.syncState?.active || this.syncState?.allocation_rebuilding ? 2500 : 30000);
			this.syncTimer?.unref?.();
		}
		acceptSync(state, reload=false) {
			const ownershipWasUpdating=this.syncState?.allocation_rebuilding;
			this.syncState=state;this.syncError="";
			if(state.allocation_rebuilding){this.scheduleSyncPoll();return;}
			const revision=JSON.stringify([state.source_id,state.snapshot_id,state.settings_version,state.allocation_revision]);
			const displayed=JSON.stringify([this.data?.sync?.source_id,this.data?.snapshot_id,this.data?.sync?.settings_version,this.data?.sync?.allocation_revision]);
			if (reload && (ownershipWasUpdating || revision!==displayed && revision!==this.syncReloadRevision)) {
				this.syncReloadRevision=revision;
				// A local reread keeps the current filters. Even a source change to
				// an empty store must replace the previously displayed source.
				this.load();
			}
			if (state.auto_sync_due && (!this.autoSyncAttempted || Date.now()-this.autoSyncAttempted>=AUTO_SYNC_BACKOFF)) {
				this.refreshApprovals(true);
			} else this.scheduleSyncPoll();
		}
		async pollSync() {
			if (!this.active || this.syncSubmitting) return;
			root.clearTimeout(this.syncTimer);
			const seq=this.syncSeq=(this.syncSeq || 0)+1;
			try {
				const state=await this.call("get_sync_status",{});
				if (seq!==this.syncSeq || !this.active) return;
				this.acceptSync(state,true);
			} catch (error) {
				if (seq!==this.syncSeq || !this.active) return;
				this.syncError=userError(error,"Synchronization status could not be loaded. Retrying shortly.");
				this.scheduleSyncPoll();
			} finally {if (seq===this.syncSeq && this.active) this.renderSyncStatus();}
		}
		async refreshApprovals(ifStale=false) {
			if (!this.active || this.syncSubmitting) return;
			root.clearTimeout(this.syncTimer);
			const seq=this.syncSeq=(this.syncSeq || 0)+1;
			this.syncSubmitting=true;this.syncError="";this.autoSyncAttempted=Date.now();
			if (!ifStale) this.loadFilterOptions(true);
			this.renderSyncStatus();
			try {
				const state=await this.call("enqueue_sync",{if_stale:ifStale ? 1 : 0});
				if (seq!==this.syncSeq || !this.active) return;
				this.acceptSync(state,true);
			} catch (error) {
				if (seq!==this.syncSeq || !this.active) return;
				this.syncError=userError(error,"Synchronization could not be queued. Retry.");
				this.scheduleSyncPoll();
			} finally {if (seq===this.syncSeq && this.active) {this.syncSubmitting=false;this.renderSyncStatus();}}
		}
		renderSyncStatus() {
			if (!this.active) return;
			const feedback=this.host?.querySelector(".cnaw-sync-feedback"), html=this.syncHTML();
			if (feedback && feedback.innerHTML!==html) feedback.innerHTML=html;
			const button=this.host?.querySelector('[data-action="refresh"]');
			if (button) {
				const syncing=this.syncSubmitting || this.syncState?.active;
				button.disabled=!!(syncing || this.syncState?.enabled===false);
				button.textContent=`↻ ${t(syncing ? "Synchronizing approvals…" : "Refresh approvals")}`;
			}
		}
		syncHTML() {
			const state=this.syncState;
			if (this.syncError) return `<div class="cnaw-error" role="alert">${esc(this.syncError)}</div>`;
			if (!state) return "";
			let message="";
			if (state.status==="queued") message=t("Approval synchronization is queued.");
			if (state.status==="running") message=t("Synchronizing approvals: {0} of {1} templates, {2} pages read.",[state.completed_templates,state.template_count,state.page_count]);
			if (state.status==="failed") message=t(state.error_code==="sync_timeout" ? "Synchronization timed out. Retry synchronization." : "Synchronization failed. Retry synchronization.");
			if (!state.enabled) message=t("Approval synchronization is disabled.");
			if (message && state.has_snapshot) message+=" "+t("The last complete snapshot remains available.");
			return message ? `<div class="${state.status==="failed" ? "cnaw-error" : "cnaw-sync-status"}" role="status" aria-live="polite">${esc(message)}</div>` : "";
		}
		option(value,label,current) {return `<option value="${esc(value)}" ${value===current ? "selected" : ""}>${esc(label)}</option>`;}
		selectFilter(name,label,options,emptyLabel="All") {
			return `<label><span>${esc(t(label))}</span><select data-filter="${name}">${emptyLabel===null ? "" : this.option("",t(emptyLabel),this.filters[name])}${options.map(row=>this.option(row.value,row.label,this.filters[name])).join("")}</select></label>`;
		}
		render() {
			if (!this.active) return;
			const focused=root.document?.activeElement;
			const focusKey=this.host.contains?.(focused) ? focused?.dataset?.filter : null;
			const selection=focusKey==="keyword" ? focused.selectionStart : null;
			const rangeFocused=focused===this.rangePicker?.trigger;
			// A new snapshot can finish while a month range is half selected.
			// Preserve the open picker unless the committed filter actually changed.
			const rangeParent=this.rangePicker?.popup
				&& this.rangePicker.value.from_date.slice(0,7)===this.filters.from_month
				&& this.rangePicker.value.to_date.slice(0,7)===this.filters.to_month
				? this.rangePicker.parent : null;
			if (!rangeParent) this.disposeRangePicker();
			const data=this.data, summary=data?.summary, options={...data?.options,...this.filterOptions}, f=this.filters;
			const syncing=this.syncSubmitting || this.syncState?.active;
			this.host.innerHTML=`<div class="cnaw" aria-busy="${this.loading}">
				<section class="cnaw-filters" aria-label="${esc(t("Approval filters"))}">
				<div class="cnaw-filter-grid ${this.managementView()?"cnam-filter-grid":""}"><div class="cnaw-range-filter"><span>${esc(t("Month Range"))}</span><div class="cnaw-month-range"></div></div>
				${this.selectFilter("company","Subsidiary",(options.companies || []).map(value=>({value,label:value})),"All Subsidiaries")}
				${!this.managementView()?this.selectFilter("status","Approval status",Object.entries(STATUSES).map(([value,label])=>({value,label:t(label)}))):""}
				${this.selectFilter("reporting_currency","Reporting Currency",(options.reporting_currencies || ["CNY","USD","MXN"]).map(value=>({value,label:value})),null)}
				${this.managementView()?this.selectFilter("management_category","Management category",[{value:"unclassified",label:t("Unclassified")},...Object.entries(data?.options?.management_categories||{}).map(([value,label])=>({value,label:t(label)}))]):`<label class="cnaw-search"><span>${esc(t("Search approvals"))}</span><input type="search" data-filter="keyword" value="${esc(f.keyword)}" placeholder="${esc(t("Number, applicant or description"))}"></label>`}<button class="cnaw-reset" data-action="reset">${esc(t("Reset"))}</button></div>
				<div class="cnaw-filter-toolbar"><div class="cnaw-period-note"><span class="cnaw-current-kind">${esc(t(this.managementView()?"Approval-based budget execution":KINDS[f.kind] || "All approvals"))}</span>${esc(t("Converted at month-start rates: budget month for budgets; application month for operating and purchase approvals. Original amounts are retained."))}${f.unassigned?`<button class="cnaw-filter-chip" data-action="clear-unassigned">${esc(t("Company not matched"))} ×</button>`:""}${f.measure?`<button class="cnaw-filter-chip" data-action="clear-measure">${esc(t(({budget:"Effective budget",approved:"Approved budget occupation",pending:"Pending expenditure applications",operating:"Approved operating expenditure",purchase:"Approved purchase expenditure",spending:"Expenditure applications",superseded:"Superseded budget",declared_budget:"Budget declarations"})[f.measure]||f.measure))} ×</button>`:""}${f.management_category&&!this.managementView()?`<button class="cnaw-filter-chip" data-action="clear-management-category">${esc(t(data?.options?.management_categories?.[f.management_category]||"Unclassified"))} ×</button>`:""}${f.project||f.no_project?`<button class="cnaw-filter-chip" data-action="clear-project">${esc(t("Project"))}: ${esc(f.project||t("Not specified"))} ×</button>`:""}${f.category ? `<button class="cnaw-filter-chip" data-action="clear-category">${esc(f.category)} ×</button>` : ""}<span class="cnaw-payment-note">${esc(t("Approval does not confirm payment"))}</span></div>
				<div class="cnaw-filter-actions">${options.can_manage_settings ? `<button class="cnaw-button" data-action="settings">${esc(t("Synchronization settings"))}</button><button class="cnaw-button" data-action="regional">${esc(t("Cross-region expense allocation"))}</button>` : ""}<span class="cnaw-updated">${data?.fetched_at ? esc(t("Updated {0}",[root.frappe.datetime?.str_to_user?.(data.fetched_at) || data.fetched_at])) : esc(t("Read-only approval data"))}</span><button class="cnaw-button primary" data-action="refresh" ${syncing || this.syncState?.enabled===false ? "disabled" : ""}>↻ ${esc(t(syncing ? "Synchronizing approvals…" : "Refresh approvals"))}</button></div></div></section>
				<div class="cnaw-sync-feedback">${this.syncHTML()}</div>
				${this.error ? `<div class="cnaw-error" role="alert">${esc(this.error)} ${data ? esc(t("The previous result is still displayed. Its filters may differ from your current selection.")) : ""}<button data-action="retry">${esc(t("Retry"))}</button></div>` : ""}
				${this.optionsError ? `<div class="cnaw-error" role="alert">${esc(this.optionsError)} <button data-action="retry-options">${esc(t("Retry"))}</button></div>` : ""}
				<div class="cnaw-body ${this.loading && data ? "is-loading" : ""}">${!data ? this.loading ? `<div class="cnaw-skeleton"><div></div><div></div><div></div><p>${esc(t("Reading approval data…"))}</p></div>` : `<div class="cnaw-empty"><strong>${esc(t("No data loaded"))}</strong></div>` : data.has_snapshot===false ? `<div class="cnaw-empty"><strong>${esc(t("Waiting for the first approval synchronization"))}</strong><p>${esc(t("Approvals will appear after a complete synchronization. You can leave this page while it runs."))}</p></div>` : this.content(summary)}</div>
				<footer class="cnaw-footer">${esc(t("Approvals are included by enabled template without filtering by execution region. Correct source discrepancies in the original approval, then refresh."))}</footer></div>`;
			if (rangeParent) {
				this.host.querySelector(".cnaw-month-range").replaceWith(rangeParent);
				this.rangePicker.position();
			} else this.mountRangePicker();
			this.mountApprovalSidebar();
			if (rangeFocused) this.rangePicker.trigger.focus();
			if (focusKey) {const field=this.host.querySelector(`[data-filter="${focusKey}"]`);field?.focus();if (focusKey==="keyword")field?.setSelectionRange(selection,selection);}
		}
		cards(summary) {
			return `<section class="cnaw-cards">${summary.cards.map(card=>`<button class="cnaw-card ${card.kind}" data-kind="${card.kind}"><span class="cnaw-card-heading">${typeLabel(card.kind)}<b>↗</b></span><span class="cnaw-card-amount">${totalsHTML(card.totals)}</span><span class="cnaw-card-caption">${esc(t("Approved application amount"))}</span><span class="cnaw-card-bottom"><strong>${card.count}</strong> ${esc(t("approvals"))}<span>${esc(t("{0} in progress",[card.pending]))}</span></span></button>`).join("")}
			<button class="cnaw-card review" data-view="review"><span class="cnaw-card-heading">${esc(t("To review"))}<b>↗</b></span><span class="cnaw-review-number">${summary.review_count}<small> / ${summary.count}</small></span><span class="cnaw-card-caption">${esc(t("Approvals with detected issues"))}</span><span class="cnaw-card-bottom">${esc(t("Inspect amounts, periods and organizations"))}</span></button></section>`;
		}
		content(summary) {
			if(this.managementView()&&root.CNApprovalManagement)return root.CNApprovalManagement.render(this);
			if (this.view==="review") return this.review(summary)+this.register();
			if (this.view==="register") return this.register();
			const panels=`<section class="cnaw-panels">${this.trend(summary)}${this.categories(summary)}</section>`;
			if (this.view==="analysis") return this.cards(summary)+panels+this.monthTable(summary);
			return this.cards(summary)+panels+`<section class="cnaw-panel"><div class="cnaw-panel-title"><div><h2>${esc(t("Recent applications"))}</h2><p>${esc(t("One row per approval; detail tables are not counted again."))}</p></div><button class="cnaw-link" data-view="register">${esc(t("View register"))} →</button></div>${tableHTML(this.data.rows,this.data.issue_labels,true)}</section>`;
		}
		trend(summary) {
			const unit=this.data.filters?.reporting_currency || this.data.filters?.currency || (this.data.options.currencies.includes("CNY") ? "CNY" : this.data.options.currencies[0]) || "CNY";
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
			return `<section class="cnaw-panel"><div class="cnaw-panel-title"><div><h2>${esc(t(this.view==="review" ? "Review queue" : "Approval register"))} <span class="cnaw-count">${data.total}</span></h2><p>${esc(t(data.analytical_lines?"Allocated detail lines are shown once; header totals are not added again.":"One row per approval; detail tables are not counted again."))}</p></div>${data.can_export ? `<button class="cnaw-button" data-action="export">↓ ${esc(t("Export Excel"))}</button>` : ""}</div>${tableHTML(data.rows,data.issue_labels)}<div class="cnaw-pagination"><span>${esc(t(data.analytical_lines?"{0} detail lines":"{0} approvals",[data.total]))}</span><button data-page="${Math.max(0,this.start-25)}" ${this.start===0 || this.loading ? "disabled" : ""}>← ${esc(t("Previous"))}</button><span>${page} / ${pages}</span><button data-page="${this.start+25}" ${this.start+25>=data.total || this.loading ? "disabled" : ""}>${esc(t("Next"))} →</button></div></section>`;
		}
		review(summary) {
			return `<section class="cnaw-review-summary"><div><span class="cnaw-eyebrow">${esc(t("DATA QUALITY"))}</span><h2>${esc(t(this.data.analytical_lines?"{0} detail lines need attention":"{0} approvals need attention",[summary.review_count]))}</h2><p>${esc(t("Review source discrepancies without changing approval or accounting records."))}</p><small>${esc(t("Undated approvals appear here only and are excluded from period totals."))}</small></div><div class="cnaw-issue-chips"><button data-issue="" class="${!this.filters.issue ? "active" : ""}">${esc(t("All issues"))}</button>${Object.entries(summary.issues).map(([key,count])=>`<button data-issue="${key}" class="${this.filters.issue===key ? "active" : ""}">${esc(t(this.data.issue_labels[key]))}<b>${count}</b></button>`).join("")}</div></section>`;
		}
		monthTable(summary) {
			return `<section class="cnaw-panel"><div class="cnaw-panel-title"><h2>${esc(t("Monthly breakdown"))}</h2></div><div class="cnaw-table-wrap"><table class="cnaw-table"><thead><tr>${["Reporting month","Approval form","Approvals","Approved application amount"].map(label=>`<th>${esc(t(label))}</th>`).join("")}</tr></thead><tbody>${summary.months.map(row=>`<tr><td>${esc(row.month)}</td><td>${typeLabel(row.kind)}</td><td>${row.count}</td><td>${totalsHTML(row.totals)}</td></tr>`).join("") || `<tr><td colspan="4">${esc(t("No approved amounts in this period"))}</td></tr>`}</tbody></table></div></section>`;
		}
		change(event) {
			const key=event.target.dataset.filter;if (!key || key==="keyword") return;
			if (key==="kind") {this.filters.category="";this.filters.issue="";}
			this.filters[key]=event.target.value;if(key==="company")this.filters.unassigned="";this.start=0;this.load();
		}
		async click(event) {
			const el=event.target.closest("button");if (!el || el.disabled) return;
			if(el.dataset.mgmtNav){
				const changes=JSON.parse(el.dataset.mgmtFilters||"{}");
				this.navigate(el.dataset.mgmtNav,{...(this.data?.filters||{}),kind:"",status:"",category:"",issue:"",keyword:"",measure:"",project:"",no_project:"",...changes});return;
			}
			if(el.dataset.mgmtDetail){this.openDetail(el.dataset.mgmtDetail,true);return;}
			if(el.dataset.exportReport){this.export(el,el.dataset.exportReport);return;}
			if(el.dataset.reportId){this.reportId=el.dataset.reportId;this.start=0;this.load();return;}
			if (el.dataset.detail) {this.openDetail(el.dataset.detail,!!this.data?.analytical_lines);return;}
			if (el.dataset.page!==undefined) {this.start=Number(el.dataset.page);this.load();return;}
			if (el.dataset.kind!==undefined) {this.filters.kind=el.dataset.kind;this.filters.category="";this.filters.issue="";this.start=0;this.load();return;}
			if (el.dataset.view) {this.navigate(el.dataset.view);return;}
			if (el.dataset.category!==undefined) {this.navigate("register",{category:el.dataset.category,kind:el.dataset.categoryKind,status:"approved",issue:el.dataset.category ? "" : "classification"});return;}
			if (el.dataset.issue!==undefined) {this.filters.issue=el.dataset.issue;this.start=0;this.load();return;}
			if (el.dataset.action==="refresh") {this.start=0;this.load(true);}
			if (el.dataset.action==="retry") this.load();
			if (el.dataset.action==="retry-options") this.loadFilterOptions(true);
			if (el.dataset.action==="clear-category") {this.filters.category="";this.start=0;this.load();}
			if (el.dataset.action==="reset") {this.filters=this.defaultFilters();this.start=0;this.load();}
			if (el.dataset.action==="export") this.export(el);
			if (el.dataset.action==="settings") this.openSettings(el);
            if (el.dataset.action==="regional") {
                await root.frappe.require("/assets/deeplinkerp_branding/js/regional_expenses.js");
                await root.RegionalExpenses.list(() => this.load());
            }
			if (el.dataset.action==="toggle-empty-companies") {this.showEmptyCompanies=!this.showEmptyCompanies;this.render();}
			if (el.dataset.action==="clear-unassigned") {this.filters.unassigned="";this.start=0;this.load();}
			if (el.dataset.action==="clear-management-category") {this.filters.management_category="";this.start=0;this.load();}
			if (el.dataset.action==="clear-project") {this.filters.project="";this.filters.no_project="";this.start=0;this.load();}
			if (el.dataset.action==="clear-measure") {this.filters.measure="";this.start=0;this.load();}
		}
		async openSettings(button) {
			if (this.settingsOpening) return;
			this.settingsOpening=true;button.disabled=true;
			try {
				await root.frappe.require("/assets/budget/js/cn_approval_settings.js");
				if (!this.active) return;
				await root.CNApprovalSettings.open(()=>{
					// Cached routes also need fresh company assignments and controls.
					for (const route of Object.keys(ROUTES)) {
						const work=root.frappe.pages?.[route]?.cn_expenses;
						if (!work) continue;
						work.seq++;work.loading=false;work.needsReload=true;work.autoSyncAttempted=false;
						work.filterOptions=null;work.closeDetail();
						if (work.active) work.load();
					}
				});
			} catch(error) {root.frappe.msgprint(esc(userError(error,"CN approval settings could not be loaded or saved. Retry.")));}
			finally {this.settingsOpening=false;button.disabled=false;}
		}
		async export(button,reportId=null) {
			button.disabled=true;
			try {
				const args={filters:{...(this.data?.filters||this.routeFilters()),review:this.view==="review"}};
				if(reportId)args.report_id=reportId;
				const file=await this.call(reportId?"export_management_report":this.data?.analytical_lines?"export_management_lines":"export_data",args);
				const bytes=Uint8Array.from(root.atob(file.content),c=>c.charCodeAt(0));
				const url=root.URL.createObjectURL(new Blob([bytes],{type:"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"}));
				const link=root.document.createElement("a");link.href=url;link.download=file.filename;link.click();root.setTimeout(()=>root.URL.revokeObjectURL(url),1000);
			} catch (error) {root.frappe.msgprint(esc(userError(error,"Export failed. Retry with a smaller selection.")));}
			finally {button.disabled=false;}
		}
		closeDetail() {this.detailSeq++;this.overlay?.remove();this.overlay=null;this.previousFocus?.focus?.();}
		async openDetail(key,analytical=false) {
			this.closeDetail();const seq=++this.detailSeq;this.previousFocus=root.document.activeElement;
			const overlay=root.document.createElement("div");overlay.className="cnaw-overlay";this.overlay=overlay;
			overlay.innerHTML=`<section class="cnaw-drawer" role="dialog" aria-modal="true" aria-label="${esc(t("Approval detail"))}"><div class="cnaw-drawer-header"><h2>${esc(t("Approval detail"))}</h2><button class="cnaw-close" aria-label="${esc(t("Close"))}">×</button></div><div class="cnaw-drawer-body">${esc(t("Loading approvals…"))}</div></section>`;
			root.document.body.appendChild(overlay);overlay.querySelector("button").focus();
			overlay.addEventListener("click",event=>{if(event.target===overlay || event.target.closest(".cnaw-close"))this.closeDetail();});
			overlay.addEventListener("keydown",event=>{
				if(event.key==="Escape")this.closeDetail();
				if(event.key==="Tab") {const nodes=overlay.querySelectorAll('button,a[href],summary');const first=nodes[0],last=nodes[nodes.length-1];if(event.shiftKey && root.document.activeElement===first){event.preventDefault();last.focus();}else if(!event.shiftKey && root.document.activeElement===last){event.preventDefault();first.focus();}}
			});
			try {const data=await this.call(analytical?"get_management_detail":"get_detail",{key,reporting_currency:this.data?.filters?.reporting_currency || ""});if(seq===this.detailSeq && this.active) {
                const body=overlay.querySelector(".cnaw-drawer-body");body.innerHTML=this.detailHTML(data);
                if(data.kind!=="budget" && this.data?.options?.can_manage_settings) {
                    const button=root.document.createElement("button");button.className="cnaw-button";
                    button.textContent=t("Cross-region expense allocation");body.prepend(button);
                    button.onclick=async()=>{await root.frappe.require("/assets/deeplinkerp_branding/js/regional_expenses.js");this.closeDetail();
                        await root.RegionalExpenses.open(data.regional_allocation?{allocation:data.regional_allocation}:{origin:"cn",key:data.source_key||key},()=>this.load());};
                }
            }}
			catch (error) {if(seq===this.detailSeq)overlay.querySelector(".cnaw-drawer-body").textContent=userError(error,"This approval is unavailable or you do not have permission to view it.");}
		}
		detailHTML(row) {
			const url=safeApprovalURL(row.approval_url), labels=this.data?.issue_labels || {};
			const fact=(label,value,translate=true)=>`<div><dt>${esc(translate ? t(label) : label)}</dt><dd>${esc(value || "—")}</dd></div>`;
			return `<div class="cnaw-detail-top">${typeLabel(row.kind)}${badge(row.status)}<h3>${esc(row.number)}</h3><div class="cnaw-detail-amount">${approvalMoney(row)}<small>${esc(t(row.reporting_currency ? "Converted amount" : "Application amount"))}</small></div>${url ? `<a class="cnaw-button primary" href="${esc(url)}" target="_blank" rel="noopener noreferrer">${esc(t("Open original approval"))} ↗</a>` : `<span class="cnaw-muted">${esc(t("Original approval link is unavailable"))}</span>`}</div>
			${row.issues.length ? `<section class="cnaw-detail-issues"><h4>${esc(t("Review issues"))}</h4><ul>${row.issues.map(issue=>`<li>${esc(t(labels[issue] || issue))}</li>`).join("")}</ul></section>` : ""}
			<dl class="cnaw-facts">${fact("Source organization",row.entity)}${fact("Company",row.company)}${fact("Applicant",row.applicant)}${fact("Application date",row.application_date)}${fact("Reporting month",row.month)}${row.budget_state?fact("Budget version",t({effective:"Effective budget",superseded:"Superseded budget",unresolved:"Needs review"}[row.budget_state])):""}${row.approved_at?fact("Approval completion time",row.approved_at):""}${fact("Business classification",row.classification.join(" / "))}${row.facts.map(item=>fact(item.label,item.value)).join("")}</dl>
			${row.reporting_currency ? `<dl class="cnaw-facts">${fact("Reporting Currency",row.reporting_currency)}${fact("Exchange rate",row.exchange_rate)}${fact("Exchange rate date",row.exchange_rate_date)}${fact("Exchange rate basis",t(FX_BASES[row.fx_date_basis] || "Unknown"))}</dl>` : ""}
			${row.description ? `<section class="cnaw-detail-section"><h4>${esc(t("Description"))}</h4><p class="cnaw-prose">${esc(row.description)}</p></section>` : ""}
			${row.budget_snapshot.budget!=null || row.budget_snapshot.used!=null ? `<section class="cnaw-budget-snapshot"><h4>${esc(t("Budget snapshot in the approval"))}</h4><p>${esc(t("Values entered when applying; these are not the current budget balance."))}</p><dl class="cnaw-facts">${fact("Budget amount in form",row.budget_snapshot.budget)}${fact("Used amount in form",row.budget_snapshot.used)}</dl></section>` : ""}
			${row.tables.map(table=>`<details class="cnaw-source-table" open><summary>${esc(table.label)} <span>${table.rows.length} ${esc(t("rows"))}</span></summary>${!table.is_amount ? `<p>${esc(t("Supporting details; not added to the application amount."))}</p>` : ""}${table.rows.map(detail=>`<div class="cnaw-source-row"><strong>${String(detail.index).padStart(2,"0")} · ${esc(detail.label || t("Detail row"))}</strong><dl>${detail.cells.map(cell=>fact(cell.label,cell.value,false)).join("")}</dl></div>`).join("")}</details>`).join("")}
			${row.detail_total!==null ? `<p class="cnaw-detail-total">${esc(t("Detail total"))}<strong>${esc(formatMoney(row.detail_total,row.detail_currency || row.currency))}</strong></p>` : ""}
			${row.attachments.length ? `<section class="cnaw-detail-section"><h4>${esc(t("Supporting files"))}</h4><ul>${row.attachments.map(name=>`<li>${esc(name)}</li>`).join("")}</ul><small>${esc(t("Open the original approval to view its attachments."))}</small></section>` : ""}`;
		}
	}
	return {Workbench,esc,formatMoney,safeApprovalURL,chartData,tableHTML,totalsHTML,userError,ROUTES,VIEW_ROUTES,installNavigation,prepareSidebarNavigation};
});
