"""Unit tests for reporting a reference's marker, and for the warnings about its directives that decide nothing."""

import unittest
from types import MappingProxyType
from typing import Final
from unittest.mock import Mock, call

from mutation_time.mutation import kills
from update_time.domain.bound import BoundRedundancy, Verb
from update_time.domain.directive import Reason
from update_time.domain.marker import Marker, Scope
from update_time.domain.reference import Reference
from update_time.markers.parser import parse_directives
from update_time.markers.report import report_marker, warn_if_the_bound_is_redundant, warn_if_the_opt_in_is_redundant

from tests.update_time.domain_objects import bound

# The marker of each case, whether its manager steers the update, and the reports besides the reference they name.
_CASES: Final[MappingProxyType[str, tuple[str, bool, tuple[tuple[str, ...] | tuple[str, str, Reason], ...]]]] = (
    MappingProxyType(
        {
            "no marker": ("", True, ()),
            "asks for the default": (
                "ignore[hash-drift]",
                True,
                (("redundant_directive", "ignore[hash-drift]", Reason.ASKS_FOR_THE_DEFAULT), ("recognised_marker",)),
            ),
            "invalid item": ("ignore[stlae]", True, (("invalid_bracket_item", "stlae"), ("recognised_marker",))),
            "held back where the marker steers the update": (
                "ignore[update]",
                True,
                (("recognised_marker",), ("ignored",)),
            ),
            "held back where the marker steers nothing": ("ignore[update]", False, (("recognised_marker",),)),
        }
    )
)


class ReportMarkerTest(unittest.TestCase):
    """Unit tests for reporting what a reference's marker says, gets wrong, and holds back."""

    @kills.force_true(
        report_marker, "str(marker)", "a reference without a marker is logged as recognised", case="no marker"
    )
    @kills.delete_statement(
        report_marker,
        "log.redundant_directive(reference, directive, Reason.ASKS_FOR_THE_DEFAULT)",
        "a directive asking for the default is never warned about",
        case="asks for the default",
    )
    @kills.delete_statement(
        report_marker,
        "log.recognised_marker(reference)",
        "a marker is never logged as recognised",
        case="asks for the default",
    )
    @kills.force_false(
        report_marker,
        "marker.invalid_item is not None",
        "an unreadable item is never warned about",
        case="invalid item",
    )
    @kills.delete_statement(
        report_marker,
        "log.ignored(reference)",
        "a held-back update is never logged as held back",
        case="held back where the marker steers the update",
    )
    @kills.delete(
        report_marker,
        "steers_the_update and ",
        "a hold-back is logged where the package manager ignores the marker",
        case="held back where the marker steers nothing",
    )
    def test_reports_the_marker(self):
        """Test that a marker is reported as recognised, with what it gets wrong and the update it holds back."""
        for case, (directives, steers_the_update, reports) in _CASES.items():
            with self.subTest(case=case):
                log = Mock()
                reference = Reference("python", "3.12", Mock(), marker=parse_directives(directives))
                report_marker(reference, log, steers_the_update=steers_the_update)
                expected = [getattr(call, name)(reference, *arguments) for name, *arguments in reports]
                self.assertEqual(log.method_calls, expected)


class WarnIfTheBoundIsRedundantTest(unittest.TestCase):
    """Unit tests for warning about a bound that decides nothing at the reference's version."""

    @kills.delete_statement(
        warn_if_the_bound_is_redundant,
        "log.redundant_bound(reference, redundancy)",
        "a bound that decides nothing is never warned about",
    )
    def test_a_bound_deciding_nothing_is_warned_about(self):
        """Test that a bound that blocks every update at the reference's version is warned about."""
        reference, log = self.warn("patch-update", Verb.IGNORE)
        log.redundant_bound.assert_called_once_with(reference, BoundRedundancy.BLOCKS_ALL)

    def warn(self, item: str, verb: Verb) -> tuple[Reference, Mock]:
        """Warn about the bound on a reference at version 3.12, and return the reference and the logger."""
        log = Mock()
        reference = Reference("python", "3.12", Mock(), marker=Marker(version_bound=bound(verb, item)))
        warn_if_the_bound_is_redundant(reference, log)
        return reference, log

    @kills.replace(
        warn_if_the_bound_is_redundant,
        "is not None:",
        "is None or True:",
        "a bound that keeps some versions is warned about as redundant",
    )
    def test_a_live_bound_is_not_warned_about(self):
        """Test that a bound that keeps some updates at the reference's version is not warned about."""
        _reference, log = self.warn("update<3.13", Verb.ALLOW)
        self.assertEqual(log.method_calls, [])


class WarnIfTheOptInIsRedundantTest(unittest.TestCase):
    """Unit tests for warning about an opt-in that decides nothing for the reference."""

    @kills.delete_statement(
        warn_if_the_opt_in_is_redundant,
        "log.redundant_directive(reference, marker.allow_directive(scope), redundancy)",
        "an opt-in that decides nothing is never warned about",
    )
    def test_an_opt_in_deciding_nothing_is_warned_about(self):
        """Test that an opt-in on a reference whose pin does not float is warned about, saying why."""
        reference, log = self.warn(Reason.PIN_NOT_FLOATING)
        log.redundant_directive.assert_called_once_with(reference, "allow[floating-pin]", Reason.PIN_NOT_FLOATING)

    def warn(self, reason: Reason | None) -> tuple[Reference, Mock]:
        """Warn about an `allow[floating-pin]` for the reason given, and return the reference and the logger."""
        log = Mock()
        reference = Reference("python", "3.12", Mock(), marker=parse_directives("allow[floating-pin]"))
        warn_if_the_opt_in_is_redundant(reference, Scope.FLOATING_PIN, reason, log)
        return reference, log

    @kills.replace(
        warn_if_the_opt_in_is_redundant,
        "is not None:",
        "is None or True:",
        "an opt-in that keeps a pin floating is warned about as redundant",
    )
    def test_a_live_opt_in_is_not_warned_about(self):
        """Test that an opt-in on a reference whose pin may float is not warned about."""
        _reference, log = self.warn(None)
        self.assertEqual(log.method_calls, [])
