"""Exception hierarchy mapped 1:1 onto exit codes (M4).

Command implementations should raise the most specific of these instead of
calling sys.exit directly, so vodp.cli.main can uniformly:
  * emit the JSON contract (M8) to stdout,
  * emit the human message to stderr,
  * exit with ``exit_code``.
"""

from vodp.core import exitcodes


class VodpError(Exception):
    """Base class for all vodp-controlled failures."""

    exit_code = exitcodes.ENV_OR_API_ERROR

    def __init__(self, message: str, *, reason_code: str | None = None):
        super().__init__(message)
        self.message = message
        self.reason_code = reason_code


class ArgError(VodpError):
    """Bad CLI input discovered after argparse (e.g. cross-field checks)."""

    exit_code = exitcodes.ARG_ERROR


class AuthRequiredError(VodpError):
    """Interactive authentication is required but not permitted/possible."""

    exit_code = exitcodes.AUTH_REQUIRED


class EnvironmentError_(VodpError):
    """Environment is unusable: missing files, tools, or API/read failures."""

    exit_code = exitcodes.ENV_OR_API_ERROR


class ApiError(EnvironmentError_):
    """A remote API call failed or returned an incomplete/ambiguous result."""


class LedgerError(EnvironmentError_):
    """Ledger is corrupt, blocked, or in an unresolved recovery state."""


class PartialFailureError(VodpError):
    """One or more videos failed while others may have succeeded (exit 6)."""

    exit_code = exitcodes.PARTIAL_FAILURE
