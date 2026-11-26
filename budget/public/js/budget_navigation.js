// Discard obsolete ownership only; native resolution chooses the current sidebar.
(function () {
    const moved = new Set(["budget ledger entry", "budget-ledger-entry", "budget-dashboard", "budget-config"]);
    try {
        const key = "sidebar_item_map";
        const raw = localStorage.getItem(key);
        if (!raw) return;
        const remembered = JSON.parse(raw);
        const unified = globalThis.frappe?.boot?.unified_expense_management;
        const retired = new Set(["china operating expenses", "mexico expenses",
            ...(globalThis.frappe?.boot?.expense_retired_sidebars || [])]);
        let changed = false;
        for (const [entity, choices] of Object.entries(remembered)) {
            if (!Array.isArray(choices)) continue;
            const keep = choices.filter(name => !(moved.has(entity.toLowerCase()) && name === "Budget")
                && !(unified && retired.has(String(name).toLowerCase())));
            if (keep.length === choices.length) continue;
            if (keep.length) remembered[entity] = keep;
            else delete remembered[entity];
            changed = true;
        }
        if (changed) localStorage.setItem(key, JSON.stringify(remembered));
    } catch { /* Storage may be unavailable; native navigation remains usable. */ }
})();

// Retired regional URLs are aliases only: resolve them before Page rendering, so a
// bookmark or cached Desk tab cannot mount new content under an old sidebar.
(function () {
    if (!(globalThis.frappe?.boot?.cn_budget_workbench || globalThis.frappe?.boot?.unified_expense_management) || !frappe.router) return;
    const router = frappe.router;
    if (router.cn_legacy_routes_installed) return;
    const cnDestinations = new Map([
        ["budget-dashboard", "china-budget-execution"],
        ["budget-config", "china-budget-execution"],
        ["china-personnel-report", "china-management-reports"],
    ]);
    const unified = () => Boolean(frappe.boot.unified_expense_management);
    const destinations = () => unified()
        ? new Map(Object.entries(frappe.boot.expense_legacy_routes || {}))
        : frappe.boot.cn_budget_workbench ? cnDestinations : new Map();
    function objectFilter(value) {
        try {
            for (let i = 0; i < 2 && typeof value === "string"; i++) value = JSON.parse(value);
            return value && typeof value === "object" && !Array.isArray(value) ? value : {};
        } catch { return {}; }
    }
    function expenseFilters(legacy) {
        // Read the current URL, never filters left by a previously opened tab.
        const params = new URLSearchParams(window.location.search);
        if (params.has("expense_filters")) {
            const values = objectFilter(params.get("expense_filters"));
            return Object.fromEntries(["month", "company", "region", "category", "source_category", "measure"]
                .filter(key => typeof values[key] === "string").map(key => [key, values[key]]));
        }
        const values = objectFilter(params.get(legacy.startsWith("mexico-") ? "mx_filters" : "cn_approval_filters"));
        const month = values.month || values.to_month || (typeof values.to_date === "string" ? values.to_date.slice(0, 7) : "");
        // Old finance dates, statuses, currencies and categories have different
        // semantics. Carry only the subsidiary and last selected calendar month.
        return {
            ...(typeof month === "string" && /^\d{4}-(0[1-9]|1[0-2])$/.test(month) ? {month} : {}),
            company: typeof values.company === "string" ? values.company : "",
        };
    }
    function migrateTabs(controller) {
        if (!controller?.state?.tabs) return;
        const targets = destinations();
        const pageKey = tab => Array.isArray(tab.route) && tab.route.length === 1 ? tab.route[0] : null;
        const affected = new Set(controller.state.tabs.map(tab => targets.get(pageKey(tab))).filter(Boolean));
        if (!affected.size) return;
        const tabs = [], positions = new Map();
        for (const source of controller.state.tabs) {
            const key = pageKey(source), destination = targets.get(key);
            const canonical = destination || key;
            if (!affected.has(canonical)) { tabs.push(source); continue; }
            let tab = {...source};
            if (destination) controller.update_tab_from_route(tab, [destination], false);
            if (positions.has(canonical)) {
                const index = positions.get(canonical), previous = tabs[index];
                // Prefer an already-open canonical tab's metadata, while retaining
                // the earliest position and any pinned / recently-used alias.
                tab = {...(destination ? previous : tab),
                    pinned: Boolean(previous.pinned || tab.pinned),
                    last_accessed: Math.max(previous.last_accessed || 0, tab.last_accessed || 0)};
                tabs[index] = tab;
            } else {
                positions.set(canonical, tabs.length);
                tabs.push(tab);
            }
        }
        controller.state.tabs = tabs;
        controller.state.active_route_key = targets.get(controller.state.active_route_key) || controller.state.active_route_key;
        controller.last_route_key = targets.get(controller.last_route_key) || controller.last_route_key;
        if (controller.container) controller.render();
        controller.persist();
    }
    // Runs after native restore (including its pin-version migration), before
    // initial tab rendering. No other user's storage or unrelated tab is touched.
    window.addEventListener?.("custom-filters:desk-tabs-restored", event => migrateTabs(event.detail));
    migrateTabs(window.CustomFiltersDeskTabs);
    const nativeReroute = router.re_route;
    router.re_route = function (subpath) {
        const legacy = subpath.replace(/\/$/, "");
        if (unified() && frappe.boot.expense_cash_journal && legacy === "china-cash-journal") {
            // Old journal bookmarks can still carry a now-retired sidebar.
            // Keep the existing Page and its parameters; only repair ownership.
            const params = new URLSearchParams(window.location.search);
            let sidebar = params.get("sidebar");
            try { sidebar = JSON.parse(sidebar); } catch { /* Native plain query value. */ }
            const retired = new Set(["china operating expenses", "mexico expenses",
                ...(frappe.boot.expense_retired_sidebars || [])]);
            if (!sidebar || retired.has(String(sidebar).toLowerCase())) {
                this.set_route_options_from_url();
                frappe.route_options = {...frappe.route_options, sidebar: "Expense Management"};
                frappe.route_hash = window.location.hash;
                frappe.route_flags = {...frappe.route_flags, replace_route: true};
                frappe.set_route("china-cash-journal");
                return true;
            }
        }
        const destination = destinations().get(legacy);
        if (!destination) {
            return nativeReroute.apply(this, arguments);
        }
        // Native parse() has not run yet on initial loads / browser back.
        this.set_route_options_from_url();
        const options = {...frappe.route_options};
        if (unified()) {
            for (const key of Object.keys(options)) {
                if (key.startsWith("cn_") || key.startsWith("mx_") || key === "expense_filters") delete options[key];
            }
            options.expense_filters = expenseFilters(legacy);
            // Native desktop links carry a sidebar override. It must not point
            // at a retired CN/MX or now-empty custom sidebar after redirecting.
            options.sidebar = "Expense Management";
        }
        // Native anchor clicks serialize query values once more than direct
        // bookmark loads. The workbench contract requires an object in both cases.
        if (typeof options.cn_approval_filters === "string") {
            try {
                const filters = JSON.parse(options.cn_approval_filters);
                if (filters && typeof filters === "object" && !Array.isArray(filters)) {
                    options.cn_approval_filters = filters;
                } else delete options.cn_approval_filters;
            } catch { delete options.cn_approval_filters; }
        }
        delete options.cn_filters; // Retired workbench contract.
        if (!unified() && legacy === "china-personnel-report") options.cn_report = "hr";
        frappe.route_options = options;
        frappe.route_hash = window.location.hash;
        frappe.route_flags = {...frappe.route_flags, replace_route: true};
        migrateTabs(window.CustomFiltersDeskTabs);
        frappe.set_route(destination);
        return true;
    };
    router.cn_legacy_routes_installed = true;
})();
