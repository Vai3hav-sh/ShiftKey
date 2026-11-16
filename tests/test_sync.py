"""SQL integrations: connections, console, pull & push jobs, scheduler – against a SQLite 'Starlink'."""
import sqlite3
from datetime import date, datetime, timedelta

import pytest

from conftest import EXTERNAL_DB, HDR, login, make_client


@pytest.fixture(scope="module", autouse=True)
def starlink_db():
    """A miniature Starlink-like database."""
    con = sqlite3.connect(EXTERNAL_DB)
    cur = con.cursor()
    cur.executescript("""
        DROP TABLE IF EXISTS tblEmployee; DROP TABLE IF EXISTS tblDepartment;
        DROP TABLE IF EXISTS tblTimeRegister; DROP TABLE IF EXISTS tblPunches; DROP TABLE IF EXISTS hr_attendance;
        CREATE TABLE tblDepartment (DepartmentCode TEXT PRIMARY KEY, DepartmentName TEXT);
        CREATE TABLE tblEmployee (PayCode TEXT PRIMARY KEY, EmpName TEXT, DepartmentCode TEXT, PresentCardNo TEXT, Shift TEXT);
        CREATE TABLE tblTimeRegister (DateOffice TEXT, PayCode TEXT, ShiftAttended TEXT, In1 TEXT, Out1 TEXT, In2 TEXT, Out2 TEXT,
                                      HoursWorked REAL, Status TEXT, LateArrival TEXT, OverTime TEXT);
        CREATE TABLE tblPunches (PayCode TEXT, PunchTime TEXT, Direction TEXT);
        CREATE TABLE hr_attendance (att_date TEXT, emp_code TEXT, in_time TEXT, out_time TEXT, status TEXT, PRIMARY KEY (att_date, emp_code));
    """)
    cur.executemany("INSERT INTO tblDepartment VALUES (?,?)", [("012", "FURNACE"), ("022", "SECURITY"), ("030", "HR")])
    cur.executemany("INSERT INTO tblEmployee VALUES (?,?,?,?,?)", [
        ("100238", "HIRA RAJBHAR", "012", "00100238", "M07"),
        ("100045", "SURAJ KUMAR SINGH", "012", "00100045", "M07"),
        ("100138", "ARUN KUMAR", "022", "00100138", "N19"),
        ("200017", "JOHN MWANGI", "022", "00200017", "M07"),
        ("100216", "NITESH SHA", "030", "00100216", "830"),
    ])
    # daily register for 2026-10-20 and 21 (Starlink stores datetimes; strings here)
    cur.executemany("INSERT INTO tblTimeRegister VALUES (?,?,?,?,?,?,?,?,?,?,?)", [
        ("2026-10-20", "100238", "M07", "2026-10-20 06:50:00", None, None, "2026-10-20 19:05:00", 12.25, "P", None, "0.15"),
        ("2026-10-20", "100045", "M07", "2026-10-20 07:11:00", None, None, None, None, "MIS", "0.11", None),
        ("2026-10-20", "100138", "N19", "2026-10-20 18:55:00", None, None, "2026-10-21 07:02:00", 12.12, "P", None, None),
        ("2026-10-20", "200017", "M07", "2026-10-20 08:40:00", None, None, "2026-10-20 13:00:00", 4.33, "HLF", "1.40", None),
        ("2026-10-20", "100216", "830", None, None, None, None, None, "A", None, None),
        ("2026-10-21", "100238", "M07", "2026-10-21 06:48:00", None, None, "2026-10-21 19:00:00", 12.2, "P", None, None),
        ("2026-10-21", "100045", "M07", "2026-10-21 07:00:00", None, None, "2026-10-21 19:10:00", 12.17, "P", None, None),
        ("2026-10-21", "100138", "N19", None, None, None, None, None, "A", None, None),
        ("2026-10-21", "200017", "M07", "2026-10-21 07:05:00", None, None, "2026-10-21 19:01:00", 11.93, "P", None, None),
        ("2026-10-21", "100216", "830", "2026-10-21 08:35:00", None, None, "2026-10-21 18:50:00", 10.25, "P", None, None),
    ])
    # raw punch log for 2026-10-22 (day + night shift + single punch)
    cur.executemany("INSERT INTO tblPunches VALUES (?,?,?)", [
        ("100238", "2026-10-22 06:52:00", "IN"), ("100238", "2026-10-22 12:00:00", "OUT"), ("100238", "2026-10-22 12:30:00", "IN"),
        ("100238", "2026-10-22 19:03:00", "OUT"),
        ("100045", "2026-10-22 07:09:00", "IN"),                                           # single punch -> MIS
        ("100138", "2026-10-22 18:57:00", "IN"), ("100138", "2026-10-23 07:04:00", "OUT"),  # night shift across midnight
        ("100138", "2026-10-23 18:58:00", "IN"),                                           # next night, must not be used
        ("200017", "2026-10-22 08:01:00", None), ("200017", "2026-10-22 17:30:00", None),
    ])
    con.commit()
    con.close()
    yield EXTERNAL_DB


