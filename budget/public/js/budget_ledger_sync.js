(function (root, factory) {
	if (typeof module === "object" && module.exports) module.exports = factory;
	else root.BudgetLedgerSync = factory(root);
})(typeof globalThis !== "undefined" ? globalThis : this, function (root) {
	"use strict";
	const t = (text, args) => root.__ ? root.__(text, args) : text;
	const esc = value => String(value ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;", "<":"&lt;", ">":"&gt;", '"':"&quot;", "'":"&#39;"}[c]));
	const API = "budget.services.ledger_sync_settings.";
	const call = async (method, args = {}) => (await root.frappe.call({method:API + method, args})).message;
	const manager = () => root.frappe.session?.user === "Administrator" || root.frappe.user.has_role("System Manager");
	const states = {idle:"Not synchronized", queued:"Queued", running:"Synchronizing", success:"Synchronization completed",
		failed:"Synchronization failed", skipped:"Synchronization skipped", unknown:"Synchronization result is unavailable"};
	const reasons = {
		not_configured:"Budget synchronization is not configured. Open Sync Settings.",
		disabled:"Budget synchronization is disabled.",
		source_changed:"The configured source differs from the ledger source. Ask an administrator to review it.",
		settings_changed:"Settings changed after this task was queued. Start synchronization again.",
		running:"Budget synchronization is already running.",
		no_source_records:"No eligible budget approvals were found. Existing ledger data was retained.",
	};
	function statusHTML(data) {
		const settings = data.settings || {}, last = data.last;
		const reason = reasons[settings.blocked_by] || reasons[data.reason];
		return `<div class="budget-sync-status" role="status">
			${reason ? `<p class="text-warning">${esc(t(reason))}</p>` : ""}
			<p><strong>${esc(t(states[data.state] || states.unknown))}</strong></p>
			${data.state === "failed" ? `<p class="text-danger">${esc(t("The last synchronization failed. Existing ledger data was retained. Open the sync log for details."))}</p>` : ""}
			${last ? `<p class="text-muted">${esc(t("Last Run"))}: ${esc(last.started_at || "—")} · ${esc(t(last.status))}</p>` : ""}
			${last?.status === "Success" ? `<p>${esc(t("Created"))}: ${esc(last.created ?? 0)} · ${esc(t("Updated"))}: ${esc(last.updated ?? 0)} · ${esc(t("Unchanged"))}: ${esc(last.unchanged ?? 0)} · ${esc(t("Removed"))}: ${esc(last.removed ?? 0)}</p>` : ""}
			<p>${esc(t("Local Ledger Entries"))}: ${esc(data.ledger_count ?? 0)}</p></div>`;
	}
	function canStart(data) {
		return !!data?.settings?.configured && !!data.settings.enabled && !data.settings.blocked_by && !["queued", "running"].includes(data.state);
	}
	async function openSettings(onSaved = () => {}) {
		if (!manager()) return;
		const settings = await call("get_sync_settings");
		const dialog = new root.frappe.ui.Dialog({title:t("Budget Sync Settings"), size:"large",
			fields:[
				{fieldtype:"HTML", options:`<p class="text-muted">${esc(t("Saving settings does not start synchronization. Database credentials are managed on the server and are never returned to this page."))}</p>`},
				{fieldname:"enabled", fieldtype:"Check", label:t("Enable Budget Synchronization"), default:settings.enabled ? 1 : 0,
					description:t("Controls manual and scheduled budget ledger synchronization.")},
				{fieldname:"corp_id", fieldtype:"Data", label:t("OA Corporation ID"), default:settings.corp_id},
				{fieldname:"profile", fieldtype:"Select", label:t("Connection Profile"), reqd:1, default:settings.profile,
					options:[{value:"budget", label:t("Budget database connection")}, {value:"shared_oa", label:t("Shared OA database connection")}],
					description:t("Reuse a server connection. Its configuration is checked when settings are saved.")},
				{fieldname:"save_error", fieldtype:"HTML"},
			], primary_action_label:t("Save Settings"),
			async primary_action(values) {
				if (dialog.saving) return;
				dialog.saving = true;
				dialog.get_primary_btn().prop("disabled", true);
				dialog.get_close_btn().prop("disabled", true);
				try {
					await call("save_sync_settings", {...values, expected_version:settings.version});
					dialog.saving = false;
					dialog.hide();
					root.frappe.show_alert({message:t("Budget sync settings saved."), indicator:"green"});
					await onSaved();
				} catch (error) {
					dialog.fields_dict.save_error.$wrapper.text(t("Could not save settings. Your inputs have been retained."));
				} finally {
					dialog.saving = false;
					dialog.get_primary_btn().prop("disabled", false);
					dialog.get_close_btn().prop("disabled", false);
				}
			},
		});
		const hide = dialog.hide.bind(dialog);
		dialog.hide = () => {if (!dialog.saving) hide();};
		dialog.$wrapper.on("hide.bs.modal", event => {if (dialog.saving) event.preventDefault();});
		dialog.get_close_btn().removeAttr("data-dismiss").off("click").on("click", () => dialog.hide());
		dialog.show();
		return dialog;
	}
	async function openSync(onCompleted = () => {}) {
		if (!manager()) return;
		let live = true, busy = false, timer, data, attempts = 0, tracked = false;
		const dialog = new root.frappe.ui.Dialog({title:t("Budget Synchronization"), size:"large",
			fields:[{fieldname:"status", fieldtype:"HTML"}], primary_action_label:t("Synchronize Now"),
			async primary_action() {
				if (busy || !canStart(data)) return;
				busy = true;
				dialog.get_primary_btn().prop("disabled", true);
				try {
					const result = (await root.frappe.call({method:"budget.services.oa_budget_sync.enqueue_oa_budget_sync"})).message;
					if (!live) return;
					if (result.blocked_by) root.frappe.msgprint(t(reasons[result.blocked_by] || states.unknown));
					tracked = !!result.job_id;
					attempts = 0;
				} catch (error) {
					if (live) root.frappe.msgprint(t("Could not queue synchronization. Check the status before retrying."));
				} finally { busy = false; }
				await refresh();
			},
			secondary_action_label:t("Refresh"), secondary_action:() => refresh(),
		});
		async function refresh() {
			clearTimeout(timer);
			if (!live || busy) return;
			busy = true;
			dialog.get_primary_btn().prop("disabled", true);
			try {
				data = await call("get_sync_state");
				if (!live) return;
				dialog.fields_dict.status.$wrapper.html(statusHTML(data));
				if (["queued", "running"].includes(data.state)) {
					tracked = true;
					if (++attempts <= 150) timer = setTimeout(refresh, 4000);
				} else if (tracked) {
					tracked = false;
					await onCompleted();
				}
			} catch (error) {
				data = null;
				if (live) dialog.fields_dict.status.$wrapper.html(`<p class="text-danger">${esc(t("Could not load synchronization status. Refresh to retry."))}</p>`);
			} finally {
				busy = false;
				if (live) dialog.get_primary_btn().prop("disabled", !canStart(data));
			}
		}
		dialog.$wrapper.on("hidden.bs.modal", () => {live = false; clearTimeout(timer);});
		dialog.add_custom_action(t("Open Sync Log"), () => root.frappe.set_route("List", "Budget Ledger Sync Log"));
		dialog.show();
		await refresh();
		return dialog;
	}
	function attach(listview) {
		const notice = root.$('<div class="budget-sync-notice text-muted" role="status" style="margin: 8px 0;"></div>');
		listview.page.main.prepend(notice);
		const refresh = async () => {
			try {
				const data = await call("get_sync_state");
				const key = reasons[data.settings.blocked_by];
				notice.text(key ? t(key) : t("Budget data is synchronized through Synchronize Data."));
			} catch (error) { notice.text(t("Could not load synchronization status. Refresh to retry.")); }
		};
		if (manager()) {
			listview.page.add_menu_item(t("Budget Sync Settings"), () => openSettings(refresh));
			listview.page.add_inner_button(t("Synchronize Data"), () => openSync(async () => {await refresh(); await listview.refresh();}));
		}
		refresh();
	}
	return {attach, openSettings, openSync, statusHTML, canStart};
});
