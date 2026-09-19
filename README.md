# ADRE Sxp static export splitter

Turns one ADRE Sxp **Tabular List** export into **one file per channel**, with the
transducer configuration listed **once**.

```bash
pip install -r requirements.txt
python main.py static.csv
```

```
export_static.csv: 21 channels, 735 samples
  configuration rows 735 -> 21 (714 duplicates removed)
  wrote 21 channel files + channels.csv
  folder channels
```

## The problem this solves

ADRE Sxp writes the Tabular List export as a single pipe-delimited UTF-16 file and
repeats the **entire transducer configuration block ahead of every sample block**.
A 21-channel, 35-sample export therefore contains 735 configuration rows where 21
would do — roughly half the file is duplicated configuration.

That breaks tools that expect one table per file. A dashboard that reads the probe
name, mounting angle and units from a single configuration row above the data finds
22 configuration lines instead of 2, gives up, and ends up with no channel identity
at all. The repeated `CH#|...` header lines are then parsed as if they were data.

## What you get

Running the command creates a `channels/` folder next to the input:

```
channels/
  39VS21-1HD-LP3.csv     one channel, config row once, then its samples
  39VS22-1VD-LP4.csv
  39V-1A.csv
  KEYPH.-LP13.csv
  ...
  channels.csv           every transducer listed once, plain comma CSV
```

Each channel file is tab-delimited UTF-16 in the layout ADRE uses for a
single-channel export:

```
                                                        <- two blank lines
CH#  Channel Name     Machine Name  Status  Angle  Direction  Speed Units(P)  Amp Unit  Phase Unit
1    39VS21-1HD-LP3   GAS TURBINE   OK      45°    Right      rpm             mil pp    deg
                                                        <- one blank line
CH#  Channel Name     Sample#  Sample Cause  Date                    Speed(P)  ...
1    39VS21-1HD-LP3   1        DT-T          03Mar1953 03:03:03.468  3599      ...
```

`channels.csv` is the configuration table on its own, one row per probe, so you can
open it in Excel without wading through sample data:

```csv
CH#,Channel Name,Machine Name,Status,Angle,Direction,Speed Units(P),Amp Unit,Phase Unit,Samples,File
1,39VS21-1HD-LP3,GAS TURBINE,OK,45°,Right,rpm,mil pp,deg,35,39VS21-1HD-LP3.csv
```

## Nothing is altered

Cell text is copied through verbatim, including ADRE's vendor status codes such as
`228BMA` and `54FNX`. The tool only removes the repeated block framing. It never
rounds, reformats or renames a value.

Two things are tidied:

- **Duplicate samples.** If the same `Sample#` appears twice for a channel, because
  plot groups overlapped, the second copy is dropped.
- **Columns a channel never fills.** A proximity probe leaves `Process Variable`
  blank on every row, so that column is omitted from its file. Pass
  `--keep-empty-columns` to keep them.

## Options

| Flag | Effect |
| --- | --- |
| `-o DIR`, `--out DIR` | Write somewhere other than `channels/` beside the input |
| `--keep-empty-columns` | Keep columns that are blank for the whole channel |
| `--encoding ENC` | Output encoding, default `utf-16` to match ADRE |

Several exports at once:

```bash
python main.py export_static.csv another_export.csv -o all_channels
```

## Requirements

Python 3.10 or newer. The splitter uses only the standard library; `pytest` is
needed only to run the tests.

```bash
python -m pytest -q
```
