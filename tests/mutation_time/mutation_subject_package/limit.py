"""Hold a limit, in a module that imports nothing from its package and that its package does not import."""


class Limit:
    """A limit whose value is a class attribute, so a mutation of the value executes this module afresh."""

    value = 10
