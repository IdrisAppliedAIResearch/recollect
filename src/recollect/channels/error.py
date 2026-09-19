"""The one error channels raise, kept in a leaf so any module can use it."""


class ChannelError(RuntimeError):
    """This send cannot happen, with a reason safe to record and show."""
