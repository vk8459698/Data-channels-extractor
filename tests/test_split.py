"""Splitting an ADRE Sxp Tabular List export into per-channel files."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import main as cli  # noqa: E402
from adre_split import (  # noqa: E402
    NotATabularExport,
    read_blocks,
    safe_filename,
    split_export,
)

META = "CH#|Channel Name|Machine Name|Status|Angle|Direction|Speed Units(P)|Amp Unit|Phase Unit|"
SAMPLE = (
    "CH#|Channel Name|Sample#|Sample Cause|Date|Speed(P)|Speed(S)|Direct|Avg Gap|"
    "Inst Gap|1XAmplitude|1X Phase|2XAmplitude|2X Phase|Bandpass|Process Variable|"
)


def _block(sample_no: int) -> list[str]:
    """One repeated configuration + sample block, as ADRE Sxp writes it."""
    stamp = f"03Mar1953 03:03:{sample_no:02d}.468"
    return [
        "",
        META,
        "1|BRG1X|TURBINE|OK|45\u00b0|Right|rpm|mil pp|deg|",
        "2|KPH|TURBINE|OK|90\u00b0|Right|rpm|V pp|deg|",
        "",
        SAMPLE,
        f"1|BRG1X|{sample_no}|DT-T|{stamp}|3599|0|0.994|-10.4|-10.4|0.454|138BMA|0.221|25|0.965||",
        f"2|KPH|{sample_no}|DT-T|{stamp}|3599|0|10.66|-10.0|-10.0|||||10.62||",
    ]


def _write_export(path: Path, sample_numbers=(1, 2, 3)) -> Path:
    lines = ["", ""]
    for sample_no in sample_numbers:
        lines.extend(_block(sample_no))
    path.write_text("\r\n".join(lines) + "\r\n", encoding="utf-16", newline="")
    return path


@pytest.fixture()
def export(tmp_path: Path) -> Path:
    return _write_export(tmp_path / "export_static.csv")


class TestReadBlocks:
    def test_repeated_config_collapses_to_one_row_per_channel(self, export: Path):
        meta_header, meta, sample_header, rows = read_blocks(export)
        assert meta_header[:2] == ["CH#", "Channel Name"]
        assert sorted(meta) == ["BRG1X", "KPH"]
        assert sample_header[2] == "Sample#"
        assert [len(v) for v in rows.values()] == [3, 3]

    def test_duplicate_sample_numbers_are_dropped(self, tmp_path: Path):
        path = _write_export(tmp_path / "dupes.csv", sample_numbers=(1, 1, 2))
        _, _, _, rows = read_blocks(path)
        assert [r[2] for r in rows["BRG1X"]] == ["1", "2"]

    def test_rejects_a_file_that_is_not_a_tabular_export(self, tmp_path: Path):
        path = tmp_path / "plain.csv"
        path.write_text("a,b\n1,2\n", encoding="utf-8")
        with pytest.raises(NotATabularExport, match="not an ADRE Sxp Tabular List"):
            read_blocks(path)


class TestSplit:
    def test_writes_one_file_per_channel_plus_a_single_config_table(self, export, tmp_path):
        result = split_export(export, tmp_path / "channels")
        assert result.n_channels == 2
        assert result.n_samples == 6
        assert result.config_rows_in == 6
        assert result.config_rows_out == 2
        assert result.duplicate_config_rows == 4
        assert {p.name for p in result.channel_files} == {"BRG1X.csv", "KPH.csv"}
        config = result.config_file.read_text(encoding="utf-8-sig").splitlines()
        assert len(config) == 3  # header plus one row per channel
        assert config[0].endswith("Samples,File")

    def test_output_folder_defaults_to_channels_beside_the_input(self, export):
        result = split_export(export)
        assert result.folder == export.parent / "channels"
        assert (export.parent / "channels" / "BRG1X.csv").is_file()

    def test_values_and_vendor_status_codes_survive_verbatim(self, export, tmp_path):
        split_export(export, tmp_path / "channels")
        lines = (tmp_path / "channels" / "BRG1X.csv").read_text(encoding="utf-16").splitlines()
        header = next(
            i for i, l in enumerate(lines) if l.startswith("CH#\tChannel Name\tSample#")
        )
        data = [line for line in lines[header + 1 :] if line.strip()]
        assert len(data) == 3
        assert "138BMA" in data[0]
        assert "0.454" in data[0]

    def test_metadata_row_carries_the_probe_identity(self, export, tmp_path):
        split_export(export, tmp_path / "channels")
        lines = (tmp_path / "channels" / "BRG1X.csv").read_text(encoding="utf-16").splitlines()
        assert lines[2].startswith("CH#\tChannel Name\tMachine Name")
        assert lines[3].split("\t")[:5] == ["1", "BRG1X", "TURBINE", "OK", "45\u00b0"]

    def test_columns_empty_for_a_channel_are_dropped(self, export, tmp_path):
        split_export(export, tmp_path / "channels")
        text = (tmp_path / "channels" / "BRG1X.csv").read_text(encoding="utf-16")
        assert "Process Variable" not in text
        assert "1XAmplitude" in text

    def test_keeping_every_column_is_opt_in(self, export, tmp_path):
        split_export(export, tmp_path / "all", drop_empty_columns=False)
        text = (tmp_path / "all" / "BRG1X.csv").read_text(encoding="utf-16")
        assert "Process Variable" in text


class TestConsumerContract:
    """Readers take the transducer metadata only when exactly two non-empty
    lines precede the sample header. The raw export breaks that; the split
    files honour it."""

    def test_split_files_have_exactly_two_metadata_lines(self, export, tmp_path):
        result = split_export(export, tmp_path / "channels")
        for path in result.channel_files:
            lines = path.read_text(encoding="utf-16").splitlines()
            header = next(
                i for i, l in enumerate(lines) if l.startswith("CH#\tChannel Name\tSample#")
            )
            assert len([l for l in lines[:header] if l.strip()]) == 2, path.name

    def test_the_raw_export_breaks_the_rule(self, export):
        lines = export.read_text(encoding="utf-16").splitlines()
        header = next(i for i, l in enumerate(lines) if l.startswith("CH#|Channel Name|Sample#"))
        assert len([l for l in lines[:header] if l.strip()]) > 2


class TestSafeFilename:
    def test_strips_characters_windows_rejects(self):
        assert safe_filename("KEYPH.-LP13") == "KEYPH.-LP13"
        assert safe_filename("A/B:C") == "A_B_C"
        assert safe_filename("trailing.") == "trailing"
        assert safe_filename("") == "channel"


class TestCommandLine:
    def test_splits_the_file_named_on_the_command_line(self, export, tmp_path, capsys):
        code = cli.main([str(export), "-o", str(tmp_path / "out")])
        assert code == 0
        assert (tmp_path / "out" / "BRG1X.csv").is_file()
        assert "2 channels" in capsys.readouterr().out

    def test_reports_a_missing_file_without_crashing(self, tmp_path, capsys):
        code = cli.main([str(tmp_path / "nope.csv")])
        assert code == 2
        assert "no such file" in capsys.readouterr().err

    def test_reports_a_file_that_is_not_an_export(self, tmp_path, capsys):
        path = tmp_path / "plain.csv"
        path.write_text("a,b\n1,2\n", encoding="utf-8")
        assert cli.main([str(path)]) == 2
        assert "not an ADRE Sxp Tabular List" in capsys.readouterr().err
