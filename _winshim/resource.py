"""Stub for the POSIX-only resource module so HA imports work on Windows.

HA only calls into this module when the real runner starts the file-descriptor
limit bootstrap, which tests never trigger.
"""

RLIMIT_NOFILE = 7

RLIM_INFINITY = -1
RLIM_SAVED_MAX = -2
RLIM_SAVED_CUR = -3


def getrlimit(resource):  # noqa: ARG001 - stub
    """Pretend the soft and hard limits are already generous."""
    return (1024, 1024)


def setrlimit(resource, limits):  # noqa: ARG001 - stub
    """Nothing to do on Windows."""


def getrusage(who):  # noqa: ARG001 - stub
    """Not available on Windows."""
    msg = "getrusage is not available on Windows"
    raise NotImplementedError(msg)
