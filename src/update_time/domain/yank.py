"""Which sources can observe that a version was yanked.

A yank — a release the maintainer withdrew — is only visible in some of the registries Update-time resolves versions
through: PyPI reports it as PEP 592 yank metadata, while an OCI registry and GitHub have no yank concept at all, so
the versions they resolve are never yanked whatever they answer. A source whose versions can carry a yank state
registers its new-version getter with `yank_reporting`, and `reports_yanks` reads the fact back, so an
`ignore[yanked]` marker on a reference resolved through any other source can be reported as redundant instead of
silently holding nothing back. Only the getters that resolve a marked reference take part.
"""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from update_time.domain.bound import NewVersionGetter

# The attribute `yank_reporting` marks a getter with and `reports_yanks` reads back. It rides on the getter itself
# rather than on a registry, so a source that hands out a getter per reference marks each one it builds.
_REPORTS_YANKS = "reports_yanks"


def yank_reporting(get_new_version: NewVersionGetter) -> NewVersionGetter:
    """Mark the source's new-version getter as one whose versions can carry a yank state, and return it.

    Applied as a decorator on the getter, so each source states the fact once, next to the code that observes the
    yank, rather than every updater repeating it where it wires the source up.
    """
    setattr(get_new_version, _REPORTS_YANKS, True)
    return get_new_version


def reports_yanks(get_new_version: NewVersionGetter) -> bool:
    """Return whether the versions the getter resolves can carry a yank state."""
    return getattr(get_new_version, _REPORTS_YANKS, False)
