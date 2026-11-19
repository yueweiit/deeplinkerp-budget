"""CN report adapter over the permission-filtered operating approval snapshot.

Approved amounts are recognized on approval completion, not as paid amounts.
Budget matching uses exact organization IDs; foreign currencies use month-start
rates through the same validated transport as MX.
"""
from datetime import datetime, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

import frappe


class ReportUnavailable(Exception):
    """A translated, non-sensitive report status instead of raw upstream errors."""


def timestamp(value):
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return None
        return parsed.astimezone(timezone.utc)
    except (ValueError, TypeError):
        return None


def business_date(value):
    parsed = timestamp(value)
    return parsed.astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat() if parsed else None


def current_report_source():
    from .shared_operating_oa import available
    if not available():
        return None
    from budget.services.ledger_sync_settings import current_source
    # Configuration failures are reported inline by the report service. Frappe
    # must still raise, but must not also enqueue a modal error in the response.
    muted = frappe.flags.mute_messages
    try:
        frappe.flags.mute_messages = True
        source = current_source()
    finally:
        frappe.flags.mute_messages = muted
    return CNExpenseReportSource(source) if source["profile"] == "operating_oa" else None


class CNExpenseReportSource:
    def __init__(self, source, *, reader=None, conversion=None):
        self.source = source
        self.reader = reader
        self.conversion = conversion
        self.company = None
        self.owners = {}
        self.rows = None
        self.end_date = None

    def set_scope(self, company, owners):
        self.company, self.owners = company, dict(owners)
        for row in self.source.get("organization_mappings", []):
            if row["corp_id"] == self.source["corp_id"]:
                self.owners[row["department_id"]] = {row["company"]}

    def _load(self, end_date):
        if self.rows is None or self.end_date < end_date:
            if (not self.source["configured"] or (self.source["bound_fingerprint"]
                    and self.source["bound_fingerprint"] != self.source["fingerprint"])):
                raise ReportUnavailable("Review the budget OA source settings before loading approval statistics.")
            if self.reader is None:
                from deeplinkerp_branding.services.operating_budget_report import read_rows
                self.reader = read_rows
            rows = []
            for row in self.reader(end_date):
                if row.get("source_system") != "dingtalk-oa" or row.get("source_withdrawn"):
                    continue
                identity = row.get("oa_identity") or {}
                if not identity.get("corp_id") or not identity.get("process_instance_id"):
                    raise ReportUnavailable("Some operating approvals lack source identities. Synchronize CN operating expenses first.")
                if identity["corp_id"] != self.source["corp_id"]:
                    continue
                snapshot = row.get("reporting") or {}
                department = str(row.get("source_company") or "").strip()
                department_id = str(snapshot.get("department_id") or "").strip()
                # An explicit form organization may differ from the applicant's
                # directory department. Never silently allocate to that directory.
                if department != str(snapshot.get("department_name") or "").strip():
                    department_id = ""
                rows.append({**row, "department": department, "department_id": department_id})
            self.rows, self.end_date = rows, end_date
        return self.rows

    def _in_company(self, row):
        if not self.company:
            return True
        owners = self.owners.get(row["department_id"], set())
        if len(owners) != 1 or not next(iter(owners)) or "待确认" in owners:
            raise ReportUnavailable("Expense company ownership is incomplete; company execution amounts are unavailable")
        return self.company in owners

    @staticmethod
    def _snapshot(row):
        snapshot = row.get("reporting") or {}
        if snapshot.get("version") != 1:
            raise ReportUnavailable("Synchronize CN operating expenses to refresh approval dates and task timings.")
        return snapshot

    def _amount(self, row, approved_date=None):
        if row.get("source_conflict") or row.get("currency_conflict"):
            raise ReportUnavailable("Some operating approval amounts or currencies require review; totals are unavailable.")
        if self.conversion is None:
            from deeplinkerp_branding.services.mexico_expense_fx import Conversion
            self.conversion = Conversion("CNY")
        muted = frappe.flags.mute_messages
        try:
            frappe.flags.mute_messages = True
            converted = self.conversion.line({"amount": row.get("amount"), "currency": row.get("currency"),
                "request_date": str(row.get("request_date") or ""), "finance_approved_date": approved_date})
        except Exception:
            raise ReportUnavailable("Month-start exchange rates are unavailable; converted approval totals cannot be calculated.") from None
        finally:
            frappe.flags.mute_messages = muted
        if converted["converted_amount"] is None:
            raise ReportUnavailable("Some operating approval amounts or currencies require review; totals are unavailable.")
        return Decimal(converted["converted_amount"])

    def list_approved_expenses(self, start_date, end_date):
        result = []
        for row in self._load(end_date):
            if row.get("approval_state") != "approved":
                continue
            snapshot = self._snapshot(row)
            finished, created = timestamp(snapshot.get("finish_time")), timestamp(snapshot.get("create_time"))
            if not finished or not created or finished < created:
                raise ReportUnavailable("Some approved applications lack valid completion dates. Synchronize and review their OA records.")
            day = business_date(snapshot["finish_time"])
            if not start_date <= day <= end_date or not self._in_company(row):
                continue
            result.append({"accounting_source": "cn_approved_application", "business_id": row.get("approval_no"),
                "department": row["department"], "department_id": row["department_id"],
                "amount": self._amount(row, day), "accounting_date": day, "exact_department_match": True})
        return result

    def pending_expenses(self, start_date, end_date):
        return [{"department": row["department"], "department_id": row["department_id"], "amount": self._amount(row)}
                for row in self._load(end_date) if row.get("approval_state") == "pending"
                and start_date <= str(row.get("request_date") or "") <= end_date
                and self._snapshot(row) and self._in_company(row)]

    def list_approval_efficiency_rows(self, *, start_date, end_date):
        instances, tasks = [], []
        for row in self._load(end_date):
            if not start_date <= str(row.get("request_date") or "") <= end_date:
                continue
            snapshot = self._snapshot(row)
            if not self._in_company(row):
                continue
            created, finished = timestamp(snapshot.get("create_time")), timestamp(snapshot.get("finish_time"))
            pending = row.get("approval_state") == "pending"
            if (not created or (not pending and not finished) or (finished and finished < created)
                    or not snapshot.get("tasks_available")):
                raise ReportUnavailable("Some approval timing records are incomplete. Synchronize and review their OA records.")
            identity = row["oa_identity"]["process_instance_id"]
            instances.append({"process_instance_id": identity, "process_code": snapshot.get("process_code"),
                "process_name": row.get("process_name") or snapshot.get("process_code") or "CN Operating Expenses",
                "create_time": created, "finish_time": finished})
            for task in snapshot.get("tasks", []):
                begin, end = timestamp(task.get("start_time")), timestamp(task.get("end_time"))
                # Unstarted or canceled tasks are not timing samples.
                if str(task.get("status") or "").upper() not in {"RUNNING", "COMPLETED"}:
                    continue
                if (not begin or (str(task.get("status")).upper() == "COMPLETED" and not end)
                        or (end and end < begin) or (finished and not end)):
                    raise ReportUnavailable("Some approval timing records are incomplete. Synchronize and review their OA records.")
                tasks.append({"process_instance_id": identity, "task_id": task["task_id"], "start_time": begin, "end_time": end})
        return instances, tasks
