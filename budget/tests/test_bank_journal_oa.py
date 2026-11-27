import unittest

from budget.services import bank_journal_oa as oa


def application(**changes):
	return dict(
		{
			"key": "source-A",
			"number": "202611160000000000001",
			"kind": "operating",
			"status": "approved",
			"amount": "125.50",
			"currency": "CNY",
			"application_date": "2026-10-31",
			"classification": ["办公", "租赁"],
			"facts": [{"label": "Project", "value": "ABC"}],
			"description": "场地租金",
			"department": "行政",
			"approved_at": "2026-11-02T06:31:00+00:00",
			"issues": [],
		},
		**changes,
	)


class JournalApprovalTest(unittest.TestCase):
	def test_monthly_approval_is_only_a_candidate(self):
		row = oa.candidate(application(), "2026-11-02")
		self.assertEqual(row["application_amount"], "125.50")
		self.assertIsNone(row["expense"])
		self.assertIsNone(row["date"])
		self.assertEqual((row["level2"], row["level3"], row["project"]), ("办公", "租赁", "ABC"))

	def test_unapproved_foreign_budget_ambiguous_amount_are_excluded(self):
		for change in (
			{"kind": "budget"},
			{"status": "pending"},
			{"status": "terminated"},
			{"currency": "USD"},
			{"amount": None},
			{"amount": "0"},
			{"issues": ["structure"]},
		):
			with self.subTest(change=change):
				self.assertIsNone(oa.candidate(application(**change), "2026-11-02"))

	def test_partial_payment_requires_real_date_and_amount(self):
		row = oa.candidate(application(), "2026-11-02")
		self.assertEqual(oa.confirmed_row(row, "2026-11-06", "65.30", "2026-11")["expense"], "65.30")
		for date, amount in [
			(None, "125.50"),
			("2026-11-06", "150"),
			("2026-11-06", "-1"),
			("2026-10-28", "1"),
		]:
			with self.assertRaises(ValueError):
				oa.confirmed_row(row, date, amount, "2026-11")

	def test_links_reject_ambiguous_instance_and_unrequested_number(self):
		facts = [("A", "I-1"), ("B", "I-2"), ("B", "I-3"), ("C", "I-4"), ("D", "../bad")]
		self.assertEqual(set(oa.unique_links({"A", "B", "D"}, facts)), {"A"})
		self.assertIn("procInstId=I-1", oa.unique_links({"A"}, facts)["A"])


class PaymentEvidenceTest(unittest.TestCase):
	def test_only_exact_single_recorded_payment_prefills(self):
		row = oa.candidate(application(), "2026-11-02")
		payment = {"payment_date": "2026-11-16", "bank_amount": "90.00", "bank_reference": "REF-2"}
		oa.prefill_payment(row, [payment], "2026-11")
		self.assertEqual((row["date"], row["expense"]), ("2026-11-16", "90.00"))
		self.assertEqual(oa.confirmed_row(row, "2026-11-16", "90.00", "2026-11")["bank_ref"], "REF-2")
		with self.assertRaises(ValueError):
			oa.confirmed_row(row, "2026-11-15", "90.00", "2026-11")
		other = oa.candidate(application(), "2026-11-02")
		oa.prefill_payment(other, [payment, payment], "2026-11")
		self.assertIsNone(other["expense"])
