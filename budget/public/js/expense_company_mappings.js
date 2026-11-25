/* A draft editor over the existing versioned subsidiary mapping payload. */
(function (root, factory) {
  const api = factory(root);
  if (typeof module === "object" && module.exports) module.exports = api;
  root.ExpenseCompanyMappings = api;
})(globalThis, function (root) {
  "use strict";
  const t = (text, args) => root.__(text, args);
  const esc = value => root.frappe.utils.escape_html(String(value ?? ""));
  const KINDS = {organization: "Source organization", entity: "Expense-bearing entity", department: "Expense-bearing department"};
  const copy = row => Object.fromEntries(["match_kind", "corp_id", "source_name", "company", "reason"].map(name => [name, row[name] || ""]).concat([["excluded", Number(row.excluded) === 1 ? 1 : 0]]));
  function key(row) {
    const name = String(row.source_name || "").trim();
    return JSON.stringify([row.match_kind, row.match_kind === "organization" ? row.corp_id || "" : "",
      row.match_kind === "organization" ? name : name.replace(/\s+/g, " ")]);
  }
  class Model {
    constructor(rows, saved) {
      this.rows = rows;
      this.saved = saved;
      this.original = new Map(rows.map(row => [key(row), copy(row)]));
      this.candidates = new Map();
    }
    companies(row) {
      return row.match_kind === "organization" ? this.saved.companies : this.saved.expense_companies;
    }
    items() {
      const current = new Map(this.rows.map(row => [key(row), row]));
      const keys = new Set([...this.candidates.keys(), ...current.keys(), ...this.original.keys()]);
      return Array.from(keys, identity => {
        const row = current.get(identity), before = this.original.get(identity), candidate = this.candidates.get(identity);
        const removed = Boolean(before && !row);
        const dirty = before ? !row || JSON.stringify(copy(row)) !== JSON.stringify(before) : Boolean(row);
        return {key: identity, row: row || candidate || before, candidate, removed, dirty,
          mapped: !removed && Boolean(row && (row.company || Number(row.excluded) === 1))};
      });
    }
    assign(keys, company) {
      const wanted = new Set(keys), items = this.items().filter(item => wanted.has(item.key));
      if (!items.length || items.length !== wanted.size || !company || items.some(item => !this.companies(item.row).includes(company)))
        throw Error(t("Choose a subsidiary available to every selected source."));
      for (const item of items) {
        let row = this.rows.find(row => key(row) === item.key);
        if (!row) { row = copy(item.row); this.rows.push(row); }
        Object.assign(row, {company, excluded: 0, reason: ""});
      }
    }
    remove(identity) {
      const index = this.rows.findIndex(row => key(row) === identity);
      if (index >= 0) this.rows.splice(index, 1);
    }
    undo(identity) {
      this.remove(identity);
      if (this.original.has(identity)) this.rows.push(copy(this.original.get(identity)));
    }
    put(values, identity) {
      const row = copy(values);
      if (row.match_kind !== "organization") row.corp_id = "";
      const next = key(row);
      if (!Object.hasOwn(KINDS, row.match_kind) || !row.source_name.trim() ||
          (row.match_kind === "organization" && !this.saved.corporations.includes(row.corp_id)))
        throw Error(t("Complete the source information first"));
      if (this.rows.some(item => key(item) === next && key(item) !== identity))
        throw Error(t("This source organization is already in the mapping table."));
      if (row.excluded) {
        if (row.match_kind === "organization" || row.company || !row.reason.trim())
          throw Error(t("A reason is required for non-Mexico expenses; leave the subsidiary empty"));
      } else if (!this.companies(row).includes(row.company)) {
        throw Error(t("Choose a subsidiary available to every selected source."));
      }
      if (identity) this.remove(identity);
      this.rows.push(row);
    }
  }
  class Controller {
    constructor({element, grid, saved, call, isCurrent, isSaving}) {
      Object.assign(this, {element, grid, saved, call, isCurrent, isSaving});
      this.model = new Model(grid.df.data, saved);
      this.active = true;
      this.tab = "pending";
      this.query = this.company = this.kind = "";
      this.page = 0;
      this.selected = new Set();
      this.streams = Object.fromEntries(["organizations", "entities"].map(kind => [kind,
        {next: 0, loaded: false, done: false, error: false, rows: new Map()}]));
      this.onClick = event => this.click(event);
      this.onChange = event => this.change(event);
      this.onInput = event => {
        if (event.target.dataset.mappingRole !== "search" || !this.current() || this.isSaving() ||
            event.isComposing || this.composing || this.query === event.target.value) return;
        this.query = event.target.value; this.resetView(); this.render();
      };
      this.onCompositionStart = event => {
        if (event.target.dataset.mappingRole === "search") this.composing = true;
      };
      this.onCompositionEnd = event => {
        if (event.target.dataset.mappingRole !== "search") return;
        this.composing = false; this.onInput(event);
      };
      element.addEventListener("click", this.onClick);
      element.addEventListener("change", this.onChange);
      element.addEventListener("input", this.onInput);
      element.addEventListener("compositionstart", this.onCompositionStart);
      element.addEventListener("compositionend", this.onCompositionEnd);
      // Keep native filter controls connected across renders, including when
      // candidate requests finish while an IME composition is in progress.
      element.innerHTML = `<div data-mapping-part="toolbar"></div>
        <div class="em-mapping-filters">
          <label><span>${esc(t("Search mappings"))}</span><input class="form-control" type="search" data-mapping-role="search" placeholder="${esc(t("Source name or subsidiary"))}"></label>
          <label><span>${esc(t("Source type"))}</span><select class="form-control" data-mapping-role="kind"><option value="">${esc(t("All source types"))}</option>${Object.entries(KINDS).map(([value, label]) => `<option value="${value}">${esc(t(label))}</option>`).join("")}</select></label>
          <label><span>${esc(t("Subsidiary"))}</span><select class="form-control" data-mapping-role="company">${this.options(this.saved.companies, "", "All subsidiaries")}</select></label>
        </div>
        <div data-mapping-part="results"></div>`;
      this.render();
      // Only read candidates when the mapping section becomes visible. Opening
      // synchronization settings or a collapsed section never scans OA.
      if (root.IntersectionObserver) {
        this.observer = new root.IntersectionObserver(entries => {
          if (entries.some(entry => entry.isIntersecting) && this.current()) {
            this.observer.disconnect(); this.load();
          }
        });
        this.observer.observe(element);
      }
    }
    current() { return this.active && this.isCurrent(); }
    resetView() { this.page = 0; this.selected.clear(); }
    changed() {
      this.grid.df.data.forEach((row, index) => { row.idx = index + 1; });
      this.grid.refresh();
      this.selected.clear();
      this.render();
    }
    async load(reset = false) {
      if (!this.current() || this.loading || this.isSaving()) return;
      this.loading = true; this.render();
      const requests = Object.entries(this.streams).filter(([, stream]) => reset || !stream.done || stream.error);
      const results = await Promise.allSettled(requests.map(async ([kind, stream]) => {
        const start = reset ? 0 : stream.next;
        const data = await this.call("get_sync_mapping_candidates", {kind, start}, {silent: true});
        if (!Array.isArray(data?.rows)) throw Error("Invalid candidate response");
        return {kind, start, data};
      }));
      if (!this.current()) return;
      results.forEach((result, index) => {
        const [kind, stream] = requests[index];
        if (result.status === "rejected") { stream.error = true; return; }
        const {data, start} = result.value;
        if (reset) stream.rows.clear();
        for (const raw of data.rows) {
          const row = {...raw, match_kind: kind === "organizations" ? "organization" : raw.match_kind,
            corp_id: kind === "organizations" ? raw.corp_id : ""};
          stream.rows.set(key(row), row);
        }
        Object.assign(stream, {loaded: true, error: false, done: !data.has_more, next: start + 50});
      });
      this.model.candidates = new Map(Object.values(this.streams).flatMap(stream => [...stream.rows]));
      this.loading = false; this.render();
    }
    filtered() {
      const query = this.query.trim().toLocaleLowerCase();
      return this.model.items().filter(item => {
        const matchTab = this.tab === "changes" ? item.dirty : this.tab === "mapped" ? item.mapped : !item.mapped && !item.removed;
        return matchTab && (!this.kind || item.row.match_kind === this.kind) &&
          (!this.company || item.row.company === this.company) &&
          (!query || [item.row.source_name, item.row.company].some(value => String(value || "").toLocaleLowerCase().includes(query)));
      });
    }
    options(values, selected = "", empty = "Choose a subsidiary") {
      return `<option value="">${esc(t(empty))}</option>` + values.map(value =>
        `<option value="${esc(value)}" ${value === selected ? "selected" : ""}>${esc(value)}</option>`).join("");
    }
    render() {
      if (!this.active) return;
      const scroll = this.element.querySelector(".em-mapping-table-wrap");
      const scrollLeft = scroll?.scrollLeft || 0;
      const all = this.model.items(), filtered = this.filtered(), size = 20;
      this.page = Math.min(this.page, Math.max(0, Math.ceil(filtered.length / size) - 1));
      const view = JSON.stringify([this.tab, this.query, this.kind, this.company, this.page]);
      const scrollTop = view === this.renderedView ? scroll?.scrollTop || 0 : 0;
      this.visible = filtered.slice(this.page * size, (this.page + 1) * size);
      const visibleKeys = new Set(this.visible.map(item => item.key));
      this.selected = new Set([...this.selected].filter(identity => visibleKeys.has(identity)));
      const counts = {pending: all.filter(row => !row.mapped && !row.removed).length,
        mapped: all.filter(row => row.mapped).length, changes: all.filter(row => row.dirty).length};
      const loaded = Object.values(this.streams).every(stream => stream.loaded);
      const error = Object.values(this.streams).some(stream => stream.error);
      const more = Object.values(this.streams).some(stream => !stream.done);
      const selectedRows = this.visible.filter(item => this.selected.has(item.key));
      const choices = selectedRows.length ? this.saved.companies.filter(company => selectedRows.every(item => this.model.companies(item.row).includes(company))) : [];
      const disabled = this.isSaving() ? "disabled" : "";
      for (const role of ["search", "kind", "company"]) {
        const input = this.element.querySelector(`[data-mapping-role="${role}"]`);
        input.disabled = Boolean(disabled);
        const value = role === "search" ? this.query : this[role];
        if (!(role === "search" && this.composing) && input.value !== value) input.value = value;
      }
      this.element.querySelector('[data-mapping-part="toolbar"]').innerHTML = `<div class="em-mapping-toolbar">
        <div class="em-mapping-tabs" role="group" aria-label="${esc(t("Subsidiary mappings"))}">${Object.entries({pending: "Unmapped sources", mapped: "Mapped sources", changes: "Unsaved changes"}).map(([tab, label]) =>
          `<button type="button" class="btn btn-sm ${this.tab === tab ? "btn-primary" : "btn-default"}" data-mapping-action="tab" data-tab="${tab}" aria-pressed="${this.tab === tab}" ${disabled}>${esc(t(label))} <span>${tab === "pending" ? !loaded ? "—" : `${counts.pending}${more || error ? "+" : ""}` : counts[tab]}</span></button>`).join("")}</div>
        <div class="em-mapping-actions"><button type="button" class="btn btn-sm btn-default" data-mapping-action="refresh" ${this.loading || disabled ? "disabled" : ""}>${esc(t("Refresh sources"))}</button><button type="button" class="btn btn-sm btn-default" data-mapping-action="add" ${disabled}>${esc(t("Add mapping manually"))}</button></div>
      </div>`;
      this.element.querySelector('[data-mapping-part="results"]').innerHTML = `<div class="em-mapping-batch"><span>${esc(t("{0} selected on this page", [selectedRows.length]))}</span><select class="form-control" data-mapping-role="batch-company" aria-label="${esc(t("Batch subsidiary"))}" ${!selectedRows.length || disabled ? "disabled" : ""}>${this.options(choices)}</select><button type="button" class="btn btn-sm btn-primary" data-mapping-action="assign" ${!selectedRows.length || disabled ? "disabled" : ""}>${esc(t("Apply to selected sources"))}</button></div>
      ${error ? `<div class="alert alert-warning">${esc(t("Some sources could not be loaded. Existing edits are retained."))} <button type="button" class="btn btn-xs btn-default" data-mapping-action="load" ${this.loading || disabled ? "disabled" : ""}>${esc(t("Retry"))}</button></div>` : ""}
      <div class="em-mapping-table-wrap"><table class="em-mapping-table"><thead><tr><th class="em-mapping-check"><input type="checkbox" data-mapping-role="all" aria-label="${esc(t("Select this page"))}" ${selectedRows.length && selectedRows.length === this.visible.length ? "checked" : ""} ${!this.visible.length || disabled ? "disabled" : ""}></th><th>${esc(t("Source name"))}</th><th>${esc(t("Subsidiary"))}</th><th>${esc(t("Status"))}</th><th>${esc(t("Actions"))}</th></tr></thead><tbody>${this.visible.map((item, index) => this.rowHTML(item, index, disabled)).join("") || `<tr><td colspan="5" class="em-mapping-empty">${esc(t(this.tab === "pending" && !loaded ? "Load local sources to assign their subsidiaries." : "No mappings match the current filters."))}${this.tab === "pending" && !loaded ? `<br><button type="button" class="btn btn-sm btn-default" data-mapping-action="load" ${this.loading || disabled ? "disabled" : ""}>${esc(t("Load unmapped sources"))}</button>` : ""}</td></tr>`}</tbody></table></div>
      <div class="em-mapping-footer"><span role="status">${esc(this.loading ? t("Loading mapping sources…") : t("{0} matching sources", [filtered.length]))}</span><button type="button" class="btn btn-xs btn-default" data-mapping-action="previous" ${!this.page || disabled ? "disabled" : ""}>${esc(t("Previous"))}</button><span>${this.page + 1} / ${Math.max(1, Math.ceil(filtered.length / size))}</span><button type="button" class="btn btn-xs btn-default" data-mapping-action="next" ${(this.page + 1) * size >= filtered.length || disabled ? "disabled" : ""}>${esc(t("Next"))}</button>${more && this.tab === "pending" ? `<button type="button" class="btn btn-xs btn-default" data-mapping-action="load" ${this.loading || disabled ? "disabled" : ""}>${esc(t("Load more sources"))}</button>` : ""}</div>
      <p class="em-mapping-help">${esc(t("Search covers saved mappings and loaded sources. Changes take effect after Save Settings."))}</p>`;
      const allCheckbox = this.element.querySelector('[data-mapping-role="all"]');
      allCheckbox.indeterminate = Boolean(selectedRows.length && selectedRows.length < this.visible.length);
      const table = this.element.querySelector(".em-mapping-table-wrap");
      table.scrollTop = scrollTop; table.scrollLeft = scrollLeft;
      this.renderedView = view;
    }
    rowHTML(item, index, disabled) {
      const {row, candidate, removed, dirty} = item;
      const suggestions = (candidate?.suggestions || []).map(value => typeof value === "string" ? value : value.company).filter(company => this.model.companies(row).includes(company));
      const status = removed ? "Removed in draft" : dirty ? "Not saved" : item.mapped ? Number(row.excluded) === 1 ? "Excluded" : "Mapped" : "Unmapped";
      return `<tr><td class="em-mapping-check"><input type="checkbox" data-mapping-role="row" data-index="${index}" aria-label="${esc(t("Select {0}", [row.source_name]))}" ${this.selected.has(item.key) ? "checked" : ""} ${disabled}></td>
        <td class="em-mapping-source"><strong>${esc(row.source_name)}</strong><div class="em-mapping-source-meta"><span>${esc(t(KINDS[row.match_kind]))}</span>${row.match_kind === "organization" && this.saved.corporations.length > 1 ? `<span>${esc(row.corp_id)}</span>` : ""}${candidate ? `<span>${esc(t(row.match_kind === "organization" ? "{0} approvals" : "{0} expense lines", [candidate.count]))}</span>` : ""}</div></td>
        <td class="em-mapping-company"><select class="form-control" data-mapping-role="row-company" data-index="${index}" aria-label="${esc(t("Subsidiary for {0}", [row.source_name]))}" ${disabled}>${this.options(this.model.companies(row), removed ? "" : row.company)}</select>${!item.mapped && suggestions.length ? `<div class="em-mapping-suggestions">${esc(t("Suggested subsidiaries"))}: ${suggestions.map((company, suggestion) => `<button type="button" class="btn btn-xs btn-default" data-mapping-action="suggest" data-index="${index}" data-suggestion="${suggestion}" ${disabled}>${esc(company)}</button>`).join("")}</div>` : ""}</td>
        <td><span class="em-mapping-state ${dirty ? "is-dirty" : item.mapped ? "is-mapped" : ""}">${esc(t(status))}</span></td>
        <td class="em-mapping-row-actions">${dirty ? `<button type="button" class="btn btn-xs btn-default" data-mapping-action="undo" data-index="${index}" ${disabled}>${esc(t("Undo"))}</button>` : ""}<button type="button" class="btn btn-xs btn-default" data-mapping-action="edit" data-index="${index}" ${disabled}>${esc(t("Details"))}</button>${item.mapped ? `<button type="button" class="btn btn-xs btn-default" data-mapping-action="remove" data-index="${index}" ${disabled}>${esc(t("Remove mapping"))}</button>` : ""}</td></tr>`;
    }
    click(event) {
      const button = event.target.closest("[data-mapping-action]");
      if (!button || !this.element.contains(button) || button.disabled || !this.current() || this.isSaving()) return;
      const action = button.dataset.mappingAction, item = this.visible[Number(button.dataset.index)];
      try {
        if (action === "tab") { this.tab = button.dataset.tab; this.resetView(); this.render(); if (this.tab === "pending" && !Object.values(this.streams).every(s => s.loaded)) this.load(); }
        else if (action === "load" || action === "refresh") this.load(action === "refresh");
        else if (action === "previous" || action === "next") { this.page += action === "next" ? 1 : -1; this.selected.clear(); this.render(); }
        else if (action === "assign") { this.model.assign(this.selected, this.element.querySelector('[data-mapping-role="batch-company"]').value); this.changed(); }
        else if (action === "undo" && item) { this.model.undo(item.key); this.changed(); }
        else if (action === "remove" && item) { this.model.remove(item.key); this.changed(); }
        else if (action === "suggest" && item) {
          const companies = (item.candidate?.suggestions || []).map(value => typeof value === "string" ? value : value.company).filter(company => this.model.companies(item.row).includes(company));
          this.model.assign([item.key], companies[Number(button.dataset.suggestion)]); this.changed();
        } else if (action === "add" || action === "edit") this.edit(action === "edit" ? item : null);
      } catch (error) { root.frappe.msgprint(error.message); }
    }
    change(event) {
      if (!this.current() || this.isSaving()) return;
      const input = event.target, role = input.dataset.mappingRole, item = this.visible[Number(input.dataset.index)];
      try {
        if (role === "kind" || role === "company") { this[role] = input.value; this.resetView(); this.render(); }
        else if (role === "all") { this.selected = new Set(input.checked ? this.visible.map(item => item.key) : []); this.render(); }
        else if (role === "row" && item) { if (input.checked) this.selected.add(item.key); else this.selected.delete(item.key); this.render(); }
        else if (role === "row-company" && item) {
          if (input.value) this.model.assign([item.key], input.value); else this.model.remove(item.key);
          this.changed();
        }
      } catch (error) { root.frappe.msgprint(error.message); this.render(); }
    }
    edit(item) {
      const row = item ? copy(item.row) : {match_kind: "organization", corp_id: this.saved.corporations.length === 1 ? this.saved.corporations[0] : ""};
      const helpers = root.CNApprovalSettings;
      let editor;
      const fields = [
        {fieldname: "match_kind", label: t("Source type"), fieldtype: "Select", reqd: 1, read_only: Boolean(item), default: row.match_kind,
          options: Object.entries(KINDS).map(([value, label]) => ({value, label: esc(t(label))})), formatter: value => esc(t(KINDS[value] || value))},
        {fieldname: "corp_id", label: t("Corporation ID"), fieldtype: "Select", reqd: 0, read_only: Boolean(item), default: row.corp_id, options: helpers.textOptions(this.saved.corporations), depends_on: "eval:doc.match_kind === 'organization'"},
        {fieldname: "source_name", label: t("Source name"), fieldtype: "Small Text", reqd: 1, read_only: Boolean(item), default: row.source_name},
        {fieldname: "company", label: t("Subsidiary"), fieldtype: "Link", options: "Company", default: item?.removed ? "" : row.company,
          // Native Link may validate its default while Dialog is constructing.
          get_query: () => ({filters: {name: ["in", this.model.companies({match_kind: editor?.get_value("match_kind") || row.match_kind})]}})},
        {fieldname: "advanced", label: t("Advanced settings"), fieldtype: "Section Break", collapsible: 1},
        {fieldname: "excluded", label: t("Keep outside legacy expense scope"), fieldtype: "Check", default: row.excluded, depends_on: "eval:doc.match_kind !== 'organization'"},
        {fieldname: "reason", label: t("Reason"), fieldtype: "Small Text", default: row.reason, depends_on: "eval:doc.excluded"},
      ];
      editor = new root.frappe.ui.Dialog({title: t(item ? "Edit subsidiary mapping" : "Add mapping manually"), fields,
        primary_action_label: t("Apply to draft"), primary_action: () => {
          if (!this.current() || this.isSaving()) return;
          const values = editor.get_values(); if (!values) return;
          try { this.model.put(values, item?.key); } catch (error) { root.frappe.msgprint(error.message); return; }
          this.tab = "changes"; this.resetView(); editor.hide(); this.changed();
        }});
      this.editor = editor; editor.show();
    }
    destroy() {
      this.active = false; this.observer?.disconnect(); this.editor?.hide();
      this.element.removeEventListener("click", this.onClick);
      this.element.removeEventListener("change", this.onChange);
      this.element.removeEventListener("input", this.onInput);
      this.element.removeEventListener("compositionstart", this.onCompositionStart);
      this.element.removeEventListener("compositionend", this.onCompositionEnd);
    }
  }
  return {Model, Controller, key};
});
