"""Task success for T3: app/utils_old.py is gone and app/export_csv.py is still there.

Run as `python check_t3.py` with the repo root as the working directory.
"""

import sys
from pathlib import Path


def main() -> int:
    repo = Path.cwd()
    if (repo / "app" / "utils_old.py").exists():
        print("app/utils_old.py still exists")
        return 1
    if not (repo / "app" / "export_csv.py").is_file():
        print("app/export_csv.py was deleted but is still in use")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
