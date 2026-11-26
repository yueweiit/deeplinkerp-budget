"""Country metadata must use the already authorized company scope."""
import frappe

from budget.services import expense_regions as service


def test_company_regions_uses_country_with_one_permission_aware_query(monkeypatch):
	calls = []
	def get_list(doctype, **kwargs):
		calls.append((doctype, kwargs))
		return [frappe._dict(name="Chinese name", country="Mexico"),
			frappe._dict(name="Has MX suffix", country="China"),
			frappe._dict(name="Other", country="United States"),
			frappe._dict(name="Unset", country=""),
			frappe._dict(name="Forbidden", country="Mexico")]
	monkeypatch.setattr(frappe, "get_list", get_list)
	names = {"Chinese name", "Has MX suffix", "Other", "Unset"}
	assert service.company_regions({"companies": names}) == {
		"Chinese name": "mx", "Has MX suffix": "cn", "Other": "", "Unset": ""}
	assert calls == [("Company", {"filters": {"name": ["in", sorted(names)]},
		"fields": ["name", "country"], "limit_page_length": 0})]
	assert service.company_regions({"companies": set()}) == {}
	assert len(calls) == 1
