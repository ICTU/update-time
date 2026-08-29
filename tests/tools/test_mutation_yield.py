"""Unit tests for measuring what each registered mutation is worth."""

import unittest
from unittest.mock import Mock, patch

from tools.mutation_yield import grouped, registrations, summary


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
        """Return a mutation of a module the tests name, told from another by the snippet it replaces."""
        return Mock(module=Mock(__name__="update_time.example"), old=old, new="new")

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


class SummaryTest(unittest.TestCase):
    """Unit tests for what the measurement reports."""

    def measured(self) -> list[tuple[list[str], str, list[str] | None]]:
        """Return a measurement whose widest mutation is not the one whose killers' names sort last.

        One mutation is registered on two tests, as a mutation named by more than one `@kills` is.
        """
        return [
            (["case.test_widest"], "the first regression", ["a1", "a2", "a3", "a4"]),
            (["case.test_shared", "case.test_sharer"], "a shared regression", ["case.test_shared", "case.test_sharer"]),
            (["case.test_alone"], "a third regression", ["case.test_alone"]),
            (["case.test_unguarded"], "a fourth regression", []),
            (["case.test_stale"], "a fifth regression", None),
        ]

    def test_it_counts_what_only_its_own_tests_kill(self):
        """Test that a mutation two registered tests kill counts alongside one a single registered test kills."""
        reported = summary(self.measured())
        self.assertIn("mutations measured: 5, registered on 6 tests", reported)
        self.assertIn("killed only by the tests registered on it: 2", reported)
        self.assertIn("the snippet no longer matches the file: 1", reported)

    def test_a_mutation_nothing_kills_is_not_counted_as_guarded(self):
        """Test that a mutation no test kills is left out, since a group killing nothing guards nothing."""
        self.assertIn("killed only by the tests registered on it: 2", summary(self.measured()))

    def test_the_widest_are_ranked_by_how_many_tests_kill_them(self):
        """Test that the ranking reads the number of killers, not the names those killers sort under."""
        ranked = [line.split(maxsplit=1)[0] for line in summary(self.measured()).splitlines()[4:]]
        self.assertEqual(ranked, ["4", "2", "1", "0", "0"])

    def test_a_mutation_registered_on_several_tests_names_how_many_share_it(self):
        """Test that a shared mutation is named by one of its tests and the number of the rest."""
        self.assertIn("case.test_shared (+1 more)", summary(self.measured()))
