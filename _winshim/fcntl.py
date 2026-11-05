"""Stub for the POSIX-only fcntl module so HA imports work on Windows.

Home Assistant imports ``fcntl`` at module level, but only uses it when the
real runner starts, which tests never do. Put this directory on ``PYTHONPATH``
when running pytest on Windows; on Linux CI this package is never imported.
"""

F_GETFD = 1
F_SETFD = 2
FD_CLOEXEC = 1

LOCK_SH = 1
LOCK_EX = 2
LOCK_NB = 4
LOCK_UN = 8


def fcntl(fd, cmd, arg=0):  # noqa: ARG001 - stub
    """Not available on Windows."""
    msg = "fcntl is not available on Windows"
    raise NotImplementedError(msg)


def flock(fd, cmd):  # noqa: ARG001 - stub
    """Not available on Windows."""
    msg = "flock is not available on Windows"
    raise NotImplementedError(msg)


def lockf(fd, cmd, length=0, start=0, whence=0):  # noqa: ARG001 - stub
    """Not available on Windows."""
    msg = "lockf is not available on Windows"
    raise NotImplementedError(msg)
