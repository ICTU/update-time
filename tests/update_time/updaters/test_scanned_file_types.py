"""Unit tests for the file type each updater script scans."""

import unittest
from unittest.mock import patch

from update_time.domain import file_type
from update_time.updaters import (
    update_circle_ci_config,
    update_github_action,
    update_gitlab_ci_config,
    update_jsdelivr,
    update_node_engine,
    update_package_json,
    update_pre_commit_config,
    update_pyproject_toml,
    update_python_inline_script_metadata,
    update_python_version_file,
)

from tests.mutation import Mutation, kills

# The file type each updater walks, and the function that walks it. The updaters that hand a file type to
# `update_files` rather than walking it themselves are covered by their own tests.
_SCANS = (
    (update_circle_ci_config, "update_circle_ci_config", file_type.CIRCLE_CI_CONFIGS),
    (update_github_action, "update_github_actions", file_type.GITHUB_WORKFLOWS),
    (update_gitlab_ci_config, "update_gitlab_ci_config", file_type.GITLAB_CI_CONFIG),
    (update_jsdelivr, "update_jsdelivrs", file_type.SPHINX_CONFIG),
    (update_node_engine, "update_node_engines", file_type.PACKAGE_JSON),
    (update_package_json, "update_package_jsons", file_type.PACKAGE_JSON),
    (update_pre_commit_config, "update_pre_commit_configs", file_type.PRE_COMMIT_CONFIG),
    (update_pyproject_toml, "update_pyproject_tomls", file_type.PYPROJECT_TOML),
    (update_python_inline_script_metadata, "update_python_inline_script_metadatas", file_type.INLINE_SCRIPT_METADATA),
    (update_python_version_file, "update_python_version_files", file_type.PYTHON_VERSION_FILE),
)


class ScannedFileTypesTest(unittest.TestCase):
    """Unit tests for which file type each updater script scans."""

    @kills(
        Mutation(
            update_node_engine,
            "    for path in glob_for(PACKAGE_JSON):",
            "    for path in glob(*PACKAGE_JSON.patterns):",
            "a script walks a file type's patterns while ignoring where and how its declaration says to walk them",
        )
    )
    def test_each_script_scans_its_own_file_type(self):
        """Test that each updater walks the file type declared for the files it updates."""
        self.assertNotEqual(_SCANS, ())  # An empty table would pass the assertion below without examining anything
        for module, function, expected in _SCANS:
            with self.subTest(script=module.__name__.rpartition(".")[2]):
                with patch(f"{module.__name__}.glob_for", return_value=[]) as glob_for:
                    getattr(module, function)()
                glob_for.assert_called_once_with(expected)
