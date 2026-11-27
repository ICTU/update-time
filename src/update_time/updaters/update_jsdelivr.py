"""jsDelivr updater bumps the version in a Sphinx config's jsDelivr URLs, and their integrity hash along with it.

A reference spans two lines: the URL, and below it the attribute dictionary declaring the Subresource Integrity hash
that has to stay in step with the version.
"""

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Final

from update_time.domain.file_type import SPHINX_CONFIG
from update_time.domain.reference import ResolvedReference, hash_drifted
from update_time.io.file import rewrite_file
from update_time.io.filesystem import glob_for
from update_time.io.log import get_logger
from update_time.manifests.sphinx_config import ATTRIBUTES, URL_REFERENCE, spell_integrity
from update_time.primitives.text import rewrite_string
from update_time.references.resolve import latest_version
from update_time.references.rewrite import apply_marker
from update_time.references.vulnerability import warn_about_vulnerable_references
from update_time.sources.jsdelivr import integrity_hash, version_getter
from update_time.sources.npmjs import SOURCE

if TYPE_CHECKING:
    import re

    from update_time.domain.reference import Reference
    from update_time.io.log import Logger
    from update_time.primitives.line import Line

_LOG: Final[Logger] = get_logger("jsdelivr")


@dataclass(frozen=True, kw_only=True, slots=True)
class _ResolvedURL(ResolvedReference):
    """A jsDelivr URL whose version has been resolved, waiting for the attribute dictionary that carries its hash."""

    filename: str

    def update_attributes(self, match: re.Match[str]) -> str:
        """Return the attribute dictionary's line, carrying the hash of the version the URL above resolved to."""
        return self._refresh(match) if match.group("sha") else self._pin(match)

    def _refresh(self, match: re.Match[str]) -> str:
        """Return the dictionary with its declared hash rewritten to the resolved version's, or left as it is."""
        if self.version_moved:
            return rewrite_string(match, {"sha": self.version.sha})
        self._warn_if_hash_mismatches(match.group("sha"))
        return match.string

    @property
    def version_moved(self) -> bool:
        """Return whether the version resolved for the URL differs from the one the URL records."""
        return self.version.version != self.current_version

    def _warn_if_hash_mismatches(self, declared_hash: str) -> None:
        """Warn when the hash the dictionary declares differs from the one jsDelivr serves for the URL's version.

        A hash that can't be resolved (an unreachable API, a file jsDelivr doesn't list) leaves nothing to compare, so
        it stays silent and the failed request reports itself.
        """
        served_hash = integrity_hash(self.dependency, self.current_version, self.filename)
        if hash_drifted(served_hash, declared_hash):
            _LOG.hash_mismatch(self, declared_hash, served_hash)

    def _pin(self, match: re.Match[str]) -> str:
        """Return the dictionary with the resolved version's hash inserted, pinning a URL that declared none.

        Such a URL loads whatever the CDN serves. The pin is only reported when the version stayed put:
        when it moved, the new version is the change to report and the pin travels with it.
        """
        sha = self.version.sha or integrity_hash(self.dependency, self.version.version, self.filename)
        if not self.version_moved:
            _LOG.pinned(self, replace(self.version, sha=sha))
        return rewrite_string(match, {"open": spell_integrity(sha)})


class _Rewriter:
    """Rewrites the jsDelivr references in one Sphinx config, walking its lines."""

    def __init__(self) -> None:
        """Start the walk without a resolved URL waiting for its dictionary."""
        self._resolved: _ResolvedURL | None = None

    def updated_lines(self, lines: list[Line]) -> list[str]:
        """Return the config's lines with every jsDelivr URL and its integrity hash updated, honouring markers.

        An attribute dictionary is only visited while a resolved URL is waiting for it, so the dictionary of a
        reference that was held back is left alone.
        """
        result = []
        for line in lines:
            if url_match := URL_REFERENCE.search(line.text):
                self._end_entry()
                result.append(apply_marker(line, url_match, self.update_url, _LOG))
            elif (resolved := self._resolved) and (attributes_match := ATTRIBUTES.search(line.text)):
                self._resolved = None
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
        if self._resolved is not None:
            _LOG.cannot_pin(self._resolved)
            self._resolved = None

    def update_url(self, match: re.Match[str], reference: Reference) -> str:
        """Return the URL's line bumped to the latest version, or unchanged when there is no new version."""
        filename = match.group("filename")
        source = replace(SOURCE, new_version=version_getter(filename), adopts_hash_drift=False)
        latest = latest_version(reference, source, _LOG)
        if latest is None:
            return match.string
        self._resolved = _ResolvedURL.from_reference(reference, version=latest, filename=filename)
        if not self._resolved.version_moved:
            return match.string
        _LOG.new_version(reference, latest)
        return rewrite_string(match, {"version": latest.version})


def _update_jsdelivrs() -> None:
    """Find the Sphinx config files under docs/ and update the jsDelivr URLs in them."""
    for sphinx_config_py in glob_for(SPHINX_CONFIG):
        lines = rewrite_file(sphinx_config_py, _Rewriter().updated_lines, _LOG)
        warn_about_vulnerable_references(lines, URL_REFERENCE, SOURCE, _LOG)


if __name__ == "__main__":
    _update_jsdelivrs()
