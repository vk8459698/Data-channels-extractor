"""Split an ADRE Sxp Tabular List export into one file per channel.

    python main.py static.csv
    python main.py static.csv -o out_folder
    python main.py export1.csv export2.csv
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from adre_split import NotATabularExport, split_export


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="main.py",
        description=(
            "Split an ADRE Sxp Tabular List export into one file per channel, "
            "with the transducer configuration listed once."
        ),
    )
    parser.add_argument(
        "exports",
        nargs="+",
        metavar="CSV",
        help="the static Tabular List export(s) written by ADRE Sxp",
    )
    parser.add_argument(
        "-o",
        "--out",
        metavar="DIR",
        help="where to write the per-channel files (default: a 'channels' folder beside the input)",
    )
    parser.add_argument(
        "--keep-empty-columns",
        action="store_true",
        help="keep columns a channel never fills, such as Process Variable on a proximity probe",
    )
    parser.add_argument(
        "--encoding",
        default="utf-16",
        help="output encoding (default: utf-16, matching the ADRE exports)",
    )
    args = parser.parse_args(argv)

    exit_code = 0
    for raw in args.exports:
        path = Path(raw)
        if not path.is_file():
            print(f"{path}: no such file", file=sys.stderr)
            exit_code = 2
            continue
        try:
            result = split_export(
                path,
                args.out,
                encoding=args.encoding,
                drop_empty_columns=not args.keep_empty_columns,
            )
        except (NotATabularExport, OSError) as exc:
            print(f"{path.name}: {exc}", file=sys.stderr)
            exit_code = 2
            continue
        print(result.format_text(), flush=True)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
