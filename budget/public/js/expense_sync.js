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
  function label(source) {
    if (!source.configured) return "Synchronization configuration required";
    if (source.active) return "Synchronizing approvals…";
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
    async synchronize(source) {
      if (!this.current() || this.submitting) return;
      if (!this.state) await this.poll();
      if (!this.current() || !this.state) return;
      const selected = Object.entries(this.state.sources).filter(
        ([key, state]) =>
          (!source || key === source) && state.can_sync && !state.active
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
        selected.map(([key]) => this.work.call("enqueue_sync", { source: key }))
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
        Object.keys(this.errors).length ||
        sources.some((s) => s.status === "failed" || !s.configured);
      const text = this.error
        ? t(this.error)
        : problem
        ? t("Synchronization needs attention")
        : busy
        ? t("Synchronizing approvals…")
        : this.state?.scheduler_enabled === false
        ? t("Site scheduler is disabled")
        : sources.length && sources.every((s) => !s.enabled)
        ? t("Automatic synchronization is disabled")
        : t("Automatic synchronization every {0} minutes", [5]);
      const node = this.work.host.querySelector(".em-sync-status");
      if (node) {
        node.innerHTML = `<button type="button" class="em-link ${
          problem || this.state?.scheduler_enabled === false ? "em-warning" : ""
        }">${esc(text)}</button>`;
        node.querySelector("button").onclick = () => this.open();
      }
      this.work.syncButton?.prop(
        "disabled",
        this.submitting || !sources.some((s) => s.can_sync && !s.active)
      );
      if (this.dialog?.is_visible) this.renderDialog();
    }
    open() {
      if (!this.current()) return;
      if (!this.dialog) {
        this.dialog = new root.frappe.ui.Dialog({
          title: t("Synchronization Settings"),
          size: "large",
          fields: [{ fieldname: "content", fieldtype: "HTML" }],
        });
        this.dialog.fields_dict.content.$wrapper.on(
          "click",
          "[data-sync-action]",
          (event) => {
            const { syncAction, source } = event.currentTarget.dataset;
            if (syncAction === "configure") this.configure(source);
            else if (syncAction === "sync") this.synchronize(source);
            else this.poll();
          }
        );
      }
      this.renderDialog();
      this.dialog.show();
      this.poll();
    }
    renderDialog() {
      const state = this.state;
      let html = `<p class="text-muted">${esc(
        t(
          "Enabled sources are checked every 5 minutes. Running jobs are reused; failed synchronization keeps existing data."
        )
      )}</p>`;
      if (state?.scheduler_enabled === false)
        html += `<div class="alert alert-warning">${esc(
          t(
            "Site scheduler is disabled. Automatic synchronization will not run until an administrator enables it."
          )
        )}</div>`;
      if (this.error)
        html += `<div class="alert alert-warning">${esc(
          t(this.error)
        )} <button class="btn btn-xs btn-default" data-sync-action="refresh">${esc(
          t("Retry")
        )}</button></div>`;
      for (const [key, source] of Object.entries(state?.sources || {})) {
        const updated = source.last_update_at
          ? root.frappe.datetime.str_to_user(source.last_update_at)
          : "—";
        html += `<section class="em-sync-source"><div><strong>${esc(
          t(SOURCES[key])
        )}</strong><p>${esc(t(label(source)))}</p>
          <small>${esc(t("Data updated at"))}: ${esc(updated)}</small>
          ${
            source.template_count != null
              ? `<small>${esc(
                  t("{0} of {1} templates completed", [
                    source.completed_templates || 0,
                    source.template_count,
                  ])
                )}</small>`
              : ""
          }
          ${
            this.errors[key]
              ? `<p class="text-danger">${esc(t(this.errors[key]))}</p>`
              : ""
          }</div><div class="em-sync-actions">
          ${
            source.can_manage
              ? `<button class="btn btn-default btn-sm" data-sync-action="configure" data-source="${key}">${esc(
                  t("Configure source")
                )}</button>`
              : `<small>${esc(
                  t("Settings are restricted to authorized administrators.")
                )}</small>`
          }
          <button class="btn btn-primary btn-sm" data-sync-action="sync" data-source="${key}" ${
          !source.can_sync || source.active || this.submitting ? "disabled" : ""
        }>${esc(t("Synchronize Now"))}</button></div></section>`;
      }
      if (!state) html += `<p>${esc(t("Loading synchronization status…"))}</p>`;
      this.dialog.fields_dict.content.$wrapper.html(html);
    }
    async configure(source) {
      if (!this.current() || !this.state?.sources[source]?.can_manage) return;
      const seq = (this.configureSeq = (this.configureSeq || 0) + 1);
      const current = () => this.current() && seq === this.configureSeq;
      this.dialog?.hide();
      const paths =
        source === "cn"
          ? [
              "/assets/budget/css/cn_approval_workbench.css",
              "/assets/budget/js/cn_approval_settings.js",
            ]
          : [
              "/assets/deeplinkerp_branding/js/mexico_expense_mappings.js",
              "/assets/deeplinkerp_branding/js/mexico_expense_settings.js",
            ];
      try {
        await new Promise((resolve) => root.frappe.require(paths, resolve));
        if (!current()) return;
        const saved = async () => {
          if (current()) {
            await this.poll();
            await this.work.load();
          }
        };
        const dialog = await (source === "cn"
          ? root.CNApprovalSettings
          : root.DeepLinkERPMexicoExpenseSettings
        ).open(saved, { isCurrent: current });
        if (!current()) {
          dialog?.hide();
          return;
        }
        this.settingsDialog = dialog;
      } catch (_) {
        if (current()) {
          this.errors[source] = "Synchronization settings could not be opened. Retry.";
          this.open();
        }
      }
    }
  }
  return { Controller, label, revision };
});
