"""Update the dependencies that the PEP 723 inline script metadata of a .py file declares, through `uv tree`."""

from typing import TYPE_CHECKING

from update_time.domain.cooldown import COOLDOWN, cooldown_cutoff
from update_time.domain.dependency import normalized_python_name
from update_time.io.process import is_installed, run
from update_time.package_managers.requirements_compiler import by_package, log_new_version
from update_time.package_managers.uv.uv import EXCLUDE_NEWER_OPTION, report_file_markers
from update_time.primitives.command import Command

if TYPE_CHECKING:
    from update_time.domain.dependency import DependencyName, VersionString
    from update_time.io.log import Logger
    from update_time.manifests.inline_script import InlineScript
    from update_time.manifests.python_dependency_toml import Declaration


def _parse_line_with_update(line: str) -> tuple[DependencyName, VersionString]:
    """Parse the package name and latest version from a `uv tree --outdated` line, e.g. 'pkg v1.0 (latest: v1.1)'."""
    fields = line.split()
    return fields[0], fields[-1].lstrip("v").rstrip(")")


def _outdated(uv_tree: Command, file: InlineScript, log: Logger) -> str | None:
    """Return what `uv tree --outdated` reports for the file, or None after logging why uv reported nothing."""
    if not is_installed(uv_tree.executable):
        log.uv_not_on_path(file.path)
        return None
    outdated = run(uv_tree)
    if not outdated.ok:
        log.uv_dependencies_not_updated(file.path, uv_tree.name)
        return None
    return outdated.stdout


def _update_dependencies(uv_tree: Command, file: InlineScript, log: Logger) -> None:
    """Run `uv tree --outdated`, log every new version no marker holds back, and rewrite the file's exact `==` pins.

    Where `uv tree` reports nothing usable, such as for an unreachable registry, the dependencies are not updated.
    """
    log.path(file.path)
    declarations = by_package(report_file_markers(file, log))
    if (outdated := _outdated(uv_tree, file, log)) is None:
        return
    lines_with_updates = [line for line in outdated.splitlines() if " (latest: " in line]
    latest_versions: dict[Declaration, VersionString] = {}
    for line in lines_with_updates:
        package, version = _parse_line_with_update(line)
        declared = declarations.get(normalized_python_name(package), [])
        updatable = [declaration for declaration in declared if declaration.updatable]
        # Only an exact pin names a version in the file to rewrite; a looser declaration is left as it stands.
        latest_versions.update({reference: version for reference in updatable if reference.pins_a_version})
        # A package with no declaration left to report at, such as one the file declares nowhere, is not reported.
        if locations := [reference.location for reference in updatable]:
            log_new_version(log, package, version, locations)
    file.rewrite_pinned_versions(latest_versions)


def update_python_inline_script_metadata(script: InlineScript, log: Logger) -> None:
    """Update a .py file's PEP 723 inline `# /// script` dependencies to their latest versions.

    A script has no lockfile, so — unlike the pyproject.toml path, which persists the cooldown into `[tool.uv]` —
    the cooldown is passed to uv on the command line as an `--exclude-newer` cutoff. `--depth=0` limits the report
    to the script's own dependencies: for a script each is its own tree root, so a deeper depth would pull in
    transitive packages that aren't pinned in the block.
    """
    uv_tree = Command(
        "uv",
        "tree",
        "--script",
        str(script.path),
        "--quiet",
        "--depth=0",
        "--outdated",
        EXCLUDE_NEWER_OPTION,
        cooldown_cutoff(COOLDOWN.get()),
    )
    _update_dependencies(uv_tree, script, log)
