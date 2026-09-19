"""Unit tests for measuring what each registered mutation is worth."""

import unittest
from unittest.mock import Mock, patch

from tools import mutation_yield
from tools.mutation_yield import deviations, grouped, registrations, summary

from tests import mutation_subject
from tests.mutation import Mutation, kills
from tests.mutation_subject import Doubler, is_even, is_multiple_of_three


class RegistrationsTest(unittest.TestCase):
    """Unit tests for finding the mutations the suite registers."""

    def test_a_registered_test_yields_its_mutations(self):
        """Test that a test carrying registrations yields each of them, and one carrying none yields nothing."""
        registered, bare = Mock(), Mock()
        registered.configure_mock(
            **{
                "id.return_value": "tests.test_module.Case.test_registered",
                "_testMethodName": "test_registered",
                "test_registered": Mock(_registered_mutations=("first", "second")),
            }
        )
        bare.configure_mock(_testMethodName="test_bare", test_bare=Mock(spec=[]))
        with patch("unittest.defaultTestLoader.discover", Mock(return_value=[[[registered, bare]]])):
            self.assertEqual(
                list(registrations()),
                [
                    ("tests.test_module.Case.test_registered", "first"),
                    ("tests.test_module.Case.test_registered", "second"),
                ],
            )


class GroupedTest(unittest.TestCase):
    """Unit tests for gathering the registrations of one mutation."""

    def mutation(self, old: str) -> Mock:
        """Return a mutation anchored to a module the tests name, told from another by the snippet it replaces."""
        return Mock(module=Mock(__name__="update_time.example"), qualified_name="", old=old, new="new")

    def test_a_mutation_registered_twice_is_gathered_once(self):
        """Test that one mutation registered on two tests is keyed once, naming both, so it is measured once."""
        shared = self.mutation("shared")
        groups = grouped(
            [("case.test_one", shared), ("case.test_two", shared), ("case.test_three", self.mutation("other"))]
        )
        self.assertEqual(
            [tests for _mutation, tests in groups.values()], [["case.test_one", "case.test_two"], ["case.test_three"]]
        )

    def test_two_mutations_of_one_module_are_told_apart(self):
        """Test that the snippet a mutation replaces is part of what tells it from another of the same module."""
        groups = grouped([("case.test_one", self.mutation("first")), ("case.test_two", self.mutation("second"))])
        self.assertEqual(len(groups), 2)

    @kills(
        Mutation(
            mutation_yield._identity,
            "mutation.qualified_name",
            '""',
            "the anchor is left out of what tells mutations apart, so the sweep measures two of them as one",
        )
    )
    def test_mutations_of_one_snippet_are_told_apart_by_their_anchors(self):
        """Test that the anchor tells one mutation from another with the same module, snippet, and replacement."""
        anchors = (is_even, is_multiple_of_three, mutation_subject)
        registered = [(f"case.test_{index}", Mutation(anchor, "old", "new")) for index, anchor in enumerate(anchors)]
        self.assertEqual(len(grouped(registered)), len(anchors))


class MeasuredTest(unittest.TestCase):
    """Unit tests for the worker process that measures one mutation."""

    @kills(
        Mutation(
            mutation_yield._measured,
            'filter(None, qualified_name.split("."))',
            "[]",
            "the worker leaves the mutation anchored to the module, so a function's mutation is measured too widely",
        ),
        Mutation(
            mutation_yield._measured,
            'filter(None, qualified_name.split("."))',
            'qualified_name.split(".")',
            "the worker looks up the empty name a module anchor stands for, so the sweep ends with a traceback",
            raises="AttributeError: module 'tests.mutation_subject' has no attribute ''",
        ),
    )
    def test_the_worker_anchors_the_mutation_where_the_request_says(self):
        """Test that a worker anchors the mutation to the definition named, and to the module where none is named."""
        anchors = (("", mutation_subject), ("is_even", is_even), ("Doubler.doubled", Doubler.doubled))
        for qualified_name, anchor in anchors:
            with (
                self.subTest(qualified_name=qualified_name),
                patch.object(Mutation, "killers", autospec=True) as killers,
            ):
                mutation_yield._measured(("tests.mutation_subject", qualified_name, "old", "new"))
                self.assertEqual(killers.call_args.args[0].anchor, anchor)


