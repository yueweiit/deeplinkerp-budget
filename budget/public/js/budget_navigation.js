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
