"""jsDelivr updater bumps the version in a Sphinx config's jsDelivr URLs, and their integrity hash along with it.

Limited to npm packages at the moment. A reference spans two lines: the URL, and below it the attribute dictionary
declaring the Subresource Integrity hash that has to stay in step with the version. The line the URL sits on resolves
the version and hands it down to the dictionary's line, which the pass reaches later. A dictionary that declares no
hash gains one there, which pins a URL that was loading whatever the CDN served; a URL declared without a dictionary
at all has nowhere to hold a hash, so it is reported rather than pinned. An `# update-time:` marker holds a URL back
or bounds it. A Sphinx config is Python, so the marker travels in a `#` comment, recognised both inline and
on the line directly above the URL.
"""

import re
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING

from update_time.domain.drift import hash_drifted
from update_time.io.filesystem import glob
from update_time.io.log import get_logger
from update_time.primitives.text import rewrite_string
from update_time.references.file import rewrite_file
from update_time.references.resolve import latest_version
from update_time.references.rewrite import apply_marker, matched_reference
from update_time.sources.jsdelivr import integrity_hash, version_getter

if TYPE_CHECKING:
    from update_time.domain.line import Line
    from update_time.domain.marker import Marker
    from update_time.domain.version import DependencyVersion, Reference
    from update_time.primitives.location import Location

_LOG = get_logger("jsdelivr")

# A jsDelivr npm URL. The file path after the version is captured so its (instead of the package default's) integrity
# hash is what gets updated.
_URL_RE = re.compile(r'https://cdn\.jsdelivr\.net/npm/(?P<dependency>[\w-]+)@(?P<version>[\d.]+)(?P<filename>/[^"]*)')

# The attribute dictionary a URL is accompanied by, and the Subresource Integrity hash it declares. The hash is
# optional, so a dictionary that declares none matches too; pinning it inserts the key in front of the entries it
# already has, which is what replacing `open` does.
_ATTRIBUTES_RE = re.compile(r'(?P<open>\{)(?:"integrity": "(?P<sha>sha\d+-[A-Za-z0-9+/=]+)", )?')


@dataclass(frozen=True)
class _ResolvedURL:
    """A jsDelivr URL whose version has been resolved, waiting for the attribute dictionary that carries its hash."""

    reference: Reference
    latest: DependencyVersion
    filename: str
    location: Location

    @property
    def version_moved(self) -> bool:
        """Return whether the version resolved for the URL differs from the one the URL records."""
        return self.latest.version != self.reference.current_version

    def update_attributes(self, match: re.Match[str]) -> str:
        """Return the attribute dictionary's line, carrying the hash of the version the URL above resolved to."""
        return self._refresh(match) if match.group("sha") else self._pin(match)

    def _pin(self, match: re.Match[str]) -> str:
        """Return the dictionary with the resolved version's hash inserted, pinning a URL that declared none.

        Such a URL loads whatever the CDN serves, so the hash goes in front of the entries the dictionary already
        has. It is the hash the version decision resolved, or, for a URL staying on its version, one looked up for
        that version. The pin is only reported when the version stayed put: when it moved, the new version is the
        change to report and the pin travels with it.
        """
        sha = self.latest.sha or integrity_hash(self.reference.dependency, self.latest.version, self.filename)
        if not self.version_moved:
            _LOG.pinned(self.reference.dependency, replace(self.latest, sha=sha), self.location)
        return rewrite_string(match, {"open": f'{{"integrity": "{sha}", '})

    def _refresh(self, match: re.Match[str]) -> str:
        """Return the dictionary with its declared hash rewritten to the resolved version's, or left as it is.

        A declared hash is only rewritten when the version moved. When the URL stays on its version, the declared
        hash is compared against the one jsDelivr serves instead (see `_warn_if_hash_mismatches`).
        """
        if self.version_moved:
            return rewrite_string(match, {"sha": self.latest.sha})
        self._warn_if_hash_mismatches(match.group("sha"))
        return match.string

    def _warn_if_hash_mismatches(self, declared_hash: str) -> None:
        """Warn when the hash the dictionary declares differs from the one jsDelivr serves for the URL's version.

        The declared hash is passed in rather than read from `reference`, because the URL's line carries no hash —
        only the dictionary's line does.
        Resolving the served hash costs one request, since a URL staying on its version never looked it up; a hash
        that can't be resolved (an unreachable API, a file jsDelivr doesn't list) leaves nothing to compare, so it
        stays silent and the failed request reports itself.
        """
        dependency, version = self.reference.dependency, self.reference.current_version
        served_hash = integrity_hash(dependency, version, self.filename)
        if hash_drifted(served_hash, declared_hash):
            _LOG.hash_mismatch(dependency, version, declared_hash, served_hash, self.location)


