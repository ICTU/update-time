"""Unit tests for the audit of the mutations the tests register."""

import io
import os
import sys
import time
import unittest
from contextlib import redirect_stdout
from unittest.mock import Mock, patch

from tools import audit_mutations as audit_module
from tools.audit_mutations import _audit, _Finding, _passes_unmutated, _SuiteRun, main

from tests import mutation_subject
from tests.helpers import time_zone
from tests.mutation import Mutation, Outcome, Result, kills

# A registered test that kills a mutation of `is_even`, and another test that kills it as well.
_REGISTERED = "tests.test_mutation.IsEvenTest.test_an_odd_number"
_ANOTHER_KILLER = "tests.test_mutation.IsEvenTest.test_an_even_number"
_A_SURVIVOR = "tests.test_mutation.IsMultipleOfThreeTest.test_a_multiple_of_three"
# A test of this module that registers a mutation, so the audit reaches it.
_OWN_REGISTRATION = "tests.tools.test_audit_mutations.AuditTest.test_a_registration_only_its_own_test_kills"
_ODD_REPORTED_AS_EVEN = Mutation(
    mutation_subject.is_even, "number % 2 == 0", "number % 2 != 0", "an odd number is reported as even"
)


def _run_with(other: str) -> _SuiteRun:
    """Return a run in which the registered test and the other one ran the subject's mutated line."""
    return _SuiteRun({_ODD_REPORTED_AS_EVEN.path: {8: [_REGISTERED, other]}}.__getitem__, {})


# The mixin whose tests five image updaters inherit.
_IMAGE_UPDATER_MIXIN = "tests.update_time.registry.ImageUpdaterTestMixin"
# The name of a mixin's test, and of its copies in the two classes that inherit it.
_MIXIN_TEST = "tests.m.Mixin.test_m"
_COPIES = ["tests.a.ATest.test_m", "tests.c.CTest.test_m"]


def _run_of_lines(contexts_by_line: dict[int, list[str]]) -> _SuiteRun:
    """Return a run in which the contexts ran the lines of a subject, the mixin's test standing for its copies."""
    return _SuiteRun({"subject.py": contexts_by_line}.__getitem__, {_MIXIN_TEST: _COPIES})


class CandidatesTest(unittest.TestCase):
    """Unit tests for the tests that may kill a mutation besides the test registering it."""

    def test_the_other_tests_that_ran_the_mutated_lines(self):
        """Test that the candidates are the tests that ran a mutated line, other than the registered test."""
        run = _run_of_lines(
            {
                8: ["tests.a.ATest.test_a", "tests.b.BTest.test_b", ""],
                9: ["tests.c.CTest.test_c"],
                10: ["tests.d.DTest.test_d"],
            }
        )
        self.assertEqual(
            run.candidates("subject.py", range(8, 10), "tests.a.ATest.test_a"),
            ["tests.b.BTest.test_b", "tests.c.CTest.test_c"],
        )

    def test_a_test_a_mixin_defines_stands_for_its_copy_in_each_class_inheriting_it(self):
        """Test that a test a mixin defines is a candidate in each class that inherits it."""
        run = _run_of_lines({8: [_MIXIN_TEST, "tests.b.BTest.test_b"]})
        self.assertEqual(run.candidates("subject.py", range(8, 9), "tests.b.BTest.test_b"), _COPIES)

    def test_a_registered_test_a_mixin_defines_is_no_candidate_in_any_class_inheriting_it(self):
        """Test that the copies of a registered test a mixin defines do not count as other tests."""
        run = _run_of_lines({8: [_MIXIN_TEST, "tests.b.BTest.test_b"]})
        self.assertEqual(run.candidates("subject.py", range(8, 9), _MIXIN_TEST), ["tests.b.BTest.test_b"])


class PassesUnmutatedTest(unittest.TestCase):
    """Unit tests for whether a candidate passes against the code as it is."""

    def test_a_test_that_passes_and_one_that_does_not(self):
        """Test that a passing test passes, and that a test the loader cannot find does not."""
        self.assertTrue(_passes_unmutated(_A_SURVIVOR))
        self.assertFalse(_passes_unmutated("tests.test_mutation.IsEvenTest.test_that_does_not_exist"))


