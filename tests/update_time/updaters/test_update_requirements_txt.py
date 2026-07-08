"""Unit tests for the requirements.txt update script."""

import unittest
from datetime import UTC, datetime
from pathlib import PurePath
from unittest.mock import MagicMock, Mock, patch

from update_time.domain.staleness import STALE_AFTER_DAYS_ENV_VAR
from update_time.updaters.update_requirements_txt import REQUIREMENTS_GLOB_PATTERNS, update_requirements_txts

from tests.update_time.assertions import (
    assert_success,
)
from tests.update_time.helpers import LoggingTestCase, mock_path, mock_response

OLD = "2020-01-01T00:00:00.000000Z"  # Well outside the cooldown window.
PUBLISHED = "1.1, published: 2020-01-01 00:00"  # How the OLD publication date is rendered in the log.


@patch("requests.get")
@patch("pathlib.Path.rglob")
class UpdateRequirementsTxtTest(LoggingTestCase):
    """Unit tests for the update requirements.txt function."""

    def requirements_file(self, contents: str, *, sibling_in: bool = False) -> Mock:
        """Return a mock requirements file, optionally with a sibling `.in` source file present.

        `is_compiled` treats a file as compiled when `(path.parent / f"{path.stem}.in").exists()`, so model the
        parent's `/` as returning that sibling `.in` file, whose `exists()` reflects `sibling_in`.
        """
        requirements_txt = mock_path(contents)
        requirements_txt.stem = "requirements"  # so the sibling checked for is `requirements.in`
        sibling_in_file = Mock(exists=Mock(return_value=sibling_in))
        requirements_txt.parent = MagicMock()
        requirements_txt.parent.__truediv__.return_value = sibling_in_file
        return requirements_txt

    def pypi(self, *versions: str, bump: bool = False, upload_time: str = OLD) -> list:
        """Return mock PyPI responses: the Index API versions, plus per-version metadata when a bump is expected."""
        responses = [mock_response({"versions": list(versions)})]
        if bump:
            info = {"description": "", "yanked": False}
            responses.append(mock_response({"info": info, "urls": [{"upload_time_iso_8601": upload_time}]}))
        return responses

    def stale_pypi(self, *versions: str, upload_time: str = OLD) -> list:
        """Return a mock Index API response listing the versions and a distribution file with the given upload time."""
        return [mock_response({"versions": list(versions), "files": [{"upload-time": upload_time}]})]

    def test_no_change(self, mock_rglob: Mock, mock_get: Mock):
        """Test that a pin already on the latest version is left unchanged."""
        requirements_txt = self.requirements_file("flask==1.0\n")
        mock_rglob.return_value = [requirements_txt]
        mock_get.side_effect = self.pypi("1.0")
        assert_success(update_requirements_txts())
        requirements_txt.write_text.assert_not_called()
        self.assert_path_logged(requirements_txt)
        self.assert_no_new_version_logged()
        self.assert_no_warnings_logged()

    def test_stale_dependency_warned(self, mock_rglob: Mock, mock_get: Mock):
        """Test that a pin whose newest release is old is warned about, without being changed."""
        requirements_txt = self.requirements_file("humanize==4.15.0\n")
        mock_rglob.return_value = [requirements_txt]
        mock_get.side_effect = self.stale_pypi("4.15.0")  # No newer version; newest release is old.
        assert_success(update_requirements_txts())
        requirements_txt.write_text.assert_not_called()
        self.assert_stale_dependency_logged(requirements_txt, "humanize", "4.15.0")

    def test_recent_dependency_not_warned(self, mock_rglob: Mock, mock_get: Mock):
        """Test that a pin whose newest release is recent is not warned about as stale."""
        requirements_txt = self.requirements_file("humanize==4.15.0\n")
        mock_rglob.return_value = [requirements_txt]
        recent = datetime.now(UTC).isoformat()
        mock_get.side_effect = self.stale_pypi("4.15.0", upload_time=recent)
        assert_success(update_requirements_txts())
        self.assert_no_warnings_logged()

    def test_staleness_disabled(self, mock_rglob: Mock, mock_get: Mock):
        """Test that no staleness warning is emitted when the check is disabled with --stale-after 0."""
        requirements_txt = self.requirements_file("humanize==4.15.0\n")
        mock_rglob.return_value = [requirements_txt]
        mock_get.side_effect = self.stale_pypi("4.15.0")
        with patch.dict("os.environ", {STALE_AFTER_DAYS_ENV_VAR: "0"}):
            assert_success(update_requirements_txts())
        self.assert_no_warnings_logged()

    def test_change(self, mock_rglob: Mock, mock_get: Mock):
        """Test that an exact pin is bumped to the latest version."""
        requirements_txt = self.requirements_file("flask==1.0\n")
        mock_rglob.return_value = [requirements_txt]
        mock_get.side_effect = self.pypi("1.0", "1.1", bump=True)
        assert_success(update_requirements_txts())
        requirements_txt.write_text.assert_called_once_with("flask==1.1\n")
        self.assert_path_logged(requirements_txt)
        self.assert_new_version_logged(requirements_txt, "flask", PUBLISHED)
        self.assert_no_warnings_logged()

    def test_preserves_extras_marker_and_comment(self, mock_rglob: Mock, mock_get: Mock):
        """Test that extras, environment markers and inline comments are preserved when bumping the version."""
        requirements_txt = self.requirements_file('flask[async]==1.0 ; python_version < "3.12"  # keep\n')
        mock_rglob.return_value = [requirements_txt]
        mock_get.side_effect = self.pypi("1.0", "1.1", bump=True)
        assert_success(update_requirements_txts())
        requirements_txt.write_text.assert_called_once_with('flask[async]==1.1 ; python_version < "3.12"  # keep\n')
        self.assert_path_logged(requirements_txt)
        self.assert_new_version_logged(requirements_txt, "flask", PUBLISHED)
        self.assert_no_warnings_logged()

    def test_spaces_around_equals_preserved(self, mock_rglob: Mock, mock_get: Mock):
        """Test that spaces around `==` and the aligned inline comment are preserved when bumping the version."""
        requirements_txt = self.requirements_file("certifi == 2020.4.5.1          # used by requests\n")
        mock_rglob.return_value = [requirements_txt]
        mock_get.side_effect = self.pypi("2020.4.5.1", "2020.4.5.2", bump=True)
        assert_success(update_requirements_txts())
        requirements_txt.write_text.assert_called_once_with("certifi == 2020.4.5.2          # used by requests\n")
        self.assert_path_logged(requirements_txt)
        self.assert_new_version_logged(requirements_txt, "certifi", "2020.4.5.2, published: 2020-01-01 00:00")
        self.assert_no_warnings_logged()

    def test_loose_specifiers_untouched(self, mock_rglob: Mock, mock_get: Mock):
        """Test that non-exact specifiers, options, URLs and comments are left untouched and not queried."""
        contents = (
            "flask>=1.0\ndjango~=2.0\nrequests\n-e .\n--index-url https://example.com\ngit+https://x/y.git\n# c\n"
        )
        requirements_txt = self.requirements_file(contents)
        mock_rglob.return_value = [requirements_txt]
        assert_success(update_requirements_txts())
        requirements_txt.write_text.assert_not_called()
        mock_get.assert_not_called()
        self.assert_path_logged(requirements_txt)
        self.assert_no_new_version_logged()
        self.assert_no_warnings_logged()

    def test_held_back_by_cooldown(self, mock_rglob: Mock, mock_get: Mock):
        """Test that a newer version published within the cooldown period is not picked up."""
        requirements_txt = self.requirements_file("flask==1.0\n")
        mock_rglob.return_value = [requirements_txt]
        recent = datetime.now(UTC).isoformat()
        mock_get.side_effect = self.pypi("1.0", "1.1", bump=True, upload_time=recent)
        assert_success(update_requirements_txts())
        requirements_txt.write_text.assert_not_called()
        self.assert_path_logged(requirements_txt)
        self.assert_no_new_version_logged()
        self.assert_no_warnings_logged()

    def test_hash_pinned_file_skipped(self, mock_rglob: Mock, mock_get: Mock):
        """Test that a hash-pinned (fully locked) requirements file is skipped entirely."""
        requirements_txt = self.requirements_file("flask==1.0 \\\n    --hash=sha256:abc\n")
        mock_rglob.return_value = [requirements_txt]
        assert_success(update_requirements_txts())
        requirements_txt.write_text.assert_not_called()
        mock_get.assert_not_called()
        self.assert_skipped_logged(requirements_txt, "compiled or hash-pinned requirements file")
        self.assert_no_path_logged()
        self.assert_no_new_version_logged()
        self.assert_no_warnings_logged()

    def test_compiled_header_skipped(self, mock_rglob: Mock, mock_get: Mock):
        """Test that a pip-compile/uv generated requirements file is skipped entirely."""
        requirements_txt = self.requirements_file("# This file is autogenerated by pip-compile\nflask==1.0\n")
        mock_rglob.return_value = [requirements_txt]
        assert_success(update_requirements_txts())
        requirements_txt.write_text.assert_not_called()
        mock_get.assert_not_called()
        self.assert_skipped_logged(requirements_txt, "compiled or hash-pinned requirements file")
        self.assert_no_path_logged()
        self.assert_no_new_version_logged()
        self.assert_no_warnings_logged()

    def test_sibling_in_file_skipped(self, mock_rglob: Mock, mock_get: Mock):
        """Test that a requirements file with a sibling .in source is skipped entirely."""
        requirements_txt = self.requirements_file("flask==1.0\n", sibling_in=True)
        mock_rglob.return_value = [requirements_txt]
        assert_success(update_requirements_txts())
        requirements_txt.write_text.assert_not_called()
        mock_get.assert_not_called()
        self.assert_skipped_logged(requirements_txt, "compiled or hash-pinned requirements file")
        self.assert_no_path_logged()
        self.assert_no_new_version_logged()
        self.assert_no_warnings_logged()


