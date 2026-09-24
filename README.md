# Data-channels-extractor

One program from **ADRE zip (or static CSV) → per-channel CSVs → website upload groups**.

```bash
git clone https://github.com/vk8459698/Data-channels-extractor.git
cd Data-channels-extractor
pip install -r requirements.txt
```

## What to run

| What you have | Command |
| --- | --- |
| Tabular List CSV (`export_static.csv`) | `python main.py static.csv` |
| Zip of that CSV | `python main.py job.zip` |
| ADRE Sxp database zip (`.adb` / `ConfigDBFile.xml` / `Data\`) | `python main.py job.zip --adre` |

`--adre` is the native-database path. It needs **Windows**, **ADRE Sxp installed**, and `pip install pywinauto`. Native `.dat` files cannot be turned into CSV without ADRE.

```bash
python main.py "C:\path\to\export_static.csv" -o out
python main.py "C:\path\to\database.zip" --adre -o out
```

## What you get

```
out/
  channels/          every probe, one file each (uploadable)
    BRG1X.csv
    BRG1Y.csv
    Kph 1.csv
    39V-1A.csv
    THRUST A-LP9.csv
    channels.csv     transducer table, once
  groups/            website upload batches (do not mix these)
    rotor/           proximity XY + keyphasor  ← start here
    casing/          seismic
    thrust/          thrust position / load
    manifest.csv
```

On rotordyn.ai: **Upload → Select CSV → pick one group folder** (all files in `groups/rotor` together). Do not upload the original `export_static.csv`. Do not drop all 21 channels in one go.

`39VS21-1HD-LP3` is rewritten as `BRG1X` (and `1VD` as `BRG1Y`) so Orbit and Centerline can pair the probes. Plant tags stay in `channels.csv` under **ADRE Name**. Pass `--keep-names` to leave the tags in the files.

## The problem this solves

ADRE Sxp writes the Tabular List export as one pipe-delimited UTF-16 file and repeats the **entire transducer configuration block ahead of every sample block**. A 21-channel, 35-sample export therefore contains 735 configuration rows where 21 would do. The website cannot read that file.

This tool:

1. Optionally drives ADRE Sxp to export CSV from a database zip (`--adre`).
2. Splits the Tabular List into one file per channel, config row once.
3. Names bearing probes for plotting.
4. Copies them into **rotor / casing / thrust** groups the website accepts.
   Separate machines (Gas Turbine, Compressor, Generator) get their own rotor folders.
5. With `--adre`, the per-channel split runs **right after** `export_static.csv` is written — not before, and not on sync/async timebase files.

A zip named on the command line (`python main.py 3_GE_7HA.zip --adre`) is loaded from this folder, or from a sibling `RotorDyn` folder.

Cell text is copied verbatim, including vendor status codes such as `228BMA`.

## Options

| Flag | Effect |
| --- | --- |
| `-o DIR`, `--out DIR` | Write `channels/` and `groups/` under DIR |
| `--adre` | Open the zip in ADRE Sxp and export CSV, then split and group |
| `--keep-names` | Keep plant tags (`39VS21-1HD-LP3`) instead of `BRG1X` |
| `--keep-empty-columns` | Keep columns that are blank for the whole channel |
| `--encoding ENC` | Output encoding, default `utf-16` |

## Requirements

Python 3.10+. The CSV split uses only the standard library.

```bash
python -m pytest -q
```

For `--adre` on the extraction PC:

```bash
pip install pywinauto
```

ADRE Sxp must already be installed there. The click path lives in `adre_export_recipe.json`.
