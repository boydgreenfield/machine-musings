"""``python -m coverset_model``: validate a bundle or write the JSON Schemas, without coverset.

python -m coverset_model validate PATH     # print problems; exit 1 when there is any
python -m coverset_model schemas DIR       # write DIR/<file>.schema.json
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from coverset_model.io import validate
from coverset_model.schema import write_all


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m coverset_model", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    check = sub.add_parser("validate", help="print every problem with a neutral bundle")
    check.add_argument("path", type=Path)
    schemas = sub.add_parser("schemas", help="write the JSON Schemas of the bundle files")
    schemas.add_argument("directory", type=Path)
    args = parser.parse_args(argv)
    if args.command == "schemas":
        for path in write_all(args.directory):
            print(path)
        return 0
    problems = validate(args.path)
    for problem in problems:
        print(problem)
    if not problems:
        print(f"{args.path}: valid")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
