# SPDX-License-Identifier: BSD-3-Clause
"""The ``vledger`` command line.

The verbs the requirements ask for (recompute, validate, export, report)
are not built yet; this is the entry point they attach to.
"""

from __future__ import annotations

import argparse
import sys

from vledger import __version__


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="vledger", description=__doc__.splitlines()[0])
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser


def main(argv: list[str] | None = None) -> int:
    build_parser().parse_args(argv)
    print("vledger: no verbs yet — see the requirements register.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
