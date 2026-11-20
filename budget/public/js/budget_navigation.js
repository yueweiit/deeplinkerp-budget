// Discard only this app's obsolete remembered ownership. Native route resolution
// then chooses the authorized CN sidebar; other user sidebar preferences survive.
(function () {
    const moved = new Set(["budget ledger entry", "budget-ledger-entry", "budget-dashboard", "budget-config"]);
    try {
        const key = "sidebar_item_map";
        const raw = localStorage.getItem(key);
        if (!raw) return;
        const remembered = JSON.parse(raw);
        let changed = false;
        for (const [entity, choices] of Object.entries(remembered)) {
            if (!moved.has(entity.toLowerCase()) || !Array.isArray(choices)) continue;
            const keep = choices.filter((name) => name !== "Budget");
            if (keep.length === choices.length) continue;
            if (keep.length) remembered[entity] = keep;
            else delete remembered[entity];
            changed = true;
        }
        if (changed) localStorage.setItem(key, JSON.stringify(remembered));
    } catch { /* Storage may be unavailable; native navigation remains usable. */ }
})();

// Retired CN URLs are aliases only: resolve them before Page rendering, so a
// bookmark or cached Desk tab cannot mount new content under an old sidebar.
(function () {
    if (!globalThis.frappe?.boot?.cn_budget_workbench || !frappe.router) return;
    const router = frappe.router;
    if (router.cn_legacy_routes_installed) return;
    const destinations = new Map([
        ["budget-dashboard", "china-budget-execution"],
        ["budget-config", "china-budget-execution"],
        ["china-personnel-report", "china-management-reports"],
    ]);
    const nativeReroute = router.re_route;
    router.re_route = function (subpath) {
        const legacy = subpath.replace(/\/$/, "");
        const destination = destinations.get(legacy);
        if (!destination || !frappe.boot.cn_budget_workbench) {
            return nativeReroute.apply(this, arguments);
        }
        // Native parse() has not run yet on initial loads / browser back.
        this.set_route_options_from_url();
        const options = {...frappe.route_options};
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
        if (legacy === "china-personnel-report") options.cn_report = "hr";
        frappe.route_options = options;
        frappe.route_hash = window.location.hash;
        frappe.route_flags = {...frappe.route_flags, replace_route: true};
        frappe.set_route(destination);
        return true;
    };
    router.cn_legacy_routes_installed = true;
})();