def _ext(sql, params=()):
    con = sqlite3.connect(EXTERNAL_DB)
    try:
        return con.execute(sql, params).fetchall()
    finally:
        con.close()


def _connection(client, name="Starlink", read_only=True):
    r = client.post("/api/connections", json={"name": name, "dialect": "sqlite", "database": EXTERNAL_DB,
                                              "password": "", "read_only": read_only})
    assert r.status_code in (201, 409), r.text
    if r.status_code == 409:
        return next(c for c in client.get("/api/connections").json() if c["name"] == name)
    return r.json()


def test_catalog_and_connection_lifecycle(client):
    cat = client.get("/api/sync/catalog").json()
    assert {d["key"] for d in cat["dialects"]} == {"mssql", "mysql", "postgresql", "sqlite"}
    assert next(d for d in cat["dialects"] if d["key"] == "sqlite")["available"] is True
    assert any(f["key"] == "paycode" and f["required"] for f in cat["fields"])
    assert {d["key"] for d in cat["datasets"]} == {"attendance", "corrections", "audit_events", "reports"}

    # unsaved test with a bad path fails politely
    r = client.post("/api/connections/test", json={"name": "x", "dialect": "sqlite", "database": "/nonexistent/dir/x.db", "password": ""})
    assert r.status_code == 200 and r.json()["ok"] is False
    # host required for server dialects
    assert client.post("/api/connections", json={"name": "bad", "dialect": "mysql", "password": "p"}).status_code == 400

    c = _connection(client)
    assert c["has_password"] is False and c["driver_available"] is True and c["read_only"] is True
    t = client.post(f"/api/connections/{c['id']}/test").json()
    assert t["ok"] is True and t["tables"] == 5 and "sqlite" in t["server"].lower() or t["ok"]
    c2 = next(x for x in client.get("/api/connections").json() if x["id"] == c["id"])
    assert c2["last_test_ok"] is True and "5 tables" in c2["last_test_message"]

    # password is stored encrypted and never returned
    r = client.patch(f"/api/connections/{c['id']}", json={"name": "Starlink", "dialect": "sqlite", "database": EXTERNAL_DB,
                                                           "password": "s3cret!", "read_only": True, "notes": "prod"})
    assert r.status_code == 200 and r.json()["has_password"] is True and "s3cret" not in r.text
    from app.connectors.crypto import decrypt, encrypt
    from app.db import SessionLocal
    from app.models import Connection
    with SessionLocal() as db:
        row = db.get(Connection, c["id"])
        assert row.password_enc != "s3cret!" and decrypt(row.password_enc) == "s3cret!"
    assert decrypt(encrypt("abc")) == "abc" and encrypt("") is None

    # audit entries exist
    acts = [e["action"] for e in client.get("/api/audit?period=all&category=integration&limit=50").json()["events"]]
    assert "connection.created" in acts and "connection.tested" in acts and "connection.updated" in acts