class SummaryTest(unittest.TestCase):
    """Unit tests for what the measurement reports."""

    def mutation(self, regression: str, expected_killers: int | None = None) -> Mock:
        """Return a mutation reporting that regression, expecting the number of killers its registration declares."""
        return Mock(regression=regression, expected_killers=expected_killers)

    def measured(self) -> list[tuple[list[str], Mock, list[str] | None]]:
        """Return a measurement whose widest mutation is not the one whose killers' names sort last.

        One mutation is registered on two tests, as a mutation named by more than one `@kills` is. Two are killed
        by as many tests as are registered on them, so the report leaves them out. Two more declare how many tests
        are expected to kill them, one matching its killers and one no longer matching.
        """
        return [
            (["case.test_widest"], self.mutation("the first regression"), ["a1", "a2", "a3", "a4"]),
            (
                ["case.test_shared", "case.test_sharer"],
                self.mutation("a shared regression"),
                ["case.test_shared", "case.test_sharer"],
            ),
            (["case.test_alone"], self.mutation("a third regression"), ["case.test_alone"]),
            (["case.test_unguarded"], self.mutation("a fourth regression"), []),
            (["case.test_stale"], self.mutation("a fifth regression"), None),
            (["case.test_wider", "case.test_widener"], self.mutation("a sixth regression"), ["z1", "z2", "z3"]),
            (["case.test_declared"], self.mutation("a declared regression", 3), ["d1", "d2", "d3"]),
            (["case.test_moved"], self.mutation("a moved regression", 2), ["m1", "m2", "m3", "m4", "m5"]),
        ]

    def test_it_counts_what_only_its_own_tests_kill(self):
        """Test that a mutation two registered tests kill counts alongside one a single registered test kills."""
        reported = summary(self.measured())
        self.assertIn("mutations measured: 8, registered on 10 tests", reported)
        self.assertIn("killed only by the tests registered on it: 2", reported)  # the one nothing kills is left out
        self.assertIn("the snippet no longer matches the file: 1", reported)

    @kills(
        Mutation(
            mutation_yield.deviations,
            "measurement[2] is None or _deviates(*measurement)",
            "_deviates(*measurement)",
            "the sweep counts a stale mutation as holding, so a registration gone stale never fails the build",
        )
    )
    def test_every_deviation_is_named(self):
        """Test that a measurement the sweep reports is named, whether its snippet went stale or its killers moved."""
        reported = [mutation.regression for _tests, mutation, _found in deviations(self.measured())]
        expected = ["a fifth regression", "a fourth regression", "a moved regression", "a sixth regression"]
        self.assertEqual(sorted(reported), sorted([*expected, "the first regression"]))

    def test_a_mutation_only_its_own_tests_kill_is_left_out(self):
        """Test that a mutation killed by exactly the tests registered on it is left out of the report."""
        reported = summary(self.measured())
        self.assertNotIn("a third regression", reported)  # one registration, killed by that one test
        self.assertNotIn("a shared regression", reported)  # two registrations, killed by those two tests
        self.assertIn("the first regression", reported)  # one registration, killed by four tests

    def test_a_reported_mutation_names_what_was_expected(self):
        """Test that a reported mutation names how many tests killed it and how many were expected to."""
        reported = summary(self.measured())
        self.assertIn("4 (expected 1)  case.test_widest", reported)
        self.assertIn("3 (expected 2)  case.test_wider", reported)

    def test_a_declared_expectation_is_what_the_killers_are_judged_against(self):
        """Test that a mutation killed by as many tests as its registration declares is left out of the report."""
        reported = summary(self.measured())
        self.assertNotIn("a declared regression", reported)  # one registration, three killers, three declared
        self.assertIn("the first regression", reported)  # one registration, four killers, none declared

    @kills(
        Mutation(
            mutation_yield,
            '            f"    {len(found or []):>4} (expected {_expected(tests, mutation)})  {_named(tests)}"',
            '            f"    {len(found or []):>4} (expected {len(tests)})  {_named(tests)}"',
            "the report shows how many tests are registered on a mutation rather than the number it declares",
        )
    )
    def test_a_declared_expectation_that_no_longer_holds_is_reported(self):
        """Test that a mutation killed by a different number of tests than its registration declares is reported."""
        self.assertIn("5 (expected 2)  case.test_moved", summary(self.measured()))

    def test_the_unexpected_are_ranked_by_how_many_tests_kill_them(self):
        """Test that the ranking reads the number of killers, not the names those killers sort under."""
        ranked = [line.split(maxsplit=1)[0] for line in summary(self.measured()).splitlines()[4:]]
        self.assertEqual(ranked, ["5", "4", "3", "0"])

    def test_a_mutation_registered_on_several_tests_names_how_many_share_it(self):
        """Test that a shared mutation is named by one of its tests and the number of the rest."""
        self.assertIn("case.test_wider (+1 more)", summary(self.measured()))
