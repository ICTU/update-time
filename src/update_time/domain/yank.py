"""Which sources can observe that a version was yanked.

A yank — a release the maintainer withdrew — is only visible in some of the registries Update-time resolves versions
through: PyPI reports it as PEP 592 yank metadata, while an OCI registry and GitHub have no yank concept at all, so
the versions they resolve are never yanked whatever they answer. A source whose versions can carry a yank state
registers its new-version getter with `yank_reporting`, and `reports_yanks` reads the fact back, so an
`ignore[yanked]` marker on a reference resolved through any other source can be reported as redundant instead of
silently holding nothing back. Only the getters that resolve a marked reference take part. npm reports a per-version
deprecation, which the jsDelivr source warns about as a yank, but a jsDelivr URL is rewritten whole-file and carries
no marker, so it has no yank scope to report on.
"""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from update_time.domain.bound import NewVersionGetter

# The new-version getters registered by `yank_reporting`: those whose versions can carry a yank state.
_YANK_REPORTING_GETTERS: set[NewVersionGetter] = set()


def yank_reporting(get_new_version: NewVersionGetter) -> NewVersionGetter:
    """Register the source's new-version getter as one whose versions can carry a yank state, and return it.

    Applied as a decorator on the getter, so each source states the fact once, next to the code that observes the
    yank, rather than every updater repeating it where it wires the source up.
    """
    _YANK_REPORTING_GETTERS.add(get_new_version)
    return get_new_version


def reports_yanks(get_new_version: NewVersionGetter) -> bool:
    """Return whether the versions the getter resolves can carry a yank state."""
    return get_new_version in _YANK_REPORTING_GETTERS
