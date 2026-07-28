"""Exit code constants (design.md §3, requirements.md M4).

Priority when multiple conditions could apply within one run:
    ARG_ERROR(2) > AUTH_REQUIRED(4) > ENV_OR_API_ERROR(5) > PARTIAL_FAILURE(6)
    > NO_TARGETS(3) > OK(0)

ARG_ERROR is the only code for which no JSON is written to stdout (argparse
itself has already failed before any business logic / JSON assembly runs).
All other codes always emit exactly one JSON document to stdout; anything
for humans goes to stderr.
"""

ARG_ERROR = 2
AUTH_REQUIRED = 4
ENV_OR_API_ERROR = 5
PARTIAL_FAILURE = 6
NO_TARGETS = 3
OK = 0

# Highest priority first -- used by callers that need to combine several
# candidate codes into the single code for the process.
PRIORITY_ORDER = (
    ARG_ERROR,
    AUTH_REQUIRED,
    ENV_OR_API_ERROR,
    PARTIAL_FAILURE,
    NO_TARGETS,
    OK,
)


def combine(*codes: int) -> int:
    """Pick the highest-priority exit code among ``codes``.

    Unknown codes are treated as lower priority than every known code.
    """
    present = [c for c in codes if c in PRIORITY_ORDER]
    if not present:
        return OK
    return min(present, key=PRIORITY_ORDER.index)