class AuditTest(unittest.TestCase):
    """Unit tests for checking each registered mutation against the other tests that ran its lines."""

    def test_a_registration_another_test_kills_too(self):
        """Test that a registration whose mutation another test kills as well is reported with that test."""
        run = _run_with(_ANOTHER_KILLER)
        findings = _audit([(_REGISTERED, _ODD_REPORTED_AS_EVEN)], run)
        self.assertEqual(findings, [_Finding(_REGISTERED, _ODD_REPORTED_AS_EVEN.regression, [_ANOTHER_KILLER])])

    @kills(
        Mutation(
            audit_module._finding,
            "outcome is Outcome.KILLED",
            "outcome",
            "every candidate that passes without the mutation counts as a kill, whatever the mutated run shows",
        )
    )
    def test_a_registration_only_its_own_test_kills(self):
        """Test that a registration whose mutation the other tests that ran its lines survive is not reported."""
        run = _run_with(_A_SURVIVOR)
        with patch.object(Mutation, "check", autospec=True, side_effect=Mutation.check) as checked:
            findings = _audit([(_REGISTERED, _ODD_REPORTED_AS_EVEN)], run)
        self.assertEqual(findings, [])
        checked.assert_called_once_with(_ODD_REPORTED_AS_EVEN, _A_SURVIVOR)

    def test_a_candidate_that_fails_without_the_mutation_is_not_counted(self):
        """Test that a candidate failing against the unmutated code is not checked, so it does not count as a kill."""
        run = _run_with(_ANOTHER_KILLER)
        with (
            patch.object(audit_module, "_passes_unmutated", Mock(return_value=False)) as passes,
            patch.object(Mutation, "check") as checked,
        ):
            findings = _audit([(_REGISTERED, _ODD_REPORTED_AS_EVEN)], run)
        self.assertEqual(findings, [])
        passes.assert_called_once_with(_ANOTHER_KILLER)
        checked.assert_not_called()

    def test_the_candidates_are_checked_in_utc(self):
        """Test that a candidate is checked in UTC, and that the machine's own zone is restored afterwards."""
        zones = []

        def check(_mutation: Mutation, _test: str) -> Result:
            zones.append((os.environ["TZ"], time.timezone))
            return Result(Outcome.SURVIVED)

        with time_zone("Asia/Tokyo"):
            with patch.object(Mutation, "check", autospec=True, side_effect=check):
                _audit([(_REGISTERED, _ODD_REPORTED_AS_EVEN)], _run_with(_A_SURVIVOR))
            zone_after = (os.environ["TZ"], time.timezone)
        # `time.timezone` holds the seconds west of UTC, as of the last `time.tzset()`.
        self.assertEqual(zones, [("UTC", 0)])
        self.assertEqual(zone_after, ("Asia/Tokyo", -9 * 3600))

    def test_the_machine_zone_is_restored_when_a_check_raises(self):
        """Test that the machine's own zone is restored when checking a candidate raises."""
        with time_zone("Asia/Tokyo"):
            with patch.object(Mutation, "check", side_effect=RuntimeError), self.assertRaises(RuntimeError):
                _audit([(_REGISTERED, _ODD_REPORTED_AS_EVEN)], _run_with(_A_SURVIVOR))
            self.assertEqual((os.environ["TZ"], time.timezone), ("Asia/Tokyo", -9 * 3600))


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
        registrations, run = self.audited.call_args.args
        self.assertIn(_OWN_REGISTRATION, {test for test, _mutation in registrations})
        self.assertIs(run.contexts_of, self.coverage.get_data.return_value.contexts_by_lineno)

    def test_a_test_a_mixin_defines_stands_for_the_test_of_each_class_inheriting_it(self):
        """Test that the context naming a test a mixin defines stands for that test in each class inheriting it."""
        self.report([])
        _registrations, run = self.audited.call_args.args
        updaters = "tests.update_time.updaters"
        expected = [
            f"{updaters}.test_update_circle_ci_config.UpdateCircleCIConfigTest.test_bumped",
            f"{updaters}.test_update_devcontainer.UpdateDevcontainerTest.test_bumped",
            f"{updaters}.test_update_dockerfile_base_image.UpdateDockerfileTest.test_bumped",
            f"{updaters}.test_update_gitlab_ci_config.UpdateGitLabCIConfigTest.test_bumped",
            f"{updaters}.test_update_manifest_images.UpdateManifestImagesTest.test_bumped",
        ]
        self.assertEqual(sorted(run.ids_by_context[f"{_IMAGE_UPDATER_MIXIN}.test_bumped"]), expected)

    def test_a_registration_a_mixin_test_makes_is_audited_once_under_the_mixin(self):
        """Test that a mutation a mixin's test registers is audited once, named after the mixin's test."""
        self.report([])
        registered = [test for test, _mutation in self.audited.call_args.args[0]]
        drift_test = "test_drift_inside_the_cooldown_is_held_back_silently"
        self.assertEqual(registered.count(f"{_IMAGE_UPDATER_MIXIN}.{drift_test}"), 1)
        self.assertEqual(
            [test for test in registered if test.endswith(f".{drift_test}")], [f"{_IMAGE_UPDATER_MIXIN}.{drift_test}"]
        )

    def test_the_registrations_of_the_tests_of_the_mutations_are_not_audited(self):
        """Test that the registrations of the tests exercising the mutations themselves are left out of the audit."""
        self.report([])
        registered = {test for test, _mutation in self.audited.call_args.args[0]}
        self.assertIn(_OWN_REGISTRATION, registered)
        self.assertNotIn(_REGISTERED, registered)
