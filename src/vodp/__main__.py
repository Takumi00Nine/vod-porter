"""Enables ``python -m vodp ...`` in addition to the installed ``vodp`` console script."""

import sys

from vodp.cli.main import main

if __name__ == "__main__":
    sys.exit(main())
