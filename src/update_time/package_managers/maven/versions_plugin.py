"""The versions plugin's goals, their options and rule sets, the pom settings winning over them, and what it moved."""

import re
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum
from itertools import starmap
from pathlib import Path
from typing import TYPE_CHECKING

from update_time.domain.bound import NO_BOUND
from update_time.domain.dependency import DependencyVersion
from update_time.domain.reference import Reference
from update_time.io.log import get_logger
from update_time.manifests import pom_xml as pom_xml_manifest
from update_time.markers.cooldown import cooldown_days
from update_time.markers.directive import Reason
from update_time.package_managers.maven.plugins import VERSIONS_ARTEFACT, VERSIONS_PLUGIN
from update_time.package_managers.maven.version_range import maven_range
from update_time.primitives.iterables import unique
from update_time.sources import maven_central

if TYPE_CHECKING:
    from collections.abc import Generator

    from update_time.domain.dependency import DependencyName, VersionString
    from update_time.formats.xml import XmlElement

_LOG = get_logger("pom.xml")

# Numbers separated by dots, such as the `2.4.0` a version starts with. The versions plugin reads the patterns built
# from it as Java regexes. Python's `\d` matches any Unicode digit and Java's matches 0 to 9 alone, so `[0-9]` keeps
# both reading these patterns the same, where `update_time.domain.dependency.VERSION_NUMBERS` would not.
_NUMBER = r"[0-9]+(\.[0-9]+)*"

# The qualifier of a pre-release. Maven reads an `a`, `b`, or `m` followed directly by a number as `alpha`, `beta`,
# or `milestone`: https://maven.apache.org/pom.html#version-order-specification. The number after a qualifier can have
# dots in it, such as the `180830.0359` of `2.4.0-b180830.0359`.
_PRE_RELEASE_QUALIFIER = rf"[-.](alpha|beta|milestone|rc|cr|m|pre|preview|[ab][0-9])[-.]?({_NUMBER})?"

# The versions no goal may adopt. The versions plugin reads every version but a snapshot as a release, so both of
# its goals adopt a pre-release otherwise. The pattern must match a whole version string.
_PRE_RELEASES = f"(?i).*{_PRE_RELEASE_QUALIFIER}"

# The suffix Maven marks a snapshot with.
_SNAPSHOT = "-SNAPSHOT"

# The date of a build, such as the `v20250814` of `9.4.58.v20250814`. The date can have the time after it, such as the
# `-1726` of `3.10.0.v20140604-1726`.
_BUILD_DATE = r"v[0-9]{8}(-[0-9]{4})?"

# What follows the number of a plain release: nothing, a qualifier Maven reads as the release itself, such as the
# `.Final` of `4.2.19.Final`, or the date of the build. A service pack, such as the `.SP2` of `5.1.1.SP2`, and a
# revision, such as the `-1` of the WebJar `3.3.7-1`, leave the release plain too.
_PLAIN_RELEASE_QUALIFIER = rf"(?i:[-.](ga|final|release|sp[0-9]*|{_BUILD_DATE})|-[0-9]+)?"


def _other_variants(*versions: VersionString) -> str:
    """Return the pattern matching every version on a variant none of the versions is on.

    The pattern is empty where any of the versions does not start with a number. Such a version does not name a variant.
    """
    if not all(re.match(_NUMBER, version) for version in versions):
        return ""
    same_variants = "|".join(unique(map(_variant_pattern, versions)))
    return f"(?!{_NUMBER}({same_variants})$).*"


def _variant_pattern(version: VersionString) -> str:
    """Return the pattern matching the variant of the version, such as the `-jdk5` of `1.18.14-jdk5`."""
    # A snapshot and a pre-release are on the variant of their release. A qualifier running on into a letter is the
    # start of a variant, such as the `-m` of `-mysql`.
    release = version.removesuffix(_SNAPSHOT)
    variant = re.sub(rf"(?i)^{_NUMBER}({_PRE_RELEASE_QUALIFIER}(?![a-z]))?", "", release)
    # `re.escape` leaves letters, digits, and underscores alone, and Java reads a backslash before any other character
    # as that character itself, so the versions plugin reads the escaped variant literally.
    return _PLAIN_RELEASE_QUALIFIER if re.fullmatch(_PLAIN_RELEASE_QUALIFIER, variant) else re.escape(variant)


