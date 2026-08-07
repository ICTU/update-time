"""Whether a source can answer a question about its versions: were they yanked, are they vulnerable.

A source that can answer registers its new-version getter, and Update-time reads that back before it trusts a
marker. Where the source behind a reference cannot answer, an `ignore[yanked]` or `ignore[vulnerable]` on that
reference silences nothing, so Update-time reports it as redundant rather than let it look effective. The
registration rides on the getter itself rather than in a table of its own, so a source that hands out a getter per
reference registers each one it builds.
"""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

    from update_time.domain.bound import NewVersionGetter

type _Reporting = Callable[[NewVersionGetter], NewVersionGetter]
type _Reports = Callable[[NewVersionGetter], bool]


def capability(attribute: str) -> tuple[_Reporting, _Reports]:
    """Return two functions: one registers a getter as answering this question, the other reads that back.

    Only those two know the attribute they use, and each capability names its own, so two cannot collide.
    """

    def reporting(get_new_version: NewVersionGetter) -> NewVersionGetter:
        """Register the getter as answering and return it, so a source can apply this as a decorator."""
        setattr(get_new_version, attribute, True)
        return get_new_version

    def reports(get_new_version: NewVersionGetter) -> bool:
        """Return whether the getter was registered as answering."""
        return getattr(get_new_version, attribute, False)

    return reporting, reports
