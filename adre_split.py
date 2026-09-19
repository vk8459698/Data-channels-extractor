"""Split an ADRE Sxp Tabular List export into one file per channel.

The Sxp "Tabular List" export packs every channel into a single pipe-delimited
UTF-16 file and repeats the whole transducer configuration block ahead of every
sample block.  A 21-channel, 35-sample export therefore carries 735
configuration rows where 21 would do, and tools that expect one table per file
cannot read it.

This module rewrites that dump as the per-channel, tab-delimited layout those
tools already accept: the configuration row appears once, followed by that
channel's samples.  Cell text is copied verbatim, including vendor status codes
such as ``280BMA``.

Standard library only - no third-party packages are required.
"""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass
from pathlib import Path

META_HEADER_PREFIX = "CH#|Channel Name|Machine Name"
SAMPLE_HEADER_PREFIX = "CH#|Channel Name|Sample#"

#: Encodings these exports show up in, most likely first.
ENCODINGS = ("utf-16", "utf-8-sig", "cp1252")

#: Never dropped, even when a channel leaves them blank.
KEEP_ALWAYS = ("CH#", "Channel Name", "Sample#", "Sample Cause", "Date")

_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


class NotATabularExport(ValueError):
    """The file is not an ADRE Sxp Tabular List export."""


@dataclass(frozen=True)
class SplitResult:
    """What :func:`split_export` wrote, and what it removed."""

    source: Path
    folder: Path
    channel_files: list[Path]
    config_file: Path
    n_channels: int
    n_samples: int
    config_rows_in: int
    config_rows_out: int

    @property
    def duplicate_config_rows(self) -> int:
        return self.config_rows_in - self.config_rows_out

    def format_text(self) -> str:
        return (
            f"{self.source.name}: {self.n_channels} channels, {self.n_samples} samples\n"
            f"  configuration rows {self.config_rows_in} -> {self.config_rows_out} "
            f"({self.duplicate_config_rows} duplicates removed)\n"
            f"  wrote {len(self.channel_files)} channel files + {self.config_file.name}\n"
            f"  folder {self.folder}"
        )


def decode_export(data: bytes) -> list[str]:
    """Decode raw export bytes, trying the encodings these files show up in."""
    last_error: Exception | None = None
    for encoding in ENCODINGS:
        try:
            text = data.decode(encoding)
        except (UnicodeDecodeError, UnicodeError) as exc:
            last_error = exc
            continue
        # A mis-guessed encoding shows up as interleaved NUL characters.
        if "\x00" in text:
            continue
        return text.splitlines()
    raise NotATabularExport(f"Could not decode the file: {last_error}")


def read_lines(path: str | Path) -> list[str]:
    return decode_export(Path(path).read_bytes())


def is_tabular_export(lines: list[str]) -> bool:
    return any(
        line.startswith(SAMPLE_HEADER_PREFIX) or line.startswith(META_HEADER_PREFIX)
        for line in lines[:80]
    )


def safe_filename(name: str) -> str:
    """A channel name that Windows will accept as a file name."""
    cleaned = _UNSAFE.sub("_", name).strip().rstrip(".")
    return cleaned or "channel"


def _split_row(line: str, width: int, delimiter: str = "|") -> list[str]:
    """Split a delimited row and pad or trim it to the header width."""
    cells = [c.strip() for c in line.split(delimiter)][:width]
    cells += [""] * (width - len(cells))
    return cells


def _strip_trailing_blanks(cells: list[str]) -> list[str]:
    out = list(cells)
    while out and out[-1] == "":
        out.pop()
    return out


