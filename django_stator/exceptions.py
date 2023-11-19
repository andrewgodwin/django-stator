class TryAgainLater(BaseException):
    """
    Special exception that Stator will catch without error,
    leaving a state to have another attempt soon.

    Equivalent to the state transition check function returning None; this
    just allows it to be more easily done from inner calls.
    """


class TimeoutError(BaseException):
    """
    Raised in threads to kill them when they time out
    """

    pass