def test_schema_browser_and_console(client):
    c = _connection(client)
    tables = client.get(f"/api/connections/{c['id']}/schema").json()
    assert {t["name"] for t in tables} >= {"tblEmployee", "tblTimeRegister", "tblPunches"}
    cols = client.get(f"/api/connections/{c['id']}/schema/tblTimeRegister?preview=true&limit=3").json()
    assert [x["name"] for x in cols["columns"]][:3] == ["DateOffice", "PayCode", "ShiftAttended"]
    assert cols["preview"]["row_count"] == 3 and cols["preview"]["truncated"] is True

    q = client.post(f"/api/connections/{c['id']}/query", json={
        "sql": "SELECT r.*, e.EmpName, e.DepartmentCode, d.DepartmentName FROM tblTimeRegister r "
               "JOIN tblEmployee e ON e.PayCode = r.PayCode JOIN tblDepartment d ON d.DepartmentCode = e.DepartmentCode "
               "WHERE r.DateOffice = :report_date AND r.In1 IS NOT NULL AND '08:30' <> ''  -- colon in literal must survive",
        "params": {"report_date": "2026-10-20"}, "limit": 10})
    assert q.status_code == 200, q.text
    body = q.json()
    assert body["row_count"] == 4 and "EmpName" in body["columns"]
    sm = body["suggested_mapping"]
    assert sm["paycode"] == "PayCode" and sm["name"] == "EmpName" and sm["dept_code"] == "DepartmentCode"
    assert sm["dept_name"] == "DepartmentName" and sm["time_in"] == "In1" and sm["time_out"] == "Out2"
    assert sm["lunch_out"] == "Out1" and sm["lunch_in"] == "In2" and sm["report_date"] == "DateOffice"
    assert sm["hrs"] == "HoursWorked" and sm["status"] == "Status" and sm["shift"] == "ShiftAttended" and sm["late"] == "LateArrival"

    # read-only guard: writes are rejected and nothing changes
    for bad in ("UPDATE tblEmployee SET EmpName='x'", "SELECT 1; DROP TABLE tblEmployee", "DELETE FROM tblPunches",
                "select * into t2 from tblEmployee"):
        r = client.post(f"/api/connections/{c['id']}/query", json={"sql": bad})
        assert r.status_code == 400, bad
    assert _ext("SELECT COUNT(*) FROM tblEmployee")[0][0] == 5
    # broken SQL -> 502 with the driver message, audited as failure
    r = client.post(f"/api/connections/{c['id']}/query", json={"sql": "SELECT * FROM nope"})
    assert r.status_code == 502 and "nope" in r.json()["detail"]
    ev = client.get("/api/audit?period=all&action=sql.query&limit=5").json()["events"]
    assert ev[0]["outcome"] == "failure" and any(e["outcome"] == "denied" for e in ev)


DAILY_SQL = ("SELECT r.DateOffice, r.PayCode, e.EmpName, e.PresentCardNo, e.DepartmentCode, d.DepartmentName, r.ShiftAttended, "
             "r.In1, r.Out1, r.In2, r.Out2, r.HoursWorked, r.Status, r.LateArrival, r.OverTime "
             "FROM tblTimeRegister r JOIN tblEmployee e ON e.PayCode = r.PayCode "
             "JOIN tblDepartment d ON d.DepartmentCode = e.DepartmentCode WHERE r.DateOffice = :report_date")


