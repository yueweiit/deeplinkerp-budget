/* Edit existing field_mapping JSON through bounded, read-only OA samples. */
(function (root, factory) {
  const api = factory(root);
  if (typeof module === "object" && module.exports) module.exports = api;
  root.ExpenseTemplateMapping = api;
})(globalThis, function (root) {
  "use strict";
  const KEEP = "__keep__", DEFAULT = "__default__";
  const t = (value, args) => root.__(value, args);
  const esc = value => root.frappe.utils.escape_html(String(value ?? ""));
  const clone = value => JSON.parse(JSON.stringify(value));
  const prefix = source => source === "cn" ? "application" : "expense";
  function mapping(value) {
    const result = JSON.parse(value || "{}");
    if (!result || typeof result !== "object" || Array.isArray(result)) throw new Error("mapping");
    return result;
  }
  function patch(row, processors, edits) {
    const result = clone(row);
    for (const processor of processors) {
      const key = prefix(processor) + "_fields", map = mapping(row[key]);
      const put = (target, key, value, array = true) => {
        if (value === KEEP) return;
        if (value === DEFAULT) delete target[key];
        else target[key] = array ? [value] : value;
      };
      for (const [key, value] of Object.entries(edits.headers)) put(map, key, value);
      for (const [table, fields] of Object.entries(edits.children)) {
        const target = ((map.table_fields ||= {})[table] ||= {});
        for (const [key, value] of Object.entries(fields)) put(target, key, value);
        if (!Object.keys(target).length) delete map.table_fields[table];
        if (!Object.keys(map.table_fields).length) delete map.table_fields;
      }
      for (const [key, value] of Object.entries(edits.currency)) put(map.table_currency ||= {}, key, value, false);
      if (map.table_currency && !Object.keys(map.table_currency).length) delete map.table_currency;
      // A no-op must preserve different historical CN/MX bindings byte for byte.
      if (JSON.stringify(map) !== JSON.stringify(mapping(row[key]))) result[key] = JSON.stringify(map);
    }
    return result;
  }
  function activeTables(row, data, edits, key) {
    const value = edits.headers[key];
    if (value && value !== KEEP && value !== DEFAULT) return [value];
    if (value === DEFAULT) return data.fallbacks?.[key] || [];
    return [...new Set(data.processors.flatMap(source => {
      const map = mapping(row[prefix(source) + "_fields"]);
      return map[key] || data.defaults?.[key] || [];
    }))];
  }
  function sampleHTML(data) {
    if (!data.sample_count) return `<p class="text-warning">${esc(t("No sample approvals found; save this template disabled first"))}</p>`;
    const money = (value, currency) => value == null ? esc(t("Unavailable")) :
      esc(root.format_currency ? root.format_currency(value, currency) : `${value} ${currency || ""}`);
    return data.samples.map(sample => `<section class="mb-3">
      <strong>${esc(t("Sample {0}", [sample.index]))}</strong>
      <p>${esc(t("Header total"))}: ${money(sample.header_total, sample.currency)} ·
      ${esc(t("Detail total"))}: ${money(sample.detail_total, sample.detail_currency)} · ${esc(t("{0} detail rows", [sample.line_count]))}</p>
      ${sample.issues.length ? `<ul class="text-warning">${sample.issues.map(issue => `<li>${esc(issue)}</li>`).join("")}</ul>` : `<p class="text-success">${esc(t("Sample validation passed"))}</p>`}
      <table class="table table-bordered"><thead><tr><th>${esc(t("Department or entity"))}</th><th>${esc(t("Detail amount"))}</th></tr></thead><tbody>
      ${sample.lines.map(line => `<tr><td>${esc(line.entity)}</td><td>${money(line.amount, line.currency)}</td></tr>`).join("")}</tbody></table>
      ${sample.line_count > 20 ? `<small>${esc(t("Showing the first 20 rows; totals include all rows."))}</small>` : ""}</section>`).join("");
  }
  function editorHTML(row, data, edits) {
    const select = (label, fields, types, value, attrs, current) => {
      const options = [{id: KEEP, label: t("Keep current binding")}, {id: DEFAULT, label: t("Use default binding")},
        ...fields.filter(field => types.includes(field.type)).map(field => ({id: field.id, label: `${field.label} · ${field.id}`}))];
      if (value && !options.some(option => option.id === value)) options.push({id: value, label: `${t("Unavailable in current samples")} · ${value}`});
      return `<div class="form-group"><label>${esc(label)}</label><select class="form-control" ${attrs}>
        ${options.map(option => `<option value="${esc(option.id)}" ${option.id === (value || KEEP) ? "selected" : ""}>${esc(option.label)}</option>`).join("")}
        </select>${current?.length ? `<small class="text-muted" style="overflow-wrap:anywhere">${esc(t("Current binding"))}: ${current.map(esc).join(" / ")}</small>` : ""}</div>`;
    };
    const current = (key, table) => [...new Set(data.processors.flatMap(source => {
      const map = mapping(row[prefix(source) + "_fields"]);
      return (table ? map.table_fields?.[table]?.[key] : map[key] || data.defaults?.[key]) || [];
    }))];
    let html = `<p class="text-muted">${esc(t("Choose fields from up to 3 sample approvals. The mapping is shared by this template and is saved with OA settings."))}</p>`;
    html += `<details open><summary>${esc(t("Header fields"))}</summary>`;
    const primary = ["entity", "amount", "currency", "application_date", "budget_month", "expense_type", "description"];
    const headers = data.headers.filter(field => !field.key.endsWith("_table"));
    const fieldHTML = header => select(header.label, data.fields, header.types, edits.headers[header.key], `data-group="headers" data-key="${esc(header.key)}"`, current(header.key));
    html += headers.filter(field => primary.includes(field.key)).map(fieldHTML).join("");
    html += "</details>";
    if (headers.some(field => !primary.includes(field.key))) html += `<details class="mt-3"><summary>${esc(t("Additional header fields"))}</summary>${headers.filter(field => !primary.includes(field.key)).map(fieldHTML).join("")}</details>`;
    for (const [table, roles] of Object.entries(data.table_roles)) {
      const header = data.headers.find(field => field.key === table);
      if (!header) continue;
      const tables = activeTables(row, data, edits, table), cells = tables.length === 1 ? data.tables[tables[0]] || [] : [];
      html += `<details ${cells.length ? "open" : ""} class="mt-3"><summary>${esc(header.label)}</summary>`;
      html += select(header.label, data.fields, ["TableField"], edits.headers[table], `data-group="headers" data-key="${esc(table)}"`, current(table));
      if (!cells.length) html += `<p class="text-muted">${esc(t("Choose one detail table with populated sample rows to configure its fields."))}</p>`;
      else {
        for (const role of roles) html += select(role.label, cells, role.types, edits.children[table]?.[role.key],
          `data-group="children" data-table="${esc(table)}" data-key="${esc(role.key)}"`, current(role.key, table));
        if (table === "allocation_table") html += select(t("Allocation amount currency"),
          [{id: "CNY", type: "currency", label: t("CNY (legacy default)")}, {id: "header", type: "currency", label: t("Same as header currency")}],
          ["currency"], edits.currency[table], `data-group="currency" data-key="${esc(table)}"`,
          [...new Set(data.processors.map(source => mapping(row[prefix(source) + "_fields"]).table_currency?.[table] || "CNY"))]);
      }
      html += "</details>";
    }
    return html;
  }
  async function open({args, call, isCurrent, onApply}) {
    let data;
    try { data = await call("inspect_sync_template", args, {silent: true}); }
    catch (_) { if (isCurrent()) root.frappe.msgprint(t("Template preview could not be loaded. Retry.")); return; }
    if (!isCurrent()) return;
    const row = clone(args.template), edits = {headers: {}, children: {}, currency: {}};
    const originalDefaults = clone(data.defaults);
    let dialog, revision = 0, previewed = 0, request = 0, closed = false;
    const current = () => !closed && dialog.is_visible && isCurrent();
    const render = () => dialog.fields_dict.editor.$wrapper.html(editorHTML(row, data, edits));
    dialog = new root.frappe.ui.Dialog({title: t("Configure field mapping"), size: "extra-large", fields: [
      {fieldname: "editor", fieldtype: "HTML"},
      {fieldname: "preview_mapping", fieldtype: "Button", label: t("Preview mapping"), click: async () => {
        if (!current()) return;
        const seq = ++request, version = revision;
        dialog.fields_dict.result.$wrapper.text(t("Loading approvals…"));
        try {
          const result = await call("inspect_sync_template", {...args, template: patch(row, data.processors, edits)}, {silent: true});
          if (!current() || seq !== request || version !== revision) return;
          // Keep means the original parent draft, never a previously previewed edit.
          data = {...result, defaults: originalDefaults}; previewed = version;
          render();
          dialog.fields_dict.result.$wrapper.html(sampleHTML(result));
        } catch (_) {
          if (current() && seq === request && version === revision) {
            dialog.fields_dict.result.$wrapper.text(t("Template preview could not be loaded. Retry."));
            previewed = -1;
          }
        }
      }},
      {fieldname: "result", fieldtype: "HTML"},
    ], onhide: () => {closed = true; request++;}, primary_action_label: t("Apply to template draft"), primary_action: () => {
      if (!current()) {dialog.hide(); return;}
      if (previewed !== revision) return root.frappe.msgprint(t("Preview the changed mapping before applying it."));
      onApply(patch(row, data.processors, edits));
      dialog.hide();
      root.frappe.show_alert({message: t("Mapping applied to draft. Save OA settings to persist it."), indicator: "green"});
    }});
    dialog.fields_dict.editor.$wrapper.on("change.expense-template-mapping", "select[data-group]", function () {
      if (!current()) return;
      const {group, key, table} = this.dataset;
      if (!["headers", "children", "currency"].includes(group)) return;
      const target = group === "children" ? (edits.children[table] ||= {}) : edits[group];
      if (this.value === KEEP) delete target[key]; else target[key] = this.value;
      if (group === "headers" && key.endsWith("_table")) delete edits.children[key];
      revision++; request++; previewed = -1;
      render();
      dialog.fields_dict.result.$wrapper.text(t("Mapping changed. Preview again to check rows and totals."));
    });
    dialog.show(); render();
    dialog.fields_dict.result.$wrapper.html(sampleHTML(data));
    return dialog;
  }
  return {open, patch, activeTables, editorHTML, sampleHTML, KEEP, DEFAULT};
});
