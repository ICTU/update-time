"""Unit tests for the shared test helpers."""

import types
import unittest
from functools import cache
from unittest.mock import patch

import update_time
from update_time.domain.bound import Verb
from update_time.sources import pypi
from update_time.sources.pypi import release_metadata

from tests.helpers import patch_get
from tests.update_time.helpers import CacheClearingTestCase, _all_cached_functions, _cached_functions, bound


class CacheClearingTest(unittest.TestCase):
    """Unit tests for the base test case that clears the caches."""

    def test_a_populated_cache_is_cleared(self):
        """Test that a cache one test populated is empty by the time the next one runs."""
        release_metadata.cache_clear()  # Whatever ran before this test may have left entries of its own behind.
        with patch_get({"info": {}}):
            release_metadata("package", "1.0")
        self.assertEqual(release_metadata.cache_info().currsize, 1)
        CacheClearingTestCase().setUp()
        self.assertEqual(release_metadata.cache_info().currsize, 0)

    def test_a_cache_no_list_names_is_cleared(self):
        """Test that a cached function added to a module is cleared, so adding one calls for no bookkeeping."""
        cached = cache(int)
        cached.__module__ = pypi.__name__
        with patch.object(pypi, "newly_cached", cached, create=True):
            cached("1")
            self.assertEqual(cached.cache_info().currsize, 1)
            CacheClearingTestCase().setUp()
            self.assertEqual(cached.cache_info().currsize, 0)


class CachedFunctionsTest(unittest.TestCase):
    """Unit tests for discovering the cached functions a module defines."""

    def test_a_cached_function_the_package_itself_defines_is_found(self):
        """Test that a cache the package's own module defines is found, not only those of the modules within it."""
        package = types.ModuleType("fake_package")
        cached = cache(int)
        cached.__module__ = package.__name__
        vars(package).update(__path__=[], cached=cached)  # An empty path leaves the package no module to walk into.
        self.assertEqual(_all_cached_functions(package), [cached])

    def test_the_cached_functions_of_the_package_are_found(self):
        """Test that walking the package reaches a cached function a module nested in it defines."""
        self.assertIn(release_metadata, _all_cached_functions(update_time))

    def test_only_the_cached_function_the_module_defines_is_returned(self):
        """Test that the discovery returns the module's own cached function, not its plain or its imported one.

        A cached function another module defines is cleared where that module is scanned, so returning it here
        would clear it twice and claim it for the wrong module.
        """
        module = types.ModuleType("fake")
        # Builtins stand in for functions the fixture would otherwise have to give a body, and `cache` wraps one
        # into what the discovery looks for. What each returned function wraps is asserted, so a leak is named.
        cached, imported = cache(int), cache(str)
        cached.__module__ = module.__name__
        vars(module).update(cached=cached, plain=str, imported=imported)
        self.assertEqual([function.__wrapped__ for function in _cached_functions(module)], [int])


class BoundTest(unittest.TestCase):
    """Unit tests for the bound helper."""

    def test_item_that_is_no_bound(self):
        """Test that the helper fails on an item that is not a bound, so a typo can't silently weaken a test."""
        self.assertRaises(ValueError, bound, Verb.ALLOW, "not-a-bound")