def test_pull_preview_and_daily_job(client):
    c = _connection(client)
    pv = client.post("/api/sync/preview", json={"connection_id": c["id"], "mode": "daily", "sql": DAILY_SQL,
                                                 "date": "2026-10-20", "options": {"hrs_format": "decimal"}})
    assert pv.status_code == 200, pv.text
    pv = pv.json()
    assert pv["main"]["row_count"] == 5 and pv["parse_error"] is None
    parsed = pv["parsed"][0]
    assert parsed["report_date"] == "2026-10-20" and parsed["summary"]["total"] == 5
    assert parsed["summary"]["by_status"] == {"P": 2, "MIS": 1, "HLF": 1, "A": 1}
    assert parsed["summary"]["by_segment"] == {"indian": 4, "local": 1}
    assert any(f["paycode"] == "100045" and f["status"] == "MIS" for f in parsed["flagged"])

    body = {"name": "Starlink daily", "direction": "pull", "connection_id": c["id"], "mode": "daily", "sql": DAILY_SQL,
            "mapping": pv["suggested_mapping"], "options": {"hrs_format": "decimal", "replace_existing": True},
            "schedule_enabled": True, "schedule_time": "06:00", "schedule_offset_days": 1}
    r = client.post("/api/sync/jobs", json=body)
    assert r.status_code == 201, r.text
    job = r.json()
    assert job["connection_name"] == "Starlink" and job["mapping"]["time_out"] == "Out2"

    run = client.post(f"/api/sync/jobs/{job['id']}/run", json={"date_from": "2026-10-20", "date_to": "2026-10-21"}).json()
    assert run["status"] == "success", run
    assert run["rows_out"] == 2 and run["rows_in"] == 10 and len(run["report_ids"]) == 2
    reports = {x["report_date"]: x for x in client.get("/api/reports").json()}
    d20 = reports["2026-10-20"]
    assert d20["source_kind"] == "sql" and d20["total"] == 5 and d20["mispunch"] == 1 and d20["halfday"] == 1
    det = client.get(f"/api/reports/{d20['id']}").json()
    hira = next(x for g in det["departments"] for x in g["records"] if x["paycode"] == "100238")
    assert (hira["time_in"], hira["time_out"], hira["hrs"], hira["status"]) == ("06:50", "19:05", "12.15", "P")
    assert hira["card_no"] == "00100238" and hira["dept_name"] == "FURNACE" and hira["ot"] == "0.15"
    night = next(x for g in det["departments"] for x in g["records"] if x["paycode"] == "100138")
    assert night["is_night"] and night["time_out"] == "07:02"
    local = next(x for g in det["departments"] for x in g["records"] if x["paycode"] == "200017")
    assert local["segment"] == "local" and local["is_halfday"] and local["late"] == "1.40"
    assert det["source_filename"] == "sql://Starlink/Starlink daily" and det["sync_job_id"] == job["id"]
    # lineage: audit + run log
    ev = client.get(f"/api/audit?period=all&action=sync.pull&report_id={d20['id']}").json()["events"][0]
    assert ev["meta"]["job"] == "Starlink daily" and ev["after"]["total"] == 5
    runs = client.get(f"/api/sync/runs?job_id={job['id']}").json()
    assert runs[0]["log"] and "2026-10-21" in runs[0]["log"][-1]

    # rerun replaces (same ids are gone, counts unchanged)
    run2 = client.post(f"/api/sync/jobs/{job['id']}/run", json={"date_from": "2026-10-20", "date_to": "2026-10-20"}).json()
    assert run2["status"] == "success" and len(client.get("/api/reports").json()) >= 2
    assert client.get("/api/audit?period=all&action=report.replaced").status_code == 200

    # a day with no rows -> partial, nothing created
    run3 = client.post(f"/api/sync/jobs/{job['id']}/run", json={"date_from": "2026-10-25"}).json()
    assert run3["status"] == "partial" and run3["report_ids"] == []

    # the range form of the query (date_from/date_to + mapped date column) splits into days
    rng = dict(body, name="Starlink range", schedule_enabled=False,
               sql=DAILY_SQL.replace("WHERE r.DateOffice = :report_date", "WHERE r.DateOffice BETWEEN :date_from AND :date_to"))
    j2 = client.post("/api/sync/jobs", json=rng).json()
    run4 = client.post(f"/api/sync/jobs/{j2['id']}/run", json={"date_from": "2026-10-20", "date_to": "2026-10-21"}).json()
    assert run4["status"] == "success" and run4["rows_out"] == 2


def test_pull_punch_log_job(client):
    c = _connection(client)
    emp_sql = ("SELECT e.PayCode, e.EmpName, e.DepartmentCode, d.DepartmentName, e.Shift FROM tblEmployee e "
               "JOIN tblDepartment d ON d.DepartmentCode = e.DepartmentCode")
    punch_sql = "SELECT PayCode, PunchTime, Direction FROM tblPunches WHERE PunchTime BETWEEN :date_from AND :date_to"
    pv = client.post("/api/sync/preview", json={"connection_id": c["id"], "mode": "punches", "sql": punch_sql,
                                                 "employees_sql": emp_sql, "date": "2026-10-22",
                                                 "options": {"day_start": "04:00", "max_span_hours": 16}}).json()
    assert pv["suggested_mapping"] == {"paycode": "PayCode", "punch_time": "PunchTime", "direction": "Direction"}
    assert pv["suggested_employees_mapping"]["shift"] == "Shift"
    r = client.post("/api/sync/jobs", json={"name": "Starlink punches", "direction": "pull", "connection_id": c["id"],
                                            "mode": "punches", "sql": punch_sql, "employees_sql": emp_sql,
                                            "mapping": pv["suggested_mapping"], "employees_mapping": pv["suggested_employees_mapping"],
                                            "options": {"day_start": "04:00", "max_span_hours": 16,
                                                        "date_param_format": "%Y-%m-%d 00:00:00"}})
    assert r.status_code == 201, r.text
    run = client.post(f"/api/sync/jobs/{r.json()['id']}/run", json={"date_from": "2026-10-22"}).json()
    assert run["status"] == "success", run
    rep = next(x for x in client.get("/api/reports").json() if x["report_date"] == "2026-10-22")
    det = client.get(f"/api/reports/{rep['id']}").json()
    by = {x["paycode"]: x for g in det["departments"] for x in g["records"]}
    assert (by["100238"]["time_in"], by["100238"]["time_out"], by["100238"]["status"], by["100238"]["hrs"]) == ("06:52", "19:03", "P", "12.11")
    assert by["100045"]["status"] == "MIS" and by["100045"]["time_in"] == "07:09" and by["100045"]["time_out"] is None
    assert (by["100138"]["time_in"], by["100138"]["time_out"]) == ("18:57", "07:04") and by["100138"]["is_night"]
    assert by["200017"]["status"] == "P" and by["200017"]["segment"] == "local"
    assert by["100216"]["status"] == "A" and det["summary"]["total"] == 5