# The versions plugin's goals take these options. `generateBackupPoms` stops the versions plugin writing a backup pom
# beside the one it rewrites.
_VERSIONS_OPTIONS = ("-DgenerateBackupPoms=false", f"-Dmaven.version.ignore={_PRE_RELEASES}")

# `use-latest-releases` advances the version in a dependency's `<version>` element, `update-properties` the property a
# `<version>` element names.
VERSIONS_GOALS = (
    f"{VERSIONS_PLUGIN}:use-latest-releases",
    f"{VERSIONS_PLUGIN}:update-properties",
)

# The rule set the versions plugin reads, which holds a rule per artefact. The plugin filters releases neither by age
# nor by variant, so the rule set names the versions it may not adopt. Both of its goals honour it, beside the
# pre-release pattern above rather than instead of it.
_RULE_SET = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<ruleset xmlns="http://mojo.codehaus.org/versions-maven-plugin/rule/2.0.0">\n'
    "  <rules>\n{rules}  </rules>\n"
    "</ruleset>\n"
)


@contextmanager
def versions_options(declarations: list[pom_xml_manifest.Declaration]) -> Generator[tuple[str, ...]]:
    """Yield the options the versions plugin's goals take, naming a rule set where anything is held back.

    `update-properties` advances a property wherever in the reactor it is declared or named. So a run over a module
    advances its parent's properties, and a run over the parent advances the properties only its modules name. The
    options limit the goal to the properties the declarations name, which the rule set covers. With an empty list, the
    goal does not advance any property.
    """
    properties = unique(filter(None, (declaration.version_property for declaration in declarations)))
    options = (*_VERSIONS_OPTIONS, f"-DincludeProperties={','.join(properties)}")
    rules = _rules(declarations)
    if not rules:
        yield options
        return
    with _rule_set_file(_RULE_SET.format(rules=rules)) as path:
        yield (*options, f"-Dmaven.version.rules={path.as_uri()}")


class OverrideConsequence(StrEnum):
    """What Maven may do once a pom overrides an option of the versions plugin that Update-time sets or relies on."""

    RULES = (
        "Maven may adopt versions of another variant, versions inside the cooldown, and versions a marker holds back"
    )
    PRE_RELEASES = "Maven may adopt pre-releases"
    PROPERTIES = "Maven may advance properties that the rule set Update-time passes does not cover"
    BACKUP_POMS = "Maven leaves a pom.xml.versionsBackup beside each pom it rewrites"
    SNAPSHOTS = "Maven may advance a property to a snapshot"
    PARENT = "Maven may update the version of the parent pom, which neither the cooldown nor a marker holds back"


# The settings of the versions plugin's configuration that win over an option Update-time sets or relies on, and the
# consequence. A rules URI or a rule set replaces the rule set, ignored versions the pre-release pattern, and included
# properties the properties the declarations name. Generating backup poms, allowing snapshots, and processing the parent
# switch on what Update-time keeps off.
_OVERRIDING_SETTINGS = {
    "rulesUri": OverrideConsequence.RULES,
    "ruleSet": OverrideConsequence.RULES,
    "ignoredVersions": OverrideConsequence.PRE_RELEASES,
    "includeProperties": OverrideConsequence.PROPERTIES,
    "generateBackupPoms": OverrideConsequence.BACKUP_POMS,
    "allowSnapshots": OverrideConsequence.SNAPSHOTS,
    "processParent": OverrideConsequence.PARENT,
}

# An empty setting takes the value of the property Update-time passes. A rule set does not have a property, so an
# empty one is a rule set without rules, which still replaces the rule set Update-time passes.
_OVERRIDING_WHEN_EMPTY = ("ruleSet",)

# The settings that switch something on or off, which override the option only where they switch it on. Maven reads a
# switch as on where it holds `true`, in any case.
_SWITCHES = ("generateBackupPoms", "allowSnapshots", "processParent")


def _overrides_the_option(name: str, setting: pom_xml_manifest.PluginSetting) -> bool:
    """Return whether the setting of that name wins over Update-time."""
    if name in _SWITCHES:
        return setting.text.lower() == "true"
    return name in _OVERRIDING_WHEN_EMPTY or not setting.is_empty