def read_blocks(
    path: str | Path,
) -> tuple[list[str], dict[str, list[str]], list[str], dict[str, list[list[str]]]]:
    """Return ``(meta_header, meta_by_channel, sample_header, rows_by_channel)``.

    Text is left exactly as exported; only the repeated block framing is undone.
    """
    path = Path(path)
    lines = read_lines(path)
    if not is_tabular_export(lines):
        raise NotATabularExport(
            f"{path.name}: not an ADRE Sxp Tabular List export "
            f"(no '{SAMPLE_HEADER_PREFIX}...' header found)"
        )

    meta_header: list[str] = []
    sample_header: list[str] = []
    meta_by_channel: dict[str, list[str]] = {}
    rows_by_channel: dict[str, list[list[str]]] = {}
    seen_samples: dict[str, set[str]] = {}
    mode = ""

    for line in lines:
        if not line.strip():
            mode = ""
            continue
        if line.startswith(META_HEADER_PREFIX):
            mode = "meta"
            meta_header = _strip_trailing_blanks([c.strip() for c in line.split("|")])
            continue
        if line.startswith(SAMPLE_HEADER_PREFIX):
            mode = "sample"
            sample_header = _strip_trailing_blanks([c.strip() for c in line.split("|")])
            continue
        if mode == "meta" and meta_header:
            cells = _split_row(line, len(meta_header))
            name = cells[meta_header.index("Channel Name")].strip()
            if name:
                meta_by_channel.setdefault(name, cells)
            continue
        if mode == "sample" and sample_header:
            cells = _split_row(line, len(sample_header))
            name = cells[sample_header.index("Channel Name")].strip()
            number = cells[sample_header.index("Sample#")].strip()
            if not name or not number.isdigit():
                continue
            # The same sample can appear twice when plot groups overlap.
            if number in seen_samples.setdefault(name, set()):
                continue
            seen_samples[name].add(number)
            rows_by_channel.setdefault(name, []).append(cells)

    if not rows_by_channel:
        raise NotATabularExport(f"{path.name}: no sample rows found")
    return meta_header, meta_by_channel, sample_header, rows_by_channel


def _used_columns(header: list[str], rows: list[list[str]]) -> list[int]:
    """Column indexes that carry a value for this channel, plus the key ones."""
    return [
        index
        for index, column in enumerate(header)
        if column in KEEP_ALWAYS or any(row[index].strip() for row in rows)
    ]


def _render_channel(
    meta_header: list[str],
    meta_row: list[str],
    sample_header: list[str],
    rows: list[list[str]],
    *,
    drop_empty_columns: bool,
) -> str:
    keep = (
        _used_columns(sample_header, rows)
        if drop_empty_columns
        else list(range(len(sample_header)))
    )

    def line(cells: list[str]) -> str:
        return "\t".join(cells) + "\t"

    out = ["", "", line(meta_header), line(meta_row), ""]
    out.append(line([sample_header[i] for i in keep]))
    out.extend(line([row[i] for i in keep]) for row in rows)
    return "\r\n".join(out) + "\r\n"


def split_export(
    source: str | Path,
    folder: str | Path | None = None,
    *,
    encoding: str = "utf-16",
    drop_empty_columns: bool = True,
) -> SplitResult:
    """Write one file per channel, plus a single ``channels.csv`` config table.

    ``folder`` defaults to a ``channels`` directory beside ``source``.
    ``encoding`` defaults to UTF-16 so the output matches the ADRE exports.
    """
    source = Path(source)
    folder = Path(folder) if folder is not None else source.parent / "channels"
    folder.mkdir(parents=True, exist_ok=True)

    meta_header, meta_by_channel, sample_header, rows_by_channel = read_blocks(source)
    blank_meta = [""] * len(meta_header)

    written: list[Path] = []
    used_names: set[str] = set()
    for name, rows in rows_by_channel.items():
        stem = safe_filename(name)
        candidate = stem
        suffix = 2
        while candidate.casefold() in used_names:
            candidate = f"{stem}_{suffix}"
            suffix += 1
        used_names.add(candidate.casefold())
        target = folder / f"{candidate}.csv"
        target.write_text(
            _render_channel(
                meta_header,
                meta_by_channel.get(name, blank_meta),
                sample_header,
                rows,
                drop_empty_columns=drop_empty_columns,
            ),
            encoding=encoding,
            newline="",
        )
        written.append(target)

    config_file = folder / "channels.csv"
    with config_file.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow([*meta_header, "Samples", "File"])
        for target, (name, rows) in zip(written, rows_by_channel.items()):
            writer.writerow([*meta_by_channel.get(name, blank_meta), len(rows), target.name])

    n_blocks = sum(1 for line in read_lines(source) if line.startswith(META_HEADER_PREFIX))
    return SplitResult(
        source=source,
        folder=folder,
        channel_files=written,
        config_file=config_file,
        n_channels=len(rows_by_channel),
        n_samples=sum(len(rows) for rows in rows_by_channel.values()),
        config_rows_in=n_blocks * len(meta_by_channel),
        config_rows_out=len(meta_by_channel),
    )
