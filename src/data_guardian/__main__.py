"""Allow ``python3 -m src.data_guardian`` to run the CLI."""

import sys

from src.data_guardian.main import main

if __name__ == "__main__":
    sys.exit(main())
