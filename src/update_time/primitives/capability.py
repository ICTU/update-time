"""Whether a function can answer a question, recorded on the function itself.

A function that can answer is registered as having the capability, and a caller reads that back for the subject it is
about to ask about. Where the function answers for some subjects only, it registers a predicate that tells those
subjects from the rest. Each question names its own capability, in the module that asks it. The registration rides on
the function itself rather than in a table of its own, so a function built per call site is registered as it is built.
"""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable
    from typing import Any, Protocol

    # The subject is `Any` rather than `object`, so a predicate that admits one kind of subject is accepted.
    type _Answers = Callable[[Any], bool]

    class _Register(Protocol):
        """Registers a function as having the capability, returning it so it can be applied as a decorator."""

        def __call__[Function](self, function: Function, when: _Answers = ...) -> Function: ...

    class _HasCapability(Protocol):
        """Tells whether a function was registered as having the capability for a subject."""

        def __call__(self, function: object, subject: object) -> bool: ...


def _every_subject(_subject: object) -> bool:
    """Return whether the function answers for the subject, which it does whichever one it is."""
    return True


def capability(capability_name: str) -> tuple[_Register, _HasCapability]:
    """Return two functions: one registers a function as having this capability, the other tells whether it has it.

    Only those two know the attribute they use, and each capability names its own, so two cannot collide.
    """

    def register[Function](function: Function, when: _Answers = _every_subject) -> Function:
        """Register the function as having the capability for the subjects `when` admits, and return it.

        `when` defaults to every subject, so a function that always answers registers by applying this as a plain
        decorator.
        """
        setattr(function, capability_name, when)
        return function

    def has_capability(function: object, subject: object) -> bool:
        """Return whether the function was registered as having the capability for this subject."""
        answers: _Answers | None = getattr(function, capability_name, None)
        return answers is not None and answers(subject)

    return register, has_capability