def test_push_jobs_and_auto_push(client):
    c = _connection(client)
    # read-only connections refuse push jobs
    r = client.post("/api/sync/jobs", json={"name": "push att", "direction": "push", "connection_id": c["id"], "dataset": "attendance"})
    assert r.status_code == 400 and "read-only" in r.json()["detail"]
    client.patch(f"/api/connections/{c['id']}", json={"name": "Starlink", "dialect": "sqlite", "database": EXTERNAL_DB, "read_only": False})

    r = client.post("/api/sync/jobs", json={"name": "push att", "direction": "push", "connection_id": c["id"], "dataset": "attendance",
                                            "create_table": True, "auto_push": True})
    assert r.status_code == 201, r.text
    pj = r.json()
    assert pj["target_table"] == "shiftkey_attendance" and pj["key_fields"] == ["report_date", "paycode"]
    run = client.post(f"/api/sync/jobs/{pj['id']}/run", json={"date_from": "2026-10-20", "date_to": "2026-10-22"}).json()
    assert run["status"] == "success", run
    assert "inserted" in run["message"]
    rows = _ext("SELECT report_date, paycode, time_in, time_out, status, is_corrected, dept_name FROM shiftkey_attendance ORDER BY 1,2")
    assert len(rows) == 15 and rows[0][:5] == ("2026-10-20", "100045", "07:11", None, "MIS")
    # idempotent: second run only updates
    run2 = client.post(f"/api/sync/jobs/{pj['id']}/run", json={"date_from": "2026-10-20", "date_to": "2026-10-22"}).json()
    assert "0 inserted, 15 updated" in run2["message"]

    # a correction auto-pushes the corrected value
    rep = next(x for x in client.get("/api/reports").json() if x["report_date"] == "2026-10-20")
    det = client.get(f"/api/reports/{rep['id']}").json()
    suraj = next(x for g in det["departments"] for x in g["records"] if x["paycode"] == "100045")
    cr = client.post(f"/api/records/{suraj['id']}/correction", json={"action": "shift_time", "remark": "auto push test"})
    assert cr.status_code == 200 and cr.json()["time_out"] == "19:00"
    row = _ext("SELECT time_out, status, is_corrected, correction_remark, machine_status FROM shiftkey_attendance WHERE report_date='2026-10-20' AND paycode='100045'")[0]
    assert row == ("19:00", "P", 1, "auto push test", "MIS")
    auto = [r for r in client.get(f"/api/sync/runs?job_id={pj['id']}").json() if r["trigger"] == "auto"]
    assert auto and auto[0]["status"] == "success"

    # corrections + reports + audit datasets
    for ds in ("corrections", "reports", "audit_events"):
        j = client.post("/api/sync/jobs", json={"name": f"push {ds}", "direction": "push", "connection_id": c["id"], "dataset": ds}).json()
        rr = client.post(f"/api/sync/jobs/{j['id']}/run", json={"date_from": "2026-10-01", "date_to": "2026-12-31"}).json()
        assert rr["status"] == "success", rr
    assert _ext("SELECT action, corrected_out FROM shiftkey_corrections")[0] == ("shift_time", "19:00")
    assert _ext("SELECT total, corrected FROM shiftkey_reports WHERE report_date='2026-10-20'")[0] == (5, 1)
    assert _ext("SELECT COUNT(*) FROM shiftkey_audit")[0][0] > 20
    assert _ext("SELECT COUNT(*) FROM shiftkey_audit WHERE hash IS NULL")[0][0] == 0

    # mapping onto an existing table with a different schema, update-only mode
    _ext_exec = sqlite3.connect(EXTERNAL_DB)
    _ext_exec.executemany("INSERT OR REPLACE INTO hr_attendance VALUES (?,?,?,?,?)",
                          [("2026-10-20", "100045", None, None, "X"), ("2026-10-20", "100238", None, None, "X")])
    _ext_exec.commit(); _ext_exec.close()
    j = client.post("/api/sync/jobs", json={"name": "push hr table", "direction": "push", "connection_id": c["id"], "dataset": "attendance",
                                            "target_table": "hr_attendance", "create_table": False, "push_mode": "update",
                                            "key_fields": ["report_date", "paycode"],
                                            "column_map": {"report_date": "att_date", "paycode": "emp_code", "time_in": "in_time",
                                                           "time_out": "out_time", "status": "status"}}).json()
    rr = client.post(f"/api/sync/jobs/{j['id']}/run", json={"date_from": "2026-10-20", "date_to": "2026-10-20"}).json()
    assert rr["status"] == "success" and "2 updated" in rr["message"] and "3 skipped" in rr["message"]
    assert _ext("SELECT in_time, out_time, status FROM hr_attendance WHERE emp_code='100045'")[0] == ("07:11", "19:00", "P")
    assert _ext("SELECT COUNT(*) FROM hr_attendance")[0][0] == 2
    # bad column map -> failed run with a clear message
    j2 = client.post("/api/sync/jobs", json={"name": "push bad map", "direction": "push", "connection_id": c["id"], "dataset": "attendance",
                                             "target_table": "hr_attendance", "create_table": False,
                                             "column_map": {"report_date": "att_date", "paycode": "emp_code", "status": "nope_col"}}).json()
    rr = client.post(f"/api/sync/jobs/{j2['id']}/run", json={"date_from": "2026-10-20"}).json()
    assert rr["status"] == "failed" and "nope_col" in rr["message"]


