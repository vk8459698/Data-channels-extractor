"""Extract ADRE data to per-channel CSVs, grouped for the website.

    python main.py static.csv
    python main.py job.zip
    python main.py job.zip --adre          # ADRE Sxp database zip, Windows + ADRE
    python main.py static.csv -o out_folder
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from pipeline import PipelineError, process, resolve_user_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="main.py",
        description=(
            "Turn an ADRE Sxp Tabular List export (or a zip of one, or an ADRE "
            "database zip) into one CSV per channel, grouped for website upload."
        ),
    )
    parser.add_argument(
        "inputs",
        nargs="+",
        metavar="PATH",
        help="static CSV, zip of CSVs, ADRE database zip, or unzipped database folder",
    )
    parser.add_argument(
        "-o",
        "--out",
        metavar="DIR",
        help="where to write channels/ and groups/ (default: beside the input)",
    )
    parser.add_argument(
        "--adre",
        action="store_true",
        help="drive ADRE Sxp to export CSV from a native database zip (Windows, ADRE installed)",
    )
    parser.add_argument(
        "--keep-empty-columns",
        action="store_true",
        help="keep columns a channel never fills, such as Process Variable on a proximity probe",
    )
    parser.add_argument(
        "--keep-names",
        action="store_true",
        help=(
            "keep the plant tags as they are; by default a tag such as "
            "39VS21-1HD-LP3 is written as BRG1X, which is how orbit and shaft "
            "centreline plots find a bearing's two probes"
        ),
    )
    parser.add_argument(
        "--encoding",
        default="utf-16",
        help="output encoding (default: utf-16, matching the ADRE exports)",
    )
    args = parser.parse_args(argv)

    exit_code = 0
    for raw in args.inputs:
        path = resolve_user_path(raw)
        if not path.exists():
            print(f"{path}: no such file", file=sys.stderr)
            exit_code = 2
            continue
        try:
            results = process(
                path,
                args.out,
                encoding=args.encoding,
                drop_empty_columns=not args.keep_empty_columns,
                name_for_plots=not args.keep_names,
                run_adre=args.adre,
            )
        except (PipelineError, FileNotFoundError, OSError) as exc:
            print(f"{path.name}: {exc}", file=sys.stderr)
            exit_code = 2
            continue
        for result in results:
            print(result.format_text(), flush=True)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
