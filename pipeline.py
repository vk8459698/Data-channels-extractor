"""Zip / folder / CSV → split channels → website upload groups.

ADRE Sxp databases arrive as a zip of native ``.dat`` files. Those cannot be
decoded without ADRE Sxp on Windows. Pass ``--adre`` on a machine that has
ADRE installed and this module drives the export, then splits and groups.

A zip that already contains a Tabular List CSV needs no ADRE install.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

from adre_split import NotATabularExport, is_tabular_export, read_lines, split_export

MAX_ZIP_UNCOMPRESSED = 80 * 1024 * 1024 * 1024
_PROJECT_ROOT = Path(__file__).resolve().parent


class PipelineError(RuntimeError):
    """The input was not a usable export or ADRE database."""


def resolve_user_path(raw: str | Path) -> Path:
    """Find a zip/CSV named on the command line.

    Bare names (``3_GE_7HA.zip``) are taken from this extractor folder, then
    the sibling RotorDyn folder — that is where the operator drops the zip.
    """
    path = Path(raw).expanduser()
    search: list[Path] = []
    if not path.is_absolute() and len(path.parts) == 1:
        search.append(_PROJECT_ROOT / path.name)
        search.append(_PROJECT_ROOT.parent / "RotorDyn" / path.name)
    search.extend((path, _PROJECT_ROOT / path, _PROJECT_ROOT / path.name))
    for candidate in search:
        if candidate.exists():
            return candidate
    return path


def _looks_like_zip(path: Path) -> bool:
    if path.suffix.lower() in {".zip", ".adbzip"}:
        return True
    if not path.is_file():
        return False
    with path.open("rb") as handle:
        return handle.read(2) == b"PK"


def _safe_extract(archive: Path, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive) as zf:
        bad = zf.testzip()
        if bad:
            raise PipelineError(f"Zip CRC failed on {bad}")
        total = 0
        dest = dest.resolve()
        members = []
        for info in zf.infolist():
            name = info.filename.replace("\\", "/")
            if name.startswith("/") or ".." in Path(name).parts:
                raise PipelineError(f"Zip path escapes the extract folder: {info.filename}")
            target = (dest / name).resolve()
            if dest not in target.parents and target != dest:
                raise PipelineError(f"Zip path escapes the extract folder: {info.filename}")
            total += max(info.file_size, 0)
            if total > MAX_ZIP_UNCOMPRESSED:
                raise PipelineError("Zip uncompressed size exceeds the safety limit (80 GiB)")
            members.append(info)
        zf.extractall(dest, members=members)


def find_adre_root(folder: Path) -> Path | None:
    if (folder / "ConfigDBFile.xml").is_file() or list(folder.glob("*.adb")):
        return folder
    hits = sorted({p.parent for p in folder.rglob("ConfigDBFile.xml")})
    if hits:
        for hit in hits:
            if (hit / "Data").is_dir() or list(hit.glob("*.adb")):
                return hit
        return hits[0]
    adb = sorted(folder.rglob("*.adb"))
    return adb[0].parent if adb else None


def find_tabular_csvs(folder: Path) -> list[Path]:
    found: list[Path] = []
    for path in sorted(folder.rglob("*.csv")):
        if path.name.casefold() in {"channels.csv", "manifest.csv"}:
            continue
        if "groups" in path.parts or path.parent.name.casefold() == "channels":
            continue
        try:
            if is_tabular_export(read_lines(path)):
                found.append(path)
        except (OSError, UnicodeError, NotATabularExport):
            continue
    return found


def _run_adre_export(database: Path, dest: Path) -> list[Path]:
    dest.mkdir(parents=True, exist_ok=True)
    try:
        import pywinauto  # noqa: F401
    except ImportError as exc:
        raise PipelineError(
            "ADRE CSV export needs pywinauto on the Windows machine that has ADRE Sxp. "
            "Install it with:  pip install pywinauto"
        ) from exc
    try:
        from adre_export import export_database_via_adre
    except ImportError as exc:
        raise PipelineError("adre_export.py is missing from this checkout.") from exc

    recipe = Path(__file__).resolve().parent / "adre_export_recipe.json"
    export_database_via_adre(
        database,
        dest,
        recipe_path=recipe,
        then_analyse=False,
    )
    return find_tabular_csvs(dest)


def process(
    source: str | Path,
    out: str | Path | None = None,
    *,
    encoding: str = "utf-16",
    drop_empty_columns: bool = True,
    name_for_plots: bool = True,
    run_adre: bool = False,
):
    """Extract (if needed), split, and group. Returns a list of SplitResult."""
    source = resolve_user_path(source)
    if not source.exists():
        raise FileNotFoundError(source)

    dest_root = Path(out) if out is not None else source.parent
    dest_root.mkdir(parents=True, exist_ok=True)
    csvs: list[Path] = []

    if source.is_file() and source.suffix.lower() in {".csv", ".txt"}:
        csvs = [source]
    elif source.is_file() and _looks_like_zip(source):
        unzipped = dest_root / "_unzipped"
        _safe_extract(source, unzipped)
        csvs = find_tabular_csvs(unzipped)
        adre = find_adre_root(unzipped)
        if not csvs and adre:
            if not run_adre:
                raise PipelineError(
                    f"{source.name} is an ADRE Sxp database, not a CSV zip. "
                    "On the Windows PC that has ADRE Sxp installed run:\n"
                    f"  python main.py {source} --adre"
                )
            csvs = _run_adre_export(adre, dest_root / "exports")
        elif not csvs:
            raise PipelineError(f"{source.name}: no Tabular List CSV and no ADRE database inside")
    elif source.is_dir():
        dest_root = Path(out) if out is not None else source
        dest_root.mkdir(parents=True, exist_ok=True)
        csvs = find_tabular_csvs(source)
        adre = find_adre_root(source)
        if not csvs and adre:
            if not run_adre:
                raise PipelineError(
                    f"{source.name} is an ADRE Sxp database folder. "
                    "On the Windows PC that has ADRE Sxp installed run:\n"
                    f"  python main.py {source} --adre"
                )
            csvs = _run_adre_export(adre, dest_root / "exports")
        elif not csvs:
            raise PipelineError(f"{source.name}: no Tabular List CSV found")
    else:
        raise PipelineError(f"{source.name}: pass a .csv, a .zip, or a folder")

    channels_dir = dest_root / "channels"
    results = []
    for path in csvs:
        try:
            results.append(
                split_export(
                    path,
                    channels_dir,
                    encoding=encoding,
                    drop_empty_columns=drop_empty_columns,
                    name_for_plots=name_for_plots,
                    write_upload_groups=True,
                )
            )
        except NotATabularExport:
            continue
    if not results:
        raise PipelineError("No Tabular List export could be split")
    return results