@dataclass
class _Rewriter:
    """Rewrites the jsDelivr references in one Sphinx config, walking its lines.

    A reference spans two lines, so the version resolved at the URL's line only reaches the attribute dictionary
    that carries its hash once the walk gets there. `resolved` is that hand-over, empty while no resolved URL is
    waiting for its dictionary.
    """

    resolved: _ResolvedURL | None = None

    def update_url(self, match: re.Match[str], location: Location, marker: Marker) -> str:
        """Return the URL's line bumped to the latest version, or unchanged when there is no new version.

        Which version to move to is `latest_version`'s decision, shared with every other reference kind, honouring
        the URL's `# update-time:` marker with its bound and hold-backs. Only the URL's own output is spelled here;
        the hash of the resolved version is left to the dictionary line, which the pass reaches next.
        """
        reference = matched_reference(match)
        filename = match.group("filename")
        latest = latest_version(reference, version_getter(filename), marker, location, _LOG)
        if latest is None:
            return match.string
        self.resolved = _ResolvedURL(reference, latest, filename, location)
        if not self.resolved.version_moved:
            return match.string
        _LOG.new_version(reference.dependency, latest, location)
        return rewrite_string(match, {"version": latest.version})

    def updated_lines(self, lines: list[Line]) -> list[str]:
        """Return the config's lines with every jsDelivr URL and its integrity hash updated, honouring markers.

        Each line that declares a URL is run through the shared `apply_marker` gate, which reads the URL's marker to
        hold it back or bound it, and hands off to `update_url` for the rewrite. An attribute dictionary is only
        visited while a resolved URL is waiting for it, so the dictionary of a reference that was held back is left
        alone.
        """
        result = []
        for line in lines:
            if url_match := _URL_RE.search(line.text):
                self._end_entry()
                result.append(apply_marker(line, url_match, self.update_url, _LOG))
            elif (resolved := self.resolved) and (attributes_match := _ATTRIBUTES_RE.search(line.text)):
                self.resolved = None
                result.append(resolved.update_attributes(attributes_match))
            else:
                result.append(line.text)
        self._end_entry()
        return result

    def _end_entry(self) -> None:
        """End the entry in progress, reporting a URL still waiting for the attribute dictionary it turns out to lack.

        An entry ends at the next URL and when the lines run out. The URL reported is forgotten with it, so the entry
        below never inherits a hash resolved for the one above.
        """
        if self.resolved is not None:
            _LOG.cannot_pin(self.resolved.reference.dependency, self.resolved.location)
            self.resolved = None


def update_jsdelivrs() -> None:
    """Find the Sphinx config files under docs/ and update the jsDelivr URLs in them."""
    for sphinx_config_py in glob("conf.py", start=Path.cwd() / "docs"):
        rewrite_file(sphinx_config_py, _Rewriter().updated_lines, _LOG)


def main() -> None:  # pragma: no cover
    """Update the jsDelivr URLs in the repository's Sphinx configuration."""
    update_jsdelivrs()


if __name__ == "__main__":  # pragma: no cover
    main()
