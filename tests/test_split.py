"""Splitting an ADRE Sxp Tabular List export into per-channel files."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import main as cli  # noqa: E402
from adre_split import (  # noqa: E402
    NotATabularExport,
    channel_group,
    plot_name,
    plot_names,
    read_blocks,
    safe_filename,
    split_export,
    write_groups,
)
from pipeline import PipelineError, process, resolve_user_path  # noqa: E402

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

    def test_reads_tab_delimited_tabular_list(self, tmp_path: Path):
        from adre_split import file_is_tabular

        lines = ["", ""]
        for sample_no in (1, 2):
            for line in _block(sample_no):
                lines.append(line.replace("|", "\t"))
        path = tmp_path / "export_static.csv"
        path.write_text("\r\n".join(lines) + "\r\n", encoding="utf-16", newline="")
        assert file_is_tabular(path)
        _, meta, _, rows = read_blocks(path)
        assert "BRG1X" in meta
        assert len(rows["BRG1X"]) == 2

    def test_empty_save_stub_is_not_tabular(self, tmp_path: Path):
        from adre_split import file_is_tabular

        path = tmp_path / "export_static.csv"
        path.write_text("", encoding="utf-16")
        assert not file_is_tabular(path)


class TestSplit:
    def test_writes_one_file_per_channel_plus_a_single_config_table(self, export, tmp_path):
        result = split_export(export, tmp_path / "channels")
        assert result.n_channels == 2
        assert result.n_samples == 6
        assert result.config_rows_in == 6
        assert result.config_rows_out == 2
        assert result.duplicate_config_rows == 4
        assert {p.name for p in result.channel_files} == {"BRG1X.csv", "Kph 1.csv"}
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


def _tagged_export(path: Path) -> Path:
    """An export named the way a plant tags its channels."""
    meta = [
        "1|39VS21-1HD-LP3|GAS TURBINE|OK|45\u00b0|Right|rpm|mil pp|deg|",
        "2|39VS22-1VD-LP4|GAS TURBINE|OK|45\u00b0|Left|rpm|mil pp|deg|",
        "9|39V-1A|GAS TURBINE|OK|0\u00b0|None|rpm|in/s rms|deg|",
        "13|THRUST A-LP9|GAS TURBINE|OK|0\u00b0|None|rpm|mil|deg|",
        "21|KEYPH.-LP13||OK|90\u00b0|Right|rpm|V pp|deg|",
    ]
    samples = [
        "1|39VS21-1HD-LP3|1|DT-T|03Mar1953 03:03:03.468|3599|0|0.994|-10.4|-10.4|0.454|138|0.221|25|0.965||",
        "2|39VS22-1VD-LP4|1|DT-T|03Mar1953 03:03:03.468|3599|0|1.086|-10.2|-10.2|0.566|31|0.332|286|1.061||",
        "9|39V-1A|1|DT-T|03Mar1953 03:03:03.468|3599|0|0.051||||||||0.051||",
        "13|THRUST A-LP9|1|DT-T|03Mar1953 03:03:03.468|3599|0|-11.2||||||||-11.2||",
        "21|KEYPH.-LP13|1|DT-T|03Mar1953 03:03:03.468|3599|0|10.66|-10.0|-10.0|||||10.62||",
    ]
    lines = ["", "", "", META, *meta, "", SAMPLE, *samples]
    path.write_text("\r\n".join(lines) + "\r\n", encoding="utf-16", newline="")
    return path


@pytest.fixture()
def tagged(tmp_path: Path) -> Path:
    return _tagged_export(tmp_path / "export_static.csv")


class TestPlotNames:
    """Orbit and centreline plots pair a bearing's probes by name, so the
    bearing and orientation stated in a plant tag is translated to BRG<n>X /
    BRG<n>Y. Horizontal is X, vertical is Y, matching the reference exports."""

    @pytest.mark.parametrize(
        ("tag", "expected"),
        [
            ("39VS21-1HD-LP3", "BRG1X"),
            ("39VS22-1VD-LP4", "BRG1Y"),
            ("39VS102-4HD-LP8", "BRG4X"),
            ("39VS101-4VD-LP7", "BRG4Y"),
            ("KEYPH.-LP13", "Kph"),
            ("Kph 1", "Kph"),
            ("39V-1A", None),
            ("THRUST A-LP9", None),
            ("Thrust Pos A", None),
            ("BRG1X", None),
        ],
    )
    def test_translates_only_tags_that_state_a_bearing(self, tag, expected):
        assert plot_name(tag) == expected

    def test_keyphasors_are_numbered(self):
        assert plot_names(["KEYPH.-LP13", "KPH2"]) == {"KEYPH.-LP13": "Kph 1", "KPH2": "Kph 2"}

    def test_a_channel_already_named_for_plotting_is_left_alone(self):
        assert plot_names(["Kph 1", "BRG1X"]) == {}

    def test_a_translation_two_channels_want_is_refused(self):
        # Both read as bearing 1 horizontal; guessing which is X would be worse
        # than leaving them alone.
        assert plot_names(["39VS21-1HD-LP3", "39VS31-1HD-LP9"]) == {}

    def test_a_translation_another_channel_already_uses_is_refused(self):
        assert plot_names(["39VS21-1HD-LP3", "BRG1X"]) == {}


class TestNamingOnDisk:
    def test_bearing_probes_and_keyphasor_are_renamed(self, tagged, tmp_path):
        result = split_export(tagged, tmp_path / "channels")
        assert {p.name for p in result.channel_files} == {
            "BRG1X.csv",
            "BRG1Y.csv",
            "39V-1A.csv",
            "THRUST A-LP9.csv",
            "Kph 1.csv",
        }
        assert result.renames == {
            "39VS21-1HD-LP3": "BRG1X",
            "39VS22-1VD-LP4": "BRG1Y",
            "KEYPH.-LP13": "Kph 1",
        }

    def test_the_new_name_reaches_both_the_config_row_and_the_samples(self, tagged, tmp_path):
        split_export(tagged, tmp_path / "channels")
        lines = (tmp_path / "channels" / "BRG1X.csv").read_text(encoding="utf-16").splitlines()
        assert lines[3].split("\t")[1] == "BRG1X"
        header = next(
            i for i, l in enumerate(lines) if l.startswith("CH#\tChannel Name\tSample#")
        )
        assert lines[header + 1].split("\t")[1] == "BRG1X"
        assert "39VS21-1HD-LP3" not in "\n".join(lines)

    def test_the_plant_tag_stays_on_record_in_channels_csv(self, tagged, tmp_path):
        result = split_export(tagged, tmp_path / "channels")
        rows = result.config_file.read_text(encoding="utf-8-sig").splitlines()
        assert "ADRE Name" in rows[0]
        assert any("BRG1X" in row and "39VS21-1HD-LP3" in row for row in rows[1:])

    def test_measurements_are_untouched_by_renaming(self, tagged, tmp_path):
        split_export(tagged, tmp_path / "channels")
        text = (tmp_path / "channels" / "BRG1X.csv").read_text(encoding="utf-16")
        assert "0.454" in text and "45\u00b0" in text and "Right" in text

    def test_keeping_the_plant_tags_is_opt_in(self, tagged, tmp_path):
        result = split_export(tagged, tmp_path / "tags", name_for_plots=False)
        assert result.renames == {}
        assert (tmp_path / "tags" / "39VS21-1HD-LP3.csv").is_file()


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
        assert (tmp_path / "out" / "channels" / "BRG1X.csv").is_file()
        assert "2 channels" in capsys.readouterr().out

    def test_reports_a_missing_file_without_crashing(self, tmp_path, capsys):
        code = cli.main([str(tmp_path / "nope.csv")])
        assert code == 2
        assert "no such file" in capsys.readouterr().err

    def test_keep_names_leaves_the_plant_tags_alone(self, tagged, tmp_path, capsys):
        assert cli.main([str(tagged), "-o", str(tmp_path / "out"), "--keep-names"]) == 0
        assert (tmp_path / "out" / "channels" / "39VS21-1HD-LP3.csv").is_file()
        assert "named for plotting" not in capsys.readouterr().out

    def test_renaming_is_reported(self, tagged, tmp_path, capsys):
        assert cli.main([str(tagged), "-o", str(tmp_path / "out")]) == 0
        assert "39VS21-1HD-LP3 -> BRG1X" in capsys.readouterr().out

    def test_reports_a_file_that_is_not_an_export(self, tmp_path, capsys):
        path = tmp_path / "plain.csv"
        path.write_text("a,b\n1,2\n", encoding="utf-8")
        assert cli.main([str(path)]) == 2
        err = capsys.readouterr().err.lower()
        assert "tabular list" in err


class TestGroups:
    def test_bearing_and_keyphasor_go_to_rotor(self):
        assert channel_group("BRG1X") == "rotor"
        assert channel_group("Kph 1") == "rotor"
        assert channel_group("39VS21-1HD-LP3") == "rotor"

    def test_casing_and_thrust_are_separate_batches(self):
        assert channel_group("39V-1A") == "casing"
        assert channel_group("THRUST A-LP9") == "thrust"

    def test_split_writes_upload_batches(self, tagged, tmp_path):
        result = split_export(tagged, tmp_path / "channels")
        assert result.groups_folder == tmp_path / "groups"
        names = {g: {p.name for p in files} for g, files in result.groups.items()}
        assert names["rotor"] == {"BRG1X.csv", "BRG1Y.csv", "Kph 1.csv"}
        assert names["casing"] == {"39V-1A.csv"}
        assert names["thrust"] == {"THRUST A-LP9.csv"}
        assert (tmp_path / "groups" / "manifest.csv").is_file()

    def test_separate_rotors_when_machine_names_differ(self, tmp_path):
        gt = tmp_path / "BRG1X.csv"
        st = tmp_path / "BRG3X.csv"
        gt.write_text("x", encoding="utf-8")
        st.write_text("x", encoding="utf-8")
        groups = write_groups(
            [gt, st],
            tmp_path / "groups",
            machines={gt: "GAS TURBINE", st: "GENERATOR"},
        )
        assert (tmp_path / "groups" / "GAS TURBINE" / "rotor" / "BRG1X.csv").is_file()
        assert (tmp_path / "groups" / "GENERATOR" / "rotor" / "BRG3X.csv").is_file()
        assert "GAS TURBINE/rotor" in groups
        assert "GENERATOR/rotor" in groups


class TestZipPipeline:
    def test_zip_of_a_tabular_export_is_split_and_grouped(self, tagged, tmp_path):
        import zipfile

        archive = tmp_path / "job.zip"
        with zipfile.ZipFile(archive, "w") as zf:
            zf.write(tagged, "export_static.csv")
        results = process(archive, tmp_path / "out")
        assert len(results) == 1
        assert (tmp_path / "out" / "channels" / "BRG1X.csv").is_file()
        assert (tmp_path / "out" / "groups" / "rotor" / "BRG1X.csv").is_file()
        assert (tmp_path / "out" / "groups" / "casing" / "39V-1A.csv").is_file()
        assert (tmp_path / "out" / "groups" / "thrust" / "THRUST A-LP9.csv").is_file()

    def test_adre_database_zip_without_flag_explains_what_to_run(self, tmp_path):
        import zipfile

        db = tmp_path / "db"
        db.mkdir()
        (db / "ConfigDBFile.xml").write_text("<ADRE_ROOT/>", encoding="utf-8")
        archive = tmp_path / "native.zip"
        with zipfile.ZipFile(archive, "w") as zf:
            zf.write(db / "ConfigDBFile.xml", "ConfigDBFile.xml")
        with pytest.raises(PipelineError, match="--adre"):
            process(archive, tmp_path / "out")

    def test_bare_zip_name_resolves_from_project_folder(self, tmp_path, monkeypatch):
        import pipeline as pipe

        archive = tmp_path / "job_from_project.zip"
        archive.write_bytes(b"PK\x03\x04")
        monkeypatch.setattr(pipe, "_PROJECT_ROOT", tmp_path)
        assert pipe.resolve_user_path("job_from_project.zip") == archive


def test_plot_config_dialog_title_match():
    from adre_export import (
        _config_dialog_score,
        _is_plot_config_dialog_title,
        _item_text_is_configure,
        _plot_click_points,
    )

    assert _is_plot_config_dialog_title("Timebase Plot Group Configuration")
    assert _is_plot_config_dialog_title("Tabular List Plot Group Configuration")
    assert not _is_plot_config_dialog_title("HV - Plot Session")
    assert _config_dialog_score("Timebase Plot Group Configuration", "Timebase") > (
        _config_dialog_score("Tabular List Plot Group Configuration")
    )
    assert _config_dialog_score("Tabular List Plot Group Configuration", "Timebase") == 0
    assert _item_text_is_configure("Configure")
    assert _item_text_is_configure("&Configure")
    assert _item_text_is_configure("Configure...")
    assert not _item_text_is_configure("Configuration Hierarchy")

    class Rect:
        left, top, right, bottom = 100, 80, 900, 680

    class Group:
        def rectangle(self):
            return Rect()

    points = _plot_click_points(Group())
    assert len(points) >= 3
    for x, y in points:
        assert 140 <= x <= 860
        assert 130 <= y <= 630

