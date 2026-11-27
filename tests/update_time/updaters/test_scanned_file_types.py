"""Unit tests for the file type each updater script scans."""

import unittest
from typing import TYPE_CHECKING, Final
from unittest.mock import patch

from mutation_time.mutation import kills
from update_time.domain.file_type import (
    CIRCLE_CI_CONFIGS,
    DEVCONTAINER_CONFIGS,
    DOCKERFILES,
    GITHUB_WORKFLOWS,
    GITLAB_CI_CONFIG,
    INLINE_SCRIPT_METADATA,
    PACKAGE_JSON,
    POM_XML,
    PRE_COMMIT_CONFIG,
    PYPROJECT_TOML,
    PYTHON_VERSION_FILE,
    REQUIREMENTS_TXT,
    SPHINX_CONFIG,
)
from update_time.updaters import (
    update_circle_ci_config,
    update_devcontainer,
    update_dockerfile_base_image,
    update_github_action,
    update_gitlab_ci_config,
    update_jsdelivr,
    update_node_engine,
    update_pom_xml,
    update_pre_commit_config,
    update_pyproject_toml,
    update_python_inline_script_metadata,
    update_python_version_file,
    update_requirements_txt,
)

if TYPE_CHECKING:
    from types import ModuleType

    from update_time.domain.file_type import FileType

# The file type each updater scans, and the function that scans it. `_update_manifest_images` is left out: it scans
# two file types, so `ScannedManifestsTest` pins both of them. So is `update_package_json`, whose tests find each file
# by its pattern.
_SCANS: Final[tuple[tuple[ModuleType, str, FileType], ...]] = (
    (update_circle_ci_config, "_update_circle_ci_config", CIRCLE_CI_CONFIGS),
    (update_devcontainer, "_update_devcontainers", DEVCONTAINER_CONFIGS),
    (update_dockerfile_base_image, "_update_dockerfiles", DOCKERFILES),
    (update_github_action, "_update_github_actions", GITHUB_WORKFLOWS),
    (update_gitlab_ci_config, "_update_gitlab_ci_config", GITLAB_CI_CONFIG),
    (update_jsdelivr, "_update_jsdelivrs", SPHINX_CONFIG),
    (update_node_engine, "_update_node_engines", PACKAGE_JSON),
    (update_pom_xml, "_update_pom_xmls", POM_XML),
    (update_pre_commit_config, "_update_pre_commit_configs", PRE_COMMIT_CONFIG),
    (update_pyproject_toml, "_update_pyproject_tomls", PYPROJECT_TOML),
    (update_python_inline_script_metadata, "_update_python_inline_script_metadatas", INLINE_SCRIPT_METADATA),
    (update_python_version_file, "_update_python_version_files", PYTHON_VERSION_FILE),
    (update_requirements_txt, "_update_requirements_txts", REQUIREMENTS_TXT),
)


def _script(module: ModuleType) -> str:
    """Return the name of the updater script the module holds."""
    return module.__name__.rpartition(".")[2]


class ScannedFileTypesTest(unittest.TestCase):
    """Unit tests for which file type each updater script scans."""

    # Aliasing the import swaps the file type without changing its shape, so no other test can notice. The script
    # walking pyproject.toml files gets the pom.xml files instead.
    @kills.each(
        _SCANS,
        lambda scan: kills.append(
            scan[0],
            "from update_time.domain.file_type import ",
            "POM_XML as " if scan[2] is PYPROJECT_TOML else "PYPROJECT_TOML as ",
            "a script walks another file type than the one whose files it updates",
            case=_script(scan[0]),
        ),
    )
    def test_each_script_scans_its_own_file_type(self):
        """Test that each updater walks the file type declared for the files it updates."""
        self.assertNotEqual(_SCANS, ())  # An empty table would pass the assertion below without examining anything
        for module, function, expected in _SCANS:
            with self.subTest(script=_script(module)):
                with patch(f"{module.__name__}.{_walker(module)}", return_value=[]) as walk:
                    getattr(module, function)()
                walk.assert_called_once()
                self.assertEqual(walk.call_args.args, (expected,))


def _walker(module: ModuleType) -> str:
    """Return the name the module hands its file type to, so the test patches the one that module uses."""
    # The names an updater hands its file type to: `glob_for` where it walks the files itself, `update_yaml_files`
    # where the walk is done for it because each file is parsed before its references are rewritten.
    walkers = ("glob_for", "update_yaml_files")
    return next(name for name in walkers if hasattr(module, name))
