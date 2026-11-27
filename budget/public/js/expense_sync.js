(function (root, factory) {
  const api = factory(root);
  if (typeof module === "object" && module.exports) module.exports = api;
  root.ExpenseSync = api;
})(globalThis, function (root) {
  "use strict";
  const t = (value, args) => root.__(value, args);
  const esc = (value) => root.frappe.utils.escape_html(String(value ?? ""));
  const SOURCES = {
    cn: "Budget and approval source",
    mx: "Expense approval source",
  };
  function activity(sources, submitting = false) {
    const syncing = submitting || sources.some((s) => s.active);
    const rebuilding = sources.some((s) => s.allocation_rebuilding);
    if (syncing && rebuilding) return "Synchronizing OA approvals and recalculating local mappings…";
    if (syncing) return "Synchronizing OA approvals…";
    if (rebuilding) return "Recalculating local mappings…";
    return "";
  }
  function label(source) {
    if (!source.configured) return "Synchronization configuration required";
    const progress = activity([source]);
    if (progress) return progress;
    if (source.status === "failed")
      return "Synchronization failed. Retry synchronization.";
    if (!source.enabled) return "Automatic synchronization is disabled";
    return source.status === "success"
      ? "Synchronization completed"
      : "Not Synchronized";
  }
  function revision(state) {
    return dataRevision(
      Object.fromEntries(
        Object.entries(state.sources || {}).map(([key, source]) => [
          key,
          source.revision,
        ])
      )
    );
  }
  function dataRevision(versions) {
    if (!versions) return null;
    return JSON.stringify(
      Object.keys(SOURCES).map((key) => [key, versions[key] ?? null])
    );
  }
  class Controller {
    constructor(work) {
      this.work = work;
      this.seq = 0;
      this.active = false;
      this.submitting = false;
      this.errors = {};
    }
    current() {
      return this.active && this.work.current();
    }
    start() {
      this.active = true;
      this.render();
      this.poll();
    }
    stop() {
      this.active = false;
      this.submitting = false;
      this.seq++;
      this.configureSeq = (this.configureSeq || 0) + 1;
      root.clearTimeout(this.timer);
      this.dialog?.hide();
      this.settingsDialog?.hide();
    }
    schedule() {
      root.clearTimeout(this.timer);
      if (!this.current()) return;
      const busy = Object.values(this.state?.sources || {}).some(
        (s) => s.active || s.allocation_rebuilding
      );
      this.timer = root.setTimeout(() => this.poll(), busy ? 2500 : 30000);
      this.timer?.unref?.();
    }
    async poll() {
      if (!this.current() || this.submitting) return;
      const seq = ++this.seq;
      root.clearTimeout(this.timer);
      try {
        const state = await this.work.call("get_sync_status");
        if (!this.current() || seq !== this.seq) return;
        const wasBusy = Object.values(this.state?.sources || {}).some(
          (s) => s.active || s.allocation_rebuilding
        );
        this.state = state;
        this.error = "";
        const busy = Object.values(state.sources).some(
          (s) => s.active || s.allocation_rebuilding
        );
        // Rebuilding can finish without changing any facts. Keep this edge
        // pending until a report succeeds, even if another load is in flight.
        if (wasBusy && !busy) this.refreshAfterSync = true;
        const changed =
          revision(state) !== dataRevision(this.work.data?.metadata?.sync_revision);
        if (!busy && !this.work.loading && (changed || this.refreshAfterSync)) {
          // Only the successful get_data response identifies displayed facts;
          // a status response or a failed/superseded load cannot acknowledge it.
          const loaded = await this.work.load(true);
          if (this.current() && seq === this.seq && loaded)
            this.refreshAfterSync = false;
        }
      } catch (_) {
        if (!this.current() || seq !== this.seq) return;
        this.error =
          "Synchronization status could not be loaded. Retrying shortly.";
      } finally {
        if (this.current() && seq === this.seq) {
          this.render();
          this.schedule();
        }
      }
    }
    async synchronize(source, retryFailed = false) {
      if (!this.current() || this.submitting) return;
      if (!this.state) await this.poll();
      if (!this.current() || !this.state) return;
      const selected = Object.entries(this.state.sources).filter(
        ([key, state]) =>
          (!source || key === source) && state.can_sync && !state.active &&
          (!retryFailed || state.status === "failed" || this.errors[key])
      );
      if (!selected.length) {
        this.open();
        return;
      }
      const seq = ++this.seq;
      root.clearTimeout(this.timer);
      this.submitting = true;
      this.errors = {};
      this.render();
      const results = await Promise.allSettled(
        selected.map(([key]) => this.work.call("enqueue_sync", { source: key, retry_failed: retryFailed ? 1 : 0 }))
      );
      if (!this.current() || seq !== this.seq) return;
      this.submitting = false;
      results.forEach((result, index) => {
        const key = selected[index][0];
        if (result.status === "rejected")
          this.errors[key] = "Synchronization could not be queued. Retry.";
        else this.refreshAfterSync = true;
      });
      this.render();
      await this.poll();
    }
    render() {
      if (!this.current()) return;
      const sources = Object.values(this.state?.sources || {});
      const busy =
        this.submitting ||
        sources.some((s) => s.active || s.allocation_rebuilding);
      const problem =
        this.error ||
        this.settingsError ||
        Object.keys(this.errors).length ||
        sources.some((s) => s.status === "failed" || !s.configured);
      const text = this.error
        ? t(this.error)
        : busy
        ? t(activity(sources, this.submitting))
        : problem
        ? t(this.state?.overall?.status === "partial_failure" ? "Synchronization partially failed" : "Synchronization needs attention")
        : this.state?.scheduler_enabled === false
        ? t("Site scheduler is disabled")
        : sources.length && sources.every((s) => !s.enabled)
        ? t("Automatic synchronization is disabled")
        : "";
      const node = this.work.host.querySelector(".em-sync-status");
      if (node) {
        node.innerHTML = text ? `<button type="button" class="em-link ${
          problem || this.state?.scheduler_enabled === false ? "em-warning" : ""
        }">${esc(text)}</button>` : "";
        if (text) node.querySelector("button").onclick = () => this.open();
      }
      this.work.syncButton?.prop(
        "disabled",
        busy || !sources.some((s) => s.can_sync && !s.active)
      );
      if (this.dialog?.is_visible) this.renderDialog();
    }
    open() {
      if (!this.current()) return;
      if (!this.dialog) {
        this.dialog = new root.frappe.ui.Dialog({
          title: t("OA Synchronization Settings"),
          size: "large",
          // A failed configuration load may reopen this immediately after hide.
          animate: false,
          fields: [{ fieldname: "content", fieldtype: "HTML" }],
        });
        this.dialog.fields_dict.content.$wrapper.on(
          "click",
          "[data-sync-action]",
          (event) => {
            const { syncAction } = event.currentTarget.dataset;
            if (syncAction === "configure") this.configure();
            else if (syncAction === "sync") this.synchronize();
            else if (syncAction === "retry-failed") this.synchronize(undefined, true);
            else this.poll();
          }
        );
      }
      this.renderDialog();
      this.dialog.show();
      this.poll();
    }
    renderDialog() {
      const state = this.state, sources = Object.values(state?.sources || {});
      const overall = state?.overall || {};
      const busy = this.submitting || sources.some(s => s.active || s.allocation_rebuilding);
      const failed = Object.keys(this.errors).length || sources.some(s => s.status === "failed");
      const status = busy ? "running" : failed && overall.status !== "partial_failure" ? "failed" : overall.status;
      const labels = {
        running: "Synchronizing approvals…", queued: "Waiting to synchronize",
        staged: "Read completed; awaiting publication", pending: "Not Synchronized",
        success: "Synchronization completed", failed: "Last synchronization failed",
        partial_failure: "Synchronization partially failed", disabled: "Disabled",
        unconfigured: "No approval templates are enabled",
      };
      const wrapper = this.dialog.fields_dict.content.$wrapper;
      const expanded = wrapper.find?.(".em-sync-details")?.prop("open");
      if (expanded !== undefined) this.detailsOpen = expanded;
      let html = `<p class="text-muted">${esc(t("The first synchronization reads all approvals. Changes synchronize every 30 minutes; a full reconciliation runs every 24 hours. Failed synchronization keeps existing data."))}</p>`;
      if (state?.scheduler_enabled === false)
        html += `<div class="alert alert-warning">${esc(t("Site scheduler is disabled. Automatic synchronization will not run until an administrator enables it."))}</div>`;
      if (this.error || this.settingsError)
        html += `<div class="alert alert-warning">${esc(t(this.settingsError || this.error))} <button class="btn btn-xs btn-default" data-sync-action="${this.settingsError ? "configure" : "refresh"}">${esc(t("Retry"))}</button></div>`;
      if (!state) {
        wrapper.html(html + `<p>${esc(t("Loading synchronization status…"))}</p>`);
        return;
      }
      html += `<section class="em-sync-source"><div><strong>${esc(t("OA Approval Synchronization"))}</strong>
        <p class="${failed ? "text-warning" : ""}">${esc(t(activity(sources, this.submitting) || labels[status] || "Not Synchronized"))}</p>
        <small>${esc(t("{0} of {1} templates updated successfully", [overall.completed || 0, overall.total || 0]))}</small>
        <small>${esc(t("Last complete update"))}: ${esc(overall.last_update_at ? root.frappe.datetime.str_to_user(overall.last_update_at) : "—")}</small>
        ${Object.keys(this.errors).length ? `<p class="text-danger">${esc(t("Some synchronization tasks could not be queued. Retry failed items."))}</p>` : ""}
        </div><div class="em-sync-actions">
        ${state.can_manage ? `<button class="btn btn-default btn-sm" data-sync-action="configure">${esc(t("Configure synchronization"))}</button>` : `<small>${esc(t("Settings are restricted to authorized administrators."))}</small>`}
        <button class="btn btn-primary btn-sm" data-sync-action="sync" ${busy || !sources.some(s => s.can_sync) ? "disabled" : ""}>${esc(t("Synchronize Now"))}</button>
        </div></section>`;
      html += `<details class="em-sync-details" ${this.detailsOpen ? "open" : ""}><summary>${esc(t("Template progress and errors"))}</summary>
        <p class="text-muted">${esc(t("Shared approval templates appear once. Some templates publish together and are retried as a batch."))}</p>
        <div class="em-sync-table"><table class="table"><thead><tr><th>${esc(t("Template Name"))}</th><th>${esc(t("Approval Form"))}</th><th>${esc(t("Status"))}</th></tr></thead><tbody>`;
      const kinds = {budget: "Budget declaration", operating: "Operating expenditure", expense: "Expense expenditure", purchase: "Purchase expenditure"};
      for (const row of state.templates || [])
        html += `<tr><td>${esc(row.name)}</td><td>${esc(t(kinds[row.approval_type] || row.approval_type || ""))}</td><td>${esc(t(labels[row.status] || "Not Synchronized"))}</td></tr>`;
      html += `</tbody></table></div></details>`;
      if (failed)
        html += `<div class="em-sync-retry"><button class="btn btn-default btn-sm" data-sync-action="retry-failed" ${busy || !Object.entries(state.sources).some(([key,s]) => s.can_sync && (s.status === "failed" || this.errors[key])) ? "disabled" : ""}>${esc(t("Retry failed items"))}</button></div>`;
      wrapper.html(html);
    }
    async configure() {
      if (!this.current() || !this.state?.can_manage) return;
      this.settingsError = "";
      const seq = (this.configureSeq = (this.configureSeq || 0) + 1);
      const current = () => this.current() && seq === this.configureSeq;
      this.dialog?.hide();
      const paths = [
        "/assets/budget/css/cn_approval_workbench.css",
        "/assets/budget/js/cn_approval_settings.js",
        "/assets/budget/js/expense_company_mappings.js",
        "/assets/budget/js/expense_template_mapping.js",
        "/assets/budget/js/expense_sync_settings.js",
      ];
      try {
        await new Promise((resolve) => root.frappe.require(paths, resolve));
        if (!current()) return;
        const saved = async () => {
          if (current()) {
            // A mapping save starts local reprojection. Let polling wait for
            // completion and retain the current report filters when reloading.
            this.refreshAfterSync = true;
            await this.poll();
          }
        };
        const dialog = await root.ExpenseSyncSettings.open(saved, { isCurrent: current });
        if (!current()) {
          dialog?.hide();
          return;
        }
        this.settingsDialog = dialog;
      } catch (_) {
        if (current()) {
          this.settingsError = "Synchronization settings could not be opened. Retry.";
          this.open();
        }
      }
    }
  }
  return { Controller, label, revision };
});