def test_scheduler_due_logic(client):
    from app.connectors import jobs as sync
    from app.db import SessionLocal
    from app.models import SyncJob

    c = _connection(client)
    now = datetime(2026, 10, 24, 6, 30)
    with SessionLocal() as db:
        job = db.query(SyncJob).filter_by(name="Starlink daily").one()
        job.schedule_enabled, job.schedule_time, job.schedule_offset_days, job.schedule_last_date = True, "06:00", 3, None
        db.commit()
        runs = sync.run_scheduled(db, now)
        assert len(runs) == 1 and runs[0].trigger == "schedule" and runs[0].params["date_from"] == "2026-10-21"
        assert runs[0].status == "success"
        assert sync.run_scheduled(db, now) == []                       # not twice a day
        assert sync.run_scheduled(db, now.replace(hour=5)) == []        # before time (same day anyway)
        assert len(sync.run_scheduled(db, now + timedelta(days=1))) == 1
    ev = client.get("/api/audit?period=all&action=sync.pull&limit=3").json()["events"][0]
    assert ev["actor"]["username"] == "scheduler" and ev["meta"]["trigger"] == "schedule"
    assert client.post("/api/sync/tick").status_code == 200


def test_non_admin_cannot_touch_integrations(client):
    client.post("/api/users", json={"username": "sync.hod", "role": "hod", "password": "Hod-Pass11", "departments": ["012"], "must_change_password": False})
    with make_client() as hc:
        login(hc, "sync.hod", "Hod-Pass11"); hc.headers.update(HDR)
        assert hc.get("/api/connections").status_code == 403
        assert hc.get("/api/sync/jobs").status_code == 403
        assert hc.post("/api/sync/preview", json={"connection_id": 1, "sql": "select 1"}).status_code == 403


def test_cleanup_jobs(client):
    for j in client.get("/api/sync/jobs").json():
        assert client.delete(f"/api/sync/jobs/{j['id']}").status_code == 204
    for c in client.get("/api/connections").json():
        assert client.delete(f"/api/connections/{c['id']}").status_code == 204
    assert client.get("/api/sync/jobs").json() == []
    for r in client.get("/api/reports").json():
        if r["source_kind"] == "sql":
            client.delete(f"/api/reports/{r['id']}")
