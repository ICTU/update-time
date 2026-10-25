"""Unit tests for the audit of the mutations the tests register."""

import io
import sys
import unittest
from contextlib import redirect_stdout
from unittest.mock import Mock, patch

from tools import audit_mutations as audit_module
from tools.audit_mutations import _audit, _candidates, _Finding, main

from tests import mutation_subject
from tests.mutation import Mutation, kills

# A registered test that kills a mutation of `is_even`, and another test that kills it as well.
_REGISTERED = "tests.test_mutation.IsEvenTest.test_an_odd_number"
_ANOTHER_KILLER = "tests.test_mutation.IsEvenTest.test_an_even_number"
_A_SURVIVOR = "tests.test_mutation.IsMultipleOfThreeTest.test_a_multiple_of_three"
_ODD_REPORTED_AS_EVEN = Mutation(
    mutation_subject.is_even, "number % 2 == 0", "number % 2 != 0", "an odd number is reported as even"
)


class CandidatesTest(unittest.TestCase):
    """Unit tests for the tests that may kill a mutation besides the test registering it."""

    def test_the_other_tests_that_ran_the_mutated_lines(self):
        """Test that the candidates are the tests that ran a mutated line, other than the registered test."""
        contexts_by_line = {
            8: ["tests.a.ATest.test_a", "tests.b.BTest.test_b", ""],
            9: ["tests.c.CTest.test_c"],
            10: ["tests.d.DTest.test_d"],
        }
        self.assertEqual(
            _candidates(contexts_by_line, range(8, 10), "tests.a.ATest.test_a"),
            {"tests.b.BTest.test_b", "tests.c.CTest.test_c"},
        )


class AuditTest(unittest.TestCase):
    """Unit tests for checking each registered mutation against the other tests that ran its lines."""

    def test_a_registration_another_test_kills_too(self):
        """Test that a registration whose mutation another test kills as well is reported with that test."""
        contexts_of = {_ODD_REPORTED_AS_EVEN.path: {8: [_REGISTERED, _ANOTHER_KILLER]}}.__getitem__
        findings = _audit([(_REGISTERED, _ODD_REPORTED_AS_EVEN)], contexts_of)
        self.assertEqual(findings, [_Finding(_REGISTERED, _ODD_REPORTED_AS_EVEN.regression, [_ANOTHER_KILLER])])

    @kills(
        Mutation(
            audit_module._audit,
            "return [finding for finding in findings if finding.killers]",
            "return findings",
            "a registration only its own test kills is reported as killed by others, with an empty list of them",
        )
    )
    def test_a_registration_only_its_own_test_kills(self):
        """Test that a registration whose mutation the other tests that ran its lines survive is not reported."""
        contexts_of = {_ODD_REPORTED_AS_EVEN.path: {8: [_REGISTERED, _A_SURVIVOR]}}.__getitem__
        with patch.object(Mutation, "check", autospec=True, side_effect=Mutation.check) as checked:
            findings = _audit([(_REGISTERED, _ODD_REPORTED_AS_EVEN)], contexts_of)
        self.assertEqual(findings, [])
        checked.assert_called_once_with(_ODD_REPORTED_AS_EVEN, _A_SURVIVOR)


class MainTest(unittest.TestCase):
    """Unit tests for the report the command line writes."""

    def report(self, findings: list[_Finding]) -> int:
        """Run the audit with coverage and the suite stood in for, finding as given, and return its exit code."""
        self.written = io.StringIO()
        self.coverage = Mock()
        self.audited = Mock(return_value=findings)
        with (
            patch.object(sys, "argv", ["audit_mutations.py"]),
            patch("tools.audit_mutations.Coverage", Mock(return_value=self.coverage)),
            patch("unittest.TextTestRunner.run"),
            patch("tools.audit_mutations._audit", self.audited),
            redirect_stdout(self.written),
        ):
            return main()

    def test_the_registrations_other_tests_kill_as_well(self):
        """Test that each registration other tests kill as well is reported with them, and that the run fails."""
        self.assertEqual(self.report([_Finding(_REGISTERED, "a regression", [_ANOTHER_KILLER, _A_SURVIVOR])]), 1)
        self.assertEqual(
            self.written.getvalue(),
            f"{_REGISTERED}: a regression; also killed by {_ANOTHER_KILLER}, {_A_SURVIVOR}\n"
            "registrations other tests kill as well: 1\n",
        )

    def test_the_registrations_are_audited_against_the_contexts_of_one_run(self):
        """Test that each registration is audited against the contexts the run records, and a clean audit passes."""
        self.assertEqual(self.report([]), 0)
        self.assertEqual(self.written.getvalue(), "registrations other tests kill as well: 0\n")
        self.coverage.set_option.assert_any_call("run:dynamic_context", "test_function")
        registrations, contexts_of = self.audited.call_args.args
        own = f"{AuditTest.__module__}.AuditTest.test_a_registration_only_its_own_test_kills"
        self.assertIn(own, {test for test, _mutation in registrations})
        self.assertIs(contexts_of, self.coverage.get_data.return_value.contexts_by_lineno)
