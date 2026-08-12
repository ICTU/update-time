"""Whether a new-version getter can answer a question about the versions it resolves.

A getter that can answer is registered as having the capability, and a caller reads that back for the dependency it
is about to resolve. Where the getter answers for some of its dependencies only, it registers a predicate that tells
those dependencies from the rest. Each question names its own capability, in the module that asks it. The
registration rides on the getter itself rather than in a table of its own, so a getter built per reference is
registered as it is built.
"""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

    from update_time.domain.bound import NewVersionGetter
    from update_time.domain.version import DependencyName

type _Answers = Callable[[DependencyName], bool]
type _Register = Callable[..., NewVersionGetter]
type _HasCapability = Callable[[NewVersionGetter, DependencyName], bool]


def _every_dependency(_dependency: DependencyName) -> bool:
    """Return whether the getter answers for the dependency, which it does whichever one it is."""
    return True


def capability(capability_name: str) -> tuple[_Register, _HasCapability]:
    """Return two functions: one registers a getter as having this capability, the other tells whether it has it.

    Only those two know the attribute they use, and each capability names its own, so two cannot collide.
    """

    def register(get_new_version: NewVersionGetter, when: _Answers = _every_dependency) -> NewVersionGetter:
        """Register the getter as having the capability for the dependencies `when` admits, and return it.

        `when` defaults to every dependency the getter resolves, so a source that always answers registers by
        applying this as a plain decorator.
        """
        setattr(get_new_version, capability_name, when)
        return get_new_version

    def has_capability(get_new_version: NewVersionGetter, dependency: DependencyName) -> bool:
        """Return whether the getter was registered as having the capability for this dependency."""
        answers: _Answers | None = getattr(get_new_version, capability_name, None)
        return answers is not None and answers(dependency)

    return register, has_capability
