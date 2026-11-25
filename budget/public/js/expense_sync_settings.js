/* One approval policy; legacy adapter bindings remain an internal detail. */
(function (root, factory) {
  const api = factory(root);
  if (typeof module === "object" && module.exports) module.exports = api;
  root.ExpenseSyncSettings = api;
})(globalThis, function (root) {
  "use strict";
  const t = (value, args) => root.__(value, args);
  const esc = value => root.frappe.utils.escape_html(String(value ?? ""));
  const call = async (method, args = {}, options = {}) => (await root.frappe.call({method: "budget.api.expenses." + method, args, ...options})).message;
  const MODES = {enabled: "Enabled", disabled: "Disabled", mixed: "Partially enabled (keep current settings)"};
  const AUTOMATIC_RULES = {budget: "application", operating: "both", purchase: "both", expense: "expense"};
  const KINDS = {budget: "Budget declaration", operating: "Operating expenditure", expense: "Expense expenditure", purchase: "Purchase expenditure"};
  const MATCHES = {organization: "Source organization", entity: "Expense-bearing entity", department: "Expense-bearing department"};
  const choices = labels => Object.entries(labels).map(([value, label]) => ({value, label: esc(t(label))}));
  const select = (fieldname, label, labels, extra = {}) => ({fieldname, label: t(label), fieldtype: "Select", options: choices(labels), formatter: value => esc(t(labels[value] || value)), ...extra});
  const code = (fieldname, label, extra = {}) => ({fieldname, label: t(label), fieldtype: "Code", options: "JSON", ...extra});
  const hasApplication = doc => ["application", "both"].includes(doc.processing_rule);
  const hasExpense = doc => ["expense", "both"].includes(doc.processing_rule);

  function inheritDefaults(row, defaults) {
    if (row.key) return row;
    return {...row, corp_id: defaults.corp_id,
      processing_rule: AUTOMATIC_RULES[row.approval_type] || ""};
  }

  function normalizeTemplate(row) {
    return !row.key && typeof row.process_code === "string" ? {...row, process_code: row.process_code.trim()} : row;
  }

  function validateTemplates(rows, corporations, firstRow = 1) {
    const seen = new Set();
    for (const [index, raw] of rows.entries()) {
      const row = normalizeTemplate(raw), rowNumber = index + firstRow;
      for (const [field, label] of [["corp_id", "Corporation ID"],
        ["process_code", "Process Code"], ["template_name", "Template Name"], ["approval_type", "Approval Form"]]) {
        if (typeof row[field] !== "string" || !row[field].trim())
          return t("Approval template row {0}: {1} is required.", [rowNumber, t(label)]);
      }
      if (!corporations.includes(row.corp_id))
        return t("Approval template row {0}: the source corporation is not authorized.", [rowNumber]);
      const key = JSON.stringify([row.corp_id, row.process_code]);
      if (seen.has(key))
        return t("Approval template row {0}: this source corporation and process code are already listed.", [rowNumber]);
      seen.add(key);
      if (!Object.hasOwn(KINDS, row.approval_type)) return t("Invalid approval template");
    }
  }

  function removeDraftTemplates(grid, persistedRows) {
    const selected = grid.get_selected_children();
    if (!selected.length) return root.frappe.msgprint(t("Select unsaved template rows to remove."));
    if (selected.some(row => persistedRows.has(row)))
      return root.frappe.msgprint(t("Disable historical templates instead of removing them"));
    root.CNApprovalSettings.flushGridEditors(grid);
    for (const row of grid.grid_rows || []) {
      if (row && selected.includes(row.doc) && row.wrapper.hasClass("grid-row-open")) row.toggle_view(false);
    }
    // Retain the original data array and every unrelated draft/editor.
    const data = grid.df.data;
    for (let index = data.length - 1; index >= 0; index--) {
      if (selected.includes(data[index])) data.splice(index, 1);
    }
    data.forEach((row, index) => {row.idx = index + 1;});
    grid.refresh();
    return true;
  }

  function appendCandidate(grid, kind, candidate) {
    const helpers = root.CNApprovalSettings;
    let row;
    if (kind === "categories") {
      row = {kind: candidate.kind, source_path: JSON.stringify(candidate.source_path),
        source_label: helpers.pathLabel(candidate.kind, candidate.source_path), category: ""};
      if (grid.get_data().some(item => item.kind === row.kind && item.source_path === row.source_path))
        return root.frappe.msgprint(t("This category path is already in the mapping table."));
    } else {
      row = {match_kind: kind === "organizations" ? "organization" : candidate.match_kind,
        corp_id: candidate.corp_id || "", source_name: candidate.source_name, company: "", excluded: 0, reason: ""};
      if (!row.source_name?.trim()) return root.frappe.msgprint(t("Complete the source information first"));
      const key = item => JSON.stringify([item.match_kind, item.corp_id || "",
        item.match_kind === "organization" ? item.source_name?.trim() : item.source_name?.trim().replace(/\s+/g, " ")]);
      if (grid.get_data().some(item => key(item) === key(row)))
        return root.frappe.msgprint(t("This source organization is already in the mapping table."));
    }
    helpers.flushGridEditors(grid);
    grid.df.data.push(row);
    grid.refresh();
    grid.grid_pagination.go_to_page(Math.ceil(grid.df.data.length / grid.grid_pagination.page_length));
    const added = grid.grid_rows.find(item => item?.doc === row);
    added?.toggle_view(true);
    added?.wrapper[0]?.scrollIntoView({block: "center"});
    return true;
  }

  function payload(values, saved) {
    const defaults = {corp_id: values.common_corp_id || ""};
    return {
      sync_mode: values.sync_mode, business_timezone: values.business_timezone, processing_mode: "approval",
      versions: saved.versions, templates: values.templates.map(row => normalizeTemplate(inheritDefaults(row, defaults))),
      template_defaults: defaults, template_defaults_version: saved.template_defaults_version,
      // Native Table omits an empty value; clearing all mappings is still an
      // explicit empty replacement in the versioned save command.
      company_mappings: values.company_mappings || [],
      management_rules: {warning_percent: values.warning_percent,
        category_mappings: (values.category_mappings || []).map(row => ({kind: row.kind,
          source_path: typeof row.source_path === "string" ? JSON.parse(row.source_path) : row.source_path,
          category: row.category}))},
    };
  }

  async function open(onSaved, options = {}) {
    const saved = await call("get_sync_settings");
    if (options.isCurrent && !options.isCurrent()) return;
    const helpers = root.CNApprovalSettings;
    const persistedRows = new WeakSet(saved.templates), candidateSequences = {};
    const defaults = saved.template_defaults || {};
    let dialog, mappingEditor, saving = false, previewSequence = 0;
    const commonValues = () => ({corp_id: dialog.get_value("common_corp_id") || ""});
    function invalidatePreview() {
      previewSequence++;
      dialog?.fields_dict.preview_result?.$wrapper.empty();
    }
    function previewContext() {
      const grid = dialog.fields_dict.templates.grid, selected = grid.get_selected_children();
      if (selected.length !== 1) return null;
      const row = selected[0], index = grid.get_data().indexOf(row);
      if (index < 0) return null;
      return {row, args: {template: {...normalizeTemplate(inheritDefaults(row, commonValues())), idx: index + 1},
        business_timezone: dialog.get_value("business_timezone"), template_defaults: commonValues(), processing_mode: "approval"}};
    }
    function updateDraftDefaults() {
      invalidatePreview();
      const grid = dialog?.fields_dict.templates?.grid;
      if (!grid) return;
      helpers.flushGridEditors(grid);
      for (const row of grid.get_data()) Object.assign(row, inheritDefaults(row, commonValues()));
      grid.refresh();
    }
    async function loadCandidates(kind, start = 0) {
      const seq = candidateSequences[kind] = (candidateSequences[kind] || 0) + 1;
      const wrapper = dialog.fields_dict[kind + "_candidates"].$wrapper;
      const current = () => dialog.is_visible && seq === candidateSequences[kind] && (!options.isCurrent || options.isCurrent());
      wrapper.text(t("Loading approvals…"));
      try {
        const result = await call("get_sync_mapping_candidates", {kind, start}, {silent: true});
        if (!current()) return;
        wrapper.html(`<div class="cnaw-mapping-sources">${result.rows.map((row, index) => {
          const title = kind === "categories" ? helpers.pathLabel(row.kind, row.source_path) : row.source_name;
          const scope = kind === "categories" ? t(KINDS[row.kind]) : kind === "organizations" ? row.corp_id : t(MATCHES[row.match_kind]);
          return `<div class="cnaw-mapping-source"><div><strong>${esc(title)}</strong><small>${esc(scope)} · ${esc(t(kind === "entities" ? "{0} expense lines" : "{0} approvals", [row.count]))}</small>${row.suggestions?.length ? `<small>${esc(t("Suggested subsidiaries"))}: ${row.suggestions.map(esc).join(" / ")}</small>` : ""}</div><button class="btn btn-xs btn-default" type="button" data-candidate-add="${index}">${esc(t("Add mapping"))}</button></div>`;
        }).join("") || `<p class="text-muted">${esc(t("No unmapped items in the current local data."))}</p>`}</div>
          <div class="cnaw-mapping-pages"><button class="btn btn-xs btn-default" type="button" data-candidate-page="${Math.max(0, start - 50)}" ${start === 0 ? "disabled" : ""}>${esc(t("Previous"))}</button><button class="btn btn-xs btn-default" type="button" data-candidate-page="${start + 50}" ${result.has_more ? "" : "disabled"}>${esc(t("Next"))}</button></div>`);
        wrapper.find("[data-candidate-add]").on("click", function () {
          if (saving || !current()) return;
          appendCandidate(dialog.fields_dict[kind === "categories" ? "category_mappings" : "company_mappings"].grid,
            kind, result.rows[Number(this.dataset.candidateAdd)]);
        });
        wrapper.find("[data-candidate-page]").on("click", function () {loadCandidates(kind, Number(this.dataset.candidatePage));});
      } catch (_) {
        if (!current()) return;
        wrapper.html(`<div class="alert alert-warning">${esc(t("Mapping candidates could not be loaded. Retry."))} <button class="btn btn-xs btn-default" type="button">${esc(t("Retry"))}</button></div>`);
        wrapper.find("button").on("click", () => loadCandidates(kind, start));
      }
    }
    const fields = [
      select("sync_mode", "Automatic synchronization", MODES, {default: saved.sync_mode, reqd: 1}),
      {fieldname: "interval", fieldtype: "HTML", options: `<p class="text-muted">${esc(t("The first synchronization reads all approvals. Changes synchronize every 5 minutes; a full reconciliation runs every 24 hours. Failed synchronization keeps existing data."))}</p>`},
      {fieldname: "common_section", fieldtype: "Section Break", label: t("Common template settings")},
      {fieldname: "common_corp_id", label: t("Corporation ID"), fieldtype: "Select",
        options: helpers.textOptions(saved.corporations), default: defaults.corp_id || (saved.corporations.length === 1 ? saved.corporations[0] : ""),
        read_only: saved.corporations.length === 1 && (!defaults.corp_id || defaults.corp_id === saved.corporations[0]),
        onchange: updateDraftDefaults},
      {fieldname: "common_help", fieldtype: "HTML", options: `<p class="text-muted">${esc(t("Templates are processed automatically by approval form. Verified expenses are included by application month after final approval."))}</p>`},
      {fieldname: "templates_section", fieldtype: "Section Break", label: t("Approval Templates")},
      {fieldname: "templates", label: t("Approval Templates"), fieldtype: "Table", cannot_delete_rows: true,
        on_add_row: () => {
          invalidatePreview();
          const grid = dialog.fields_dict.templates.grid;
          const row = grid.df.data.at(-1);
          Object.assign(row, inheritDefaults(row, commonValues()));
        },
        data: saved.templates, fields: helpers.textFields([
          {fieldname: "key", fieldtype: "Data", hidden: 1, read_only: 1},
          select("sync_mode", "Status", MODES, {default: "disabled", reqd: 1, in_list_view: 1, columns: 2}),
          select("approval_type", "Approval Form", KINDS, {reqd: 1, in_list_view: 1, columns: 2, read_only_depends_on: "eval:!!doc.key",
            onchange: updateDraftDefaults}),
          {fieldname: "template_name", label: t("Template Name"), fieldtype: "Data", reqd: 1, in_list_view: 1, columns: 3},
          {fieldname: "process_code", label: t("Process Code"), fieldtype: "Data", reqd: 1, in_list_view: 1, columns: 3, read_only_depends_on: "eval:!!doc.key"},
          {fieldname: "corp_id", fieldtype: "Data", hidden: 1, read_only: 1},
          {fieldname: "processing_rule", fieldtype: "Data", hidden: 1, read_only: 1},
          {fieldname: "template_advanced", label: t("Advanced settings"), fieldtype: "Section Break", collapsible: 1},
          code("application_fields", "Application field mapping", {depends_on: hasApplication}),
          code("application_classification", "Application classification fields", {depends_on: hasApplication}),
          code("expense_fields", "Expense detail field mapping", {depends_on: hasExpense}),
          code("expense_classification", "Expense classification fields", {depends_on: hasExpense}),
        ])},
      {fieldname: "template_help", fieldtype: "HTML", options: `<p class="text-muted">${esc(t("Shared approval templates appear once. Disable historical templates instead of deleting them."))}</p>`},
      {fieldname: "remove_drafts", label: t("Remove selected draft templates"), fieldtype: "Button",
        click: () => {if (!saving) {invalidatePreview(); removeDraftTemplates(dialog.fields_dict.templates.grid, persistedRows);}}},
      {fieldname: "preview", label: t("Preview Selected Template"), fieldtype: "Button", click: async () => {
        invalidatePreview();
        helpers.flushGridEditors(dialog.fields_dict.templates.grid);
        const context = previewContext();
        if (!context) return root.frappe.msgprint(t("Select exactly one template"));
        const error = validateTemplates([context.args.template], saved.corporations, context.args.template.idx);
        if (error) return root.frappe.msgprint(error);
        const sequence = previewSequence, signature = JSON.stringify(context.args);
        const current = () => {
          if (!dialog.is_visible || sequence !== previewSequence || (options.isCurrent && !options.isCurrent())) return false;
          helpers.flushGridEditors(dialog.fields_dict.templates.grid);
          const latest = previewContext();
          return latest?.row === context.row && JSON.stringify(latest.args) === signature;
        };
        try {
          const result = await call("preview_sync_template", context.args, {silent: true});
          if (!current()) return;
          dialog.fields_dict.preview_result.$wrapper.html(result.map(row => `<p><strong>${esc(t("Approval data preview"))}</strong> · ${esc(t("{0} sample approvals", [row.count]))}<br>${row.issues.map(esc).join(" / ")}</p>`).join(""));
        } catch (_) {
          if (current()) root.frappe.msgprint(t("Template preview could not be loaded. Retry."));
        }
      }},
      {fieldname: "preview_result", fieldtype: "HTML"},
      {fieldname: "mapping_section", fieldtype: "Section Break", label: t("Subsidiary mappings"), collapsible: 1},
      {fieldname: "company_mapping_workspace", fieldtype: "HTML"},
      // Retain the native Table as the single payload/validation store. The
      // mapping workspace edits this same array; filtering never drops rows.
      {fieldname: "company_mappings", label: t("Subsidiary mappings"), fieldtype: "Table", hidden: 1, data: saved.company_mappings,
        fields: helpers.textFields([
          select("match_kind", "Match by", MATCHES, {reqd: 1, in_list_view: 1, columns: 2}),
          {fieldname: "corp_id", label: t("Corporation ID"), fieldtype: "Select", options: helpers.textOptions(saved.corporations), depends_on: doc => doc.match_kind === "organization"},
          {fieldname: "source_name", label: t("Source name"), fieldtype: "Small Text", reqd: 1, in_list_view: 1, columns: 4, max_height: 64},
          {fieldname: "company", label: t("Subsidiary"), fieldtype: "Link", options: "Company", in_list_view: 1, columns: 4,
            formatter: esc,
            get_query: doc => ({filters: {name: ["in", doc.match_kind === "organization" ? saved.companies : saved.expense_companies]}})},
          {fieldname: "excluded", label: t("Keep outside legacy expense scope"), fieldtype: "Check", depends_on: doc => doc.match_kind !== "organization"},
          {fieldname: "reason", label: t("Reason"), fieldtype: "Small Text", depends_on: "eval:doc.excluded"},
        ])},
      {fieldname: "category_section", fieldtype: "Section Break", label: t("Management category mappings"), collapsible: 1},
      {fieldname: "warning_percent", label: t("Budget warning percentage"), fieldtype: "Percent", default: saved.management_rules.warning_percent, reqd: 1},
      {fieldname: "category_mappings", label: t("Management category mappings"), fieldtype: "Table", cannot_add_rows: true,
        data: saved.management_rules.category_mappings.map(row => ({...row, source_path: JSON.stringify(row.source_path), source_label: helpers.pathLabel(row.kind, row.source_path)})),
        fields: helpers.textFields([
          select("kind", "Approval Form", {budget: KINDS.budget, operating: KINDS.operating, purchase: KINDS.purchase}, {reqd: 1, read_only: 1, in_list_view: 1, columns: 2}),
          {fieldname: "source_path", fieldtype: "Small Text", hidden: 1},
          {fieldname: "source_label", label: t("Source category path"), fieldtype: "Small Text", read_only: 1, in_list_view: 1, columns: 5},
          select("category", "Management category", saved.management_categories, {reqd: 1, in_list_view: 1, columns: 3}),
        ])},
      {fieldname: "discover_categories", label: t("Unmapped management categories"), fieldtype: "Button", click: () => loadCandidates("categories")},
      {fieldname: "categories_candidates", fieldtype: "HTML"},
      {fieldname: "advanced", label: t("Advanced settings"), fieldtype: "Section Break", collapsible: 1},
      {fieldname: "business_timezone", label: t("OA business timezone"), fieldtype: "Data", default: saved.business_timezone, reqd: 1, onchange: invalidatePreview},
    ];
    dialog = new root.frappe.ui.Dialog({title: t("Configure OA Synchronization"), size: "extra-large", fields,
      onhide: () => {invalidatePreview(); mappingEditor?.destroy();},
      // Native collapsible sections inspect layout.doc for required fields.
      doc: {warning_percent: saved.management_rules.warning_percent, business_timezone: saved.business_timezone,
        common_corp_id: defaults.corp_id || (saved.corporations.length === 1 ? saved.corporations[0] : "")},
      primary_action_label: t("Save Settings"), primary_action: async () => {
        if (saving) return;
        for (const name of ["templates", "company_mappings", "category_mappings"]) helpers.flushGridEditors(dialog.fields_dict[name].grid);
        const values = dialog.get_values();
        if (!values) return;
        let data;
        try { data = payload(values, saved); }
        catch (_) { return root.frappe.msgprint(t("Category paths must be valid JSON arrays.")); }
        const error = validateTemplates(data.templates, saved.corporations);
        if (error) return root.frappe.msgprint(error);
        saving = true;
        mappingEditor?.render();
        dialog.disable_primary_action();
        try {
          await call("save_sync_settings", {values: data});
          dialog.hide();
          await onSaved?.();
          root.frappe.show_alert({message: t("Settings saved; cached records will be rechecked"), indicator: "green"});
        } finally { saving = false; dialog.enable_primary_action(); mappingEditor?.render(); }
      },
    });
    for (const name of ["templates", "company_mappings", "category_mappings"])
      helpers.guardGrid(dialog.fields_dict[name].grid, name === "templates" ? {collapsedSections: ["template_advanced"]} : {});
    // Selection and editor input invalidate already-rendered results too. The
    // response guard also compares the captured draft for programmatic changes.
    dialog.fields_dict.templates.grid.wrapper.on("input.expense-sync-preview change.expense-sync-preview", invalidatePreview);
    dialog.$wrapper.addClass("cnaw-settings-dialog");
    dialog.show();
    mappingEditor = new root.ExpenseCompanyMappings.Controller({
      element: dialog.fields_dict.company_mapping_workspace.$wrapper[0],
      grid: dialog.fields_dict.company_mappings.grid, saved, call,
      isCurrent: () => dialog.is_visible && (!options.isCurrent || options.isCurrent()),
      isSaving: () => saving,
    });
    return dialog;
  }
  return {open, payload, appendCandidate, removeDraftTemplates, inheritDefaults, validateTemplates};
});
