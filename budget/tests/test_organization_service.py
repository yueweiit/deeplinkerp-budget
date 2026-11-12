from __future__ import annotations

from budget.services import organization_service as service

GROUP_ROW = {
	"dept_id": "d1",
	"name": "悦为集团",
	"path_ids": ["d1"],
	"path_names": ["悦为集团"],
	"is_current": True,
}
COMPANY_ROW = {
	"dept_id": "d2",
	"name": "悦为科技",
	"path_ids": ["d1", "d2"],
	"path_names": ["悦为集团", "悦为科技"],
	"is_current": True,
}
DEPARTMENT_ROW = {
	"dept_id": "d3",
	"name": "采购部",
	"path_ids": ["d1", "d2", "d3"],
	"path_names": ["悦为集团", "悦为科技", "采购部"],
	"is_current": True,
}
NESTED_DEPARTMENT_ROW = {
	"dept_id": "d4",
	"name": "采购一组",
	"path_ids": ["d1", "d2", "d3", "d4"],
	"path_names": ["悦为集团", "悦为科技", "采购部", "采购一组"],
	"is_current": True,
}
EXTERNAL_ROW = {
	"dept_id": "d7",
	"name": "外部部门",
	"path_ids": ["x1", "x2", "d7"],
	"path_names": ["外集团", "外公司", "外部部门"],
	"is_current": True,
}


def test_build_organization_options_splits_group_company_department() -> None:
	options = service.build_organization_options(
		[GROUP_ROW, COMPANY_ROW, DEPARTMENT_ROW, NESTED_DEPARTMENT_ROW, EXTERNAL_ROW]
	)

	assert [item["name"] for item in options["groups"]] == ["悦为集团"]
	assert [item["name"] for item in options["companies"]] == ["悦为科技"]
	assert sorted(item["id"] for item in options["departments"]) == ["d3", "d4"]


def test_build_organization_options_ignores_paths_without_group_root() -> None:
	options = service.build_organization_options([EXTERNAL_ROW])

	assert options == {"groups": [], "companies": [], "departments": []}


def test_build_organization_options_carries_parent_labels() -> None:
	options = service.build_organization_options([DEPARTMENT_ROW])

	department = options["departments"][0]
	assert department["company_dept_id"] == "d2"
	assert department["group_name"] == "悦为集团"


def test_pick_unique_department_returns_single_current_row() -> None:
	selected = service.pick_unique_department([DEPARTMENT_ROW])

	assert selected is not None
	assert selected["dept_id"] == "d3"


def test_pick_unique_department_returns_none_when_ambiguous() -> None:
	other = dict(DEPARTMENT_ROW, dept_id="d5", name="另一个采购部")

	assert service.pick_unique_department([DEPARTMENT_ROW, other]) is None


def test_pick_unique_department_ignores_historical_rows() -> None:
	historical = dict(DEPARTMENT_ROW, is_current=False)

	assert service.pick_unique_department([historical]) is None


def test_pick_unique_department_requires_non_empty_name() -> None:
	nameless = dict(DEPARTMENT_ROW, name="  ")

	assert service.pick_unique_department([nameless]) is None


def test_normalize_department_snapshots_drops_incomplete_rows() -> None:
	rows = [
		{"dept_id": "d3", "name": "采购部", "path_ids": ["d1"], "path_names": ["悦为集团"]},
		{"dept_id": "d4", "name": "采购一组", "path_ids": [], "path_names": ["悦为集团"]},
	]

	snapshots = service.normalize_department_snapshots(rows)

	assert list(snapshots) == ["d3"]
	assert snapshots["d3"]["path_names"] == ["悦为集团"]
