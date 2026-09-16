"""Unit tests for the file type each updater script scans."""

import unittest
from typing import TYPE_CHECKING
from unittest.mock import patch

from update_time.domain import file_type
from update_time.updaters import (
    update_circle_ci_config,
    update_devcontainer,
    update_dockerfile_base_image,
    update_github_action,
    update_gitlab_ci_config,
    update_jsdelivr,
    update_node_engine,
    update_package_json,
    update_pre_commit_config,
    update_pyproject_toml,
    update_python_inline_script_metadata,
    update_python_version_file,
    update_requirements_txt,
)

from tests.mutation import Mutation, kills

if TYPE_CHECKING:
    from types import ModuleType

# The file type each updater scans, and the function that scans it. Only `update_manifest_images` is left out: it
# scans two file types, so `ScannedManifestsTest` pins both of them.
_SCANS = (
    (update_circle_ci_config, "update_circle_ci_config", file_type.CIRCLE_CI_CONFIGS),
    (update_devcontainer, "update_devcontainers", file_type.DEVCONTAINER_CONFIGS),
    (update_dockerfile_base_image, "update_dockerfiles", file_type.DOCKERFILES),
    (update_github_action, "update_github_actions", file_type.GITHUB_WORKFLOWS),
    (update_gitlab_ci_config, "update_gitlab_ci_config", file_type.GITLAB_CI_CONFIG),
    (update_jsdelivr, "update_jsdelivrs", file_type.SPHINX_CONFIG),
    (update_node_engine, "update_node_engines", file_type.PACKAGE_JSON),
    (update_package_json, "update_package_jsons", file_type.PACKAGE_JSON),
    (update_pre_commit_config, "update_pre_commit_configs", file_type.PRE_COMMIT_CONFIG),
    (update_pyproject_toml, "update_pyproject_tomls", file_type.PYPROJECT_TOML),
    (update_python_inline_script_metadata, "update_python_inline_script_metadatas", file_type.INLINE_SCRIPT_METADATA),
    (update_python_version_file, "update_python_version_files", file_type.PYTHON_VERSION_FILE),
    (update_requirements_txt, "update_requirements_txts", file_type.REQUIREMENTS_TXT),
)


# The names an updater hands its file type to: `glob_for` where it walks the files itself, `update_yaml_files`
# where the walk is done for it because each file is parsed before its references are rewritten.
_WALKERS = ("glob_for", "update_yaml_files")


def _walker(module: ModuleType) -> str:
    """Return the name the module hands its file type to, so the test patches the one that module uses."""
    return next(name for name in _WALKERS if hasattr(module, name))


class ScannedFileTypesTest(unittest.TestCase):
    """Unit tests for which file type each updater script scans."""

    # Aliasing the import swaps the file type without changing its shape, so no other test can notice.
    @kills(
        Mutation(
            update_package_json,
            "from update_time.domain.file_type import PACKAGE_JSON",
            "from update_time.domain.file_type import PYPROJECT_TOML as PACKAGE_JSON",
            "a script walks another file type than the one whose files it updates",
        )
    )
    def test_each_script_scans_its_own_file_type(self):
        """Test that each updater walks the file type declared for the files it updates."""
        self.assertNotEqual(_SCANS, ())  # An empty table would pass the assertion below without examining anything
        for module, function, expected in _SCANS:
            with self.subTest(script=module.__name__.rpartition(".")[2]):
                with patch(f"{module.__name__}.{_walker(module)}", return_value=[]) as walk:
                    getattr(module, function)()
                walk.assert_called_once()
                self.assertEqual(walk.call_args.args, (expected,))