@dataclass(frozen=True)
class _Override:
    """A setting of the pom's versions plugin configuration that wins over Update-time."""

    name: str
    consequence: OverrideConsequence
    setting: pom_xml_manifest.PluginSetting


def _overrides(effective_pom: XmlElement | None) -> list[_Override]:
    """Return the settings of the pom's versions plugin configuration that win over Update-time."""
    settings = {} if effective_pom is None else pom_xml_manifest.plugin_settings(effective_pom, VERSIONS_ARTEFACT)
    return [
        _Override(name, consequence, setting)
        for name, setting in settings.items()
        if (consequence := _OVERRIDING_SETTINGS.get(name)) and _overrides_the_option(name, setting)
    ]


def report_options_the_pom_overrides(pom_xml: Path, effective_pom: XmlElement | None) -> None:
    """Report each setting of the pom's versions plugin configuration that wins over Update-time."""
    for override in _overrides(effective_pom):
        set_at = override.setting.set_at
        _LOG.option_overridden_by_the_pom(pom_xml, override.name, override.consequence, set_at.pom_name, set_at.line)


def _rules(declarations: list[pom_xml_manifest.Declaration]) -> str:
    """Return a rule per artefact whose versions anything holds back, or nothing where nothing does.

    An artefact several declarations name gets one rule. The rule holds back what each of their markers holds back, and
    keeps the artefact on any of their variants.
    """
    declarations_by_artefact: dict[DependencyName, list[pom_xml_manifest.Declaration]] = {}
    for declaration in declarations:
        declarations_by_artefact.setdefault(declaration.dependency, []).append(declaration)
    return "".join(starmap(_artefact_rule, declarations_by_artefact.items()))


# The `ignoreVersion` matching every version, which holds an artefact's update back whatever the repository offers.
_EVERY_VERSION = '<ignoreVersion type="regex">.*</ignoreVersion>'


def _artefact_rule(artefact: DependencyName, declarations: list[pom_xml_manifest.Declaration]) -> str:
    """Return the rule holding back what the markers, the variants, and the cooldown do, or nothing where none does.

    A marker that holds every version back does not have a range, and leaves the repository unasked.
    """
    held_back_ranges = [held_back for declaration in declarations if (held_back := range_held_back(declaration)) != ""]
    if None in held_back_ranges:
        return _rule(artefact, _EVERY_VERSION)
    range_elements = (f'<ignoreVersion type="range">{held_back}</ignoreVersion>' for held_back in held_back_ranges)
    days = max(cooldown_days(declaration.marker) for declaration in declarations)
    ignored = unique((*range_elements, *_variant_versions(declarations), *_cooldown_versions(artefact, days)))
    return _rule(artefact, *ignored) if ignored else ""


def _variant_versions(declarations: list[pom_xml_manifest.Declaration]) -> tuple[str, ...]:
    """Return an `ignoreVersion` matching every version on a variant none of the declarations is on, if any."""
    other_variants = _other_variants(*(declaration.current_version for declaration in declarations))
    return (f'<ignoreVersion type="regex">{other_variants}</ignoreVersion>',) if other_variants else ()


def range_held_back(declaration: pom_xml_manifest.Declaration) -> str | None:
    """Return the Maven range of the versions the declaration's marker holds back, or None where it holds every one.

    The range is empty where the marker does not hold any version back. Where the rule set cannot express a bound,
    the marker holds every version back, so Maven never crosses the bound.
    """
    if declaration.marker.holds_the_update_back:
        return None
    bound = declaration.marker.version_bound
    return "" if bound == NO_BOUND else maven_range(bound, declaration.current_version)


def _cooldown_versions(artefact: DependencyName, days: int) -> tuple[str, ...]:
    """Return an `ignoreVersion` for each of the artefact's versions published inside the cooldown.

    An `ignoreVersion` without a `type` attribute matches a version exactly, so a version is never read as a pattern.
    The repository is not asked at all for a cooldown that holds nothing back.
    """
    if days <= 0:
        return ()
    return tuple(
        f"<ignoreVersion>{version}</ignoreVersion>" for version in maven_central.versions_held_back(artefact, days)
    )


