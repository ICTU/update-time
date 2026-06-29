"""Unit tests for the requirements.txt update script."""

from datetime import UTC, datetime
from unittest.mock import MagicMock, Mock, patch

from update_time.updaters.update_requirements_txt import update_requirements_txts

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
        """Return a mock requirements file, optionally with a sibling .in source file present."""
        requirements_txt = mock_path(contents)
        requirements_txt.stem = "requirements"
        requirements_txt.parent = MagicMock()
        requirements_txt.parent.__truediv__.return_value = Mock(exists=Mock(return_value=sibling_in))
        return requirements_txt

    def pypi(self, *versions: str, bump: bool = False, upload_time: str = OLD) -> list:
        """Return mock PyPI responses: the Index API versions, plus per-version metadata when a bump is expected."""
        responses = [mock_response({"versions": list(versions)})]
        if bump:
            info = {"description": "", "yanked": False}
            responses.append(mock_response({"info": info, "urls": [{"upload_time_iso_8601": upload_time}]}))
        return responses

    def test_no_change(self, mock_rglob: Mock, mock_get: Mock):
        """Test that a pin already on the latest version is left unchanged."""
        requirements_txt = self.requirements_file("flask==1.0\n")
        mock_rglob.return_value = [requirements_txt]
        mock_get.side_effect = self.pypi("1.0")
        assert_success(update_requirements_txts())
        requirements_txt.write_text.assert_not_called()
        self.assert_path_logged(requirements_txt.relative_to())
        self.assert_no_new_version_logged()
        self.assert_no_warnings_logged()

    def test_change(self, mock_rglob: Mock, mock_get: Mock):
        """Test that an exact pin is bumped to the latest version."""
        requirements_txt = self.requirements_file("flask==1.0\n")
        mock_rglob.return_value = [requirements_txt]
        mock_get.side_effect = self.pypi("1.0", "1.1", bump=True)
        assert_success(update_requirements_txts())
        requirements_txt.write_text.assert_called_once_with("flask==1.1\n")
        self.assert_path_logged(requirements_txt.relative_to())
        self.assert_new_version_logged("flask", PUBLISHED, path=requirements_txt.relative_to())
        self.assert_no_warnings_logged()

    def test_preserves_extras_marker_and_comment(self, mock_rglob: Mock, mock_get: Mock):
        """Test that extras, environment markers and inline comments are preserved when bumping the version."""
        requirements_txt = self.requirements_file('flask[async]==1.0 ; python_version < "3.12"  # keep\n')
        mock_rglob.return_value = [requirements_txt]
        mock_get.side_effect = self.pypi("1.0", "1.1", bump=True)
        assert_success(update_requirements_txts())
        requirements_txt.write_text.assert_called_once_with('flask[async]==1.1 ; python_version < "3.12"  # keep\n')
        self.assert_path_logged(requirements_txt.relative_to())
        self.assert_new_version_logged("flask", PUBLISHED, path=requirements_txt.relative_to())
        self.assert_no_warnings_logged()

    def test_spaces_around_equals_preserved(self, mock_rglob: Mock, mock_get: Mock):
        """Test that spaces around `==` and the aligned inline comment are preserved when bumping the version."""
        requirements_txt = self.requirements_file("certifi == 2020.4.5.1          # used by requests\n")
        mock_rglob.return_value = [requirements_txt]
        mock_get.side_effect = self.pypi("2020.4.5.1", "2020.4.5.2", bump=True)
        assert_success(update_requirements_txts())
        requirements_txt.write_text.assert_called_once_with("certifi == 2020.4.5.2          # used by requests\n")
        self.assert_path_logged(requirements_txt.relative_to())
        self.assert_new_version_logged(
            "certifi", "2020.4.5.2, published: 2020-01-01 00:00", path=requirements_txt.relative_to()
        )
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
        self.assert_path_logged(requirements_txt.relative_to())
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
        self.assert_path_logged(requirements_txt.relative_to())
        self.assert_no_new_version_logged()
        self.assert_no_warnings_logged()

    def test_hash_pinned_file_skipped(self, mock_rglob: Mock, mock_get: Mock):
        """Test that a hash-pinned (fully locked) requirements file is skipped entirely."""
        requirements_txt = self.requirements_file("flask==1.0 \\\n    --hash=sha256:abc\n")
        mock_rglob.return_value = [requirements_txt]
        assert_success(update_requirements_txts())
        requirements_txt.write_text.assert_not_called()
        mock_get.assert_not_called()
        self.assert_skipped_logged(requirements_txt.relative_to(), "compiled or hash-pinned requirements file")
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
        self.assert_skipped_logged(requirements_txt.relative_to(), "compiled or hash-pinned requirements file")
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
        self.assert_skipped_logged(requirements_txt.relative_to(), "compiled or hash-pinned requirements file")
        self.assert_no_path_logged()
        self.assert_no_new_version_logged()
        self.assert_no_warnings_logged()