class RequirementsGlobPatternsTest(unittest.TestCase):
    """Unit tests for which paths the requirements glob patterns match.

    `glob` uses `rglob`, which matches each path against the pattern with `PurePath.full_match`, so matching the
    patterns directly mirrors discovery without touching the file system.
    """

    def matches(self, path: str) -> bool:
        """Return whether any requirements glob pattern matches the path, case-sensitively (as `glob` matches)."""
        return any(PurePath(path).full_match(pattern, case_sensitive=True) for pattern in REQUIREMENTS_GLOB_PATTERNS)

    def test_recognized_flat_names(self):
        """Test that the flat requirements naming conventions match."""
        for name in ("requirements.txt", "requirements-dev.txt", "dev-requirements.txt"):
            self.assertTrue(self.matches(name), name)

    def test_nested_requirements_directory(self):
        """Test that a requirements file in a nested `requirements/` directory matches."""
        self.assertTrue(self.matches("requirements/base.txt"))

    def test_unrelated_txt_files_ignored(self):
        """Test that unrelated `.txt` files, a `requirements.in` source, and names without a hyphen do not match.

        The purpose must be hyphen-separated on both sides, so `requirementsfoo.txt` and `foorequirements.txt` are
        not treated as requirements files (only `requirements-foo.txt` / `foo-requirements.txt` are).
        """
        for name in ("notes.txt", "constraints.txt", "requirements.in", "requirementsfoo.txt", "foorequirements.txt"):
            self.assertFalse(self.matches(name), name)

    def test_case_sensitive(self):
        """Test that matching is case-sensitive, so a differently-cased name does not match."""
        self.assertFalse(self.matches("Requirements.txt"))