def _rule(artefact: DependencyName, *ignore_versions: str) -> str:
    """Return the rule for the artefact, holding the given `ignoreVersion` elements."""
    group_id, artifact_id = pom_xml_manifest.coordinates(artefact)
    ignored = "".join(f"        {ignore_version}\n" for ignore_version in ignore_versions)
    return (
        f'    <rule groupId="{group_id}" artifactId="{artifact_id}">\n'
        f"      <ignoreVersions>\n{ignored}      </ignoreVersions>\n"
        "    </rule>\n"
    )


@contextmanager
def _rule_set_file(rule_set: str) -> Generator[Path]:
    """Write the rule set to a file for the Maven run, and remove the file once the run is over."""
    with tempfile.NamedTemporaryFile("w", suffix=".xml", delete_on_close=False) as file:
        file.write(rule_set)
        file.close()
        yield Path(file.name)


def names_rules_of_its_own(effective_pom: XmlElement | None) -> bool:
    """Return whether the pom's versions plugin configuration names rules that replace Update-time's rule set."""
    return any(override.consequence == OverrideConsequence.RULES for override in _overrides(effective_pom))


def reason_the_marker_steers_nothing(
    declaration: pom_xml_manifest.Declaration, *, pom_names_rules: bool
) -> Reason | None:
    """Return why the declaration's marker steers nothing of what Maven adopts, or None where it does."""
    if not declaration.maven_updates_the_version:
        return Reason.PLUGIN_NOT_UPDATED if declaration.literal_plugin_version else Reason.VERSION_HELD_ELSEWHERE
    return Reason.RULES_OF_THE_POM if pom_names_rules else None


@dataclass(frozen=True)
class _Update:
    """A declaration Maven updated, as the pom held it before and after the update and as the effective pom lists it."""

    old: pom_xml_manifest.Declaration
    new: pom_xml_manifest.Declaration
    effective: pom_xml_manifest.Declaration

    @property
    def reference(self) -> Reference:
        """Return the reference at the version it had, named as the effective pom names it."""
        return Reference(self.effective.dependency, self.old.current_version, self.new.location)

    @property
    def is_resolvable(self) -> bool:
        """Return whether the effective pom names the artefact whole, so a repository can be asked about it."""
        return pom_xml_manifest.is_resolved(self.effective.dependency)


def report_new_versions(
    before: list[pom_xml_manifest.Declaration],
    after: list[pom_xml_manifest.Declaration],
    resolved: list[pom_xml_manifest.Declaration],
) -> None:
    """Report each dependency and plugin whose version differs between the two readings, with its changes and date.

    The name is the one the effective pom gives the declaration, where it resolves one. Two declarations of one
    artefact naming one property read the same in each reading, so they are reported once. The new version of an
    artefact Maven Central does not serve goes unreported, where a served artefact names its property too.
    """
    updates = [
        _Update(old, new, effective)
        for old, new, effective in unique(zip(before, after, resolved, strict=True))
        if old.current_version != new.current_version
    ]
    unreportable = _unreportable_updates(updates)
    for update in updates:
        reference, new = update.reference, update.new
        if update in unreportable:
            _LOG.new_version_not_reported(reference, new.version_property, new.current_version)
            continue
        _LOG.new_version(reference, _new_version(update))


def _new_version(update: _Update) -> DependencyVersion:
    """Return the version Maven moved to, with its changes and publication date where the coordinates resolve."""
    artefact, version = update.effective.dependency, update.new.current_version
    if not pom_xml_manifest.fully_resolved(update.effective.pinned):
        return DependencyVersion(version)
    changes = maven_central.get_changes(artefact, version)
    return DependencyVersion(version, changes, publication=maven_central.publication(artefact, version))


def _unreportable_updates(updates: list[_Update]) -> list[_Update]:
    """Return the updates of an artefact Maven Central does not serve, whose property a served artefact names too.

    Maven moves a property to a version that every artefact naming it has, so the move says nothing about an artefact
    whose versions Maven does not find.
    """
    candidates = [update for update in updates if update.new.version_property and update.is_resolvable]
    artefacts = {update.effective.dependency for update in candidates}
    off_central = {artefact for artefact in artefacts if not maven_central.serves(artefact)}
    properties_on_central = {
        update.new.version_property for update in candidates if update.effective.dependency not in off_central
    }
    return [
        update
        for update in candidates
        if update.effective.dependency in off_central and update.new.version_property in properties_on_central
    ]
