"""Fetch pinned legacy PHP/Java applications into ``corpus/`` (gitignored).

TODO: define a small registry of small, permissively licensed legacy apps with pinned commits;
implement ``--list`` and ``--fetch <name>`` (git clone or archive download), and refuse to
overwrite an existing checkout.
"""

from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> int:
    print("TODO: corpus fetch script (see corpus/README.md)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
