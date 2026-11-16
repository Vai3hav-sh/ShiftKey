from datetime import date

import pytest

from app.parser import ParseError, parse_duration, parse_report, parse_time


def test_parses_raw_starlink_export(raw_bytes):
    rep = parse_report(raw_bytes, "daily format.xlsx")
    assert rep.report_date == date(2026, 10, 22)
    assert rep.company == "OMI"
    assert rep.title == "DAILY PERFORMANCE REPORT"
    assert rep.run_at == "23/10/2026 16:22"
    assert rep.total == 172
    assert len(rep.departments) == 30
    s = rep.summary()
    assert s["by_status"] == {"P": 141, "A": 26, "MIS": 2, "HLF": 3}
    assert s["halfday"] == 3 and s["mispunch"] == 2 and s["absent"] == 26
    first = rep.records[0]
    assert (first.paycode, first.name, first.dept_code, first.dept_name) == ("100284", "MUKESH KUMAR PANDEY", "001", "A.O.D")
    assert (first.shift, first.shift_start, first.time_in, first.time_out) == ("M07", "07:00", "06:47", "19:01")
    assert first.hrs == "12.14" and first.hrs_minutes == 12 * 60 + 14
    assert first.early == "0.13" and first.late is None and first.status == "P" and first.is_present
    assert first.segment == "indian"


def test_parses_hand_formatted_sheet_too(formatted_bytes):
    rep = parse_report(formatted_bytes, "21 October 2026 Expatriates.xlsx")
    assert rep.report_date == date(2026, 10, 21)
    assert rep.total == 171
    s = rep.summary()
    assert s["by_status"] == {"P": 136, "A": 25, "MIS": 6, "HLF": 4}
    assert rep.columns == ["paycode", "name", "time_in", "time_out", "status", "hrs"]


def test_flags_and_notes(raw_bytes):
    rep = parse_report(raw_bytes, "x.xlsx")
    by_code = {r.paycode: r for r in rep.records}
    mis = by_code["100115"]
    assert mis.is_mispunch and mis.time_in == "22:13" and mis.time_out is None
    assert any("no OUT punch" in n for n in mis.notes)
    hlf = by_code["100305"]
    assert hlf.is_halfday and not hlf.is_mispunch and hlf.status_label == "Half day"
    long_span = by_code["100238"]  # 05:07 -> 07:19 next day, 26.12 h, machine says P
    assert long_span.has_warning and not long_span.is_mispunch
    night = by_code["100043"]  # N20 shift 19:47 -> 08:11
    assert night.is_night and night.is_present
    manual = by_code["100303"]
    assert manual.manual and "Manual punch" in manual.notes
    absent = by_code["100212"]
    assert absent.is_absent and absent.time_in is None and not absent.has_warning


def test_single_punch_with_present_status_becomes_mispunch():
    grid = [
        ["", "", "Company Name:TEST"],
        ["", "", "DAILY PERFORMANCE REPORT FOR DATE : 01/02/2026"],
        ["", "PayCode", "Employee Name", "In", "Out", "Status", "Hrs Works"],
        ["** Department Code & Name : 001  OPS", None, None, None, None, None, None],
        ["", "1", "ONE PUNCH", "08:05", None, "P", ""],
        ["", "2", "FULL DAY", "08:00", "17:00", "P", "9.00"],
        ["", "3", "DOUBLE TAP", "08:00", "08:00", "P", "0.00"],
    ]
    from app.parser import parse_grid

    rep = parse_grid(grid, "anything.xlsx")
    assert rep.report_date == date(2026, 2, 1)
    r1, r2, r3 = rep.records
    assert r1.is_mispunch and "treated as mispunch" in r1.notes[0]
    assert not r2.is_mispunch and r2.hrs_minutes == 540
    assert r3.has_warning


def test_segments_from_paycode_prefix():
    from app.parser import parse_grid, segment_for

    assert segment_for("100284") == "indian" and segment_for("200017") == "local" and segment_for("9") == "other"
    grid = [["PayCode", "Employee Name", "Status"], ["100001", "IND", "P"], ["200001", "LOC", "A"]]
    rep = parse_grid(grid, "01 January 2026.xlsx")
    assert [r.segment for r in rep.records] == ["indian", "local"]


def test_date_fallback_from_filename():
    from app.parser import parse_grid

    grid = [["PayCode", "Employee Name", "Status"], ["9", "X", "P"]]
    rep = parse_grid(grid, "05 March 2026 Expatriates.xlsx")
    assert rep.report_date == date(2026, 3, 5)
    assert rep.problems
    rep2 = parse_grid(grid, "notes.xlsx")
    assert rep2.report_date is None


def test_rejects_garbage():
    with pytest.raises(ParseError):
        parse_report(b"hello,world\n1,2\n", "x.csv")
    with pytest.raises(ParseError):
        parse_report(b"\x00\x01\x02binary", "x.bin")


@pytest.mark.parametrize("value,expected", [
    ("06:51", "06:51"), ("6:51", "06:51"), ("18:49:00", "18:49"), (" 7.05 ", "07:05"),
    (0.5, "12:00"), ("", None), (None, None), ("abc", None), ("9:30 PM", "21:30"),
])
def test_parse_time(value, expected):
    assert parse_time(value) == expected


@pytest.mark.parametrize("value,expected", [
    ("    12.14", ("12.14", 734)), ("0.13", ("0.13", 13)), ("       ", (None, None)),
    (None, (None, None)), ("24.12", ("24.12", 1452)), ("8", ("8.00", 480)),
])
def test_parse_duration(value, expected):
    assert parse_duration(value) == expected
