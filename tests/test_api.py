import io

import openpyxl

from conftest import HDR, login, make_client


def _upload(client, data, name):
    return client.post("/api/reports/upload", files={"file": (name, data, "application/octet-stream")})


def test_health_and_config_public(anon):
    assert anon.get("/api/health").json()["database"] is True
    assert anon.get("/api/config").json()["segment_prefixes"] == {"1": "indian", "2": "local"}


def test_auth_required_and_csrf(anon, raw_bytes):
    assert anon.get("/api/reports").status_code == 401
    assert anon.get("/api/me").status_code == 401
    r = anon.post("/api/auth/login", json={"username": "admin", "password": "wrong"}, headers=HDR)
    assert r.status_code == 401
    login(anon, "admin", "Admin-Pass1")
    assert anon.get("/api/me").json()["role"] == "admin"
    # state-changing call without the custom header is refused
    r = anon.post("/api/reports/upload", files={"file": ("x.xlsx", raw_bytes)})
    assert r.status_code == 403
    anon.post("/api/auth/logout", headers=HDR)
    assert anon.get("/api/me").status_code == 401


def test_upload_list_get_export_delete(client, raw_bytes, formatted_bytes):
    r = _upload(client, raw_bytes, "daily format.xlsx")
    assert r.status_code == 201, r.text
    detail = r.json()
    assert detail["report_date"] == "2026-10-22" and detail["replaced"] is False
    assert detail["summary"]["total"] == 172 and detail["summary"]["mispunch"] == 2
    assert detail["summary"]["pending"] == 6  # 2 MIS + 4 long spans
    assert detail["summary"]["by_segment"] == {"indian": 172}
    rid = detail["id"]

    r2 = _upload(client, formatted_bytes, "21 October 2026 Expatriates.xlsx")
    assert r2.status_code == 201
    listing = client.get("/api/reports").json()
    assert [x["report_date"] for x in listing] == ["2026-10-22", "2026-10-21"]
    assert listing[0]["pending"] == 6 and listing[0]["corrected"] == 0

    r3 = _upload(client, raw_bytes, "daily format.xlsx")
    assert r3.status_code == 201 and r3.json()["replaced"] is True
    rid = r3.json()["id"]

    got = client.get(f"/api/reports/{rid}").json()
    assert got["summary"]["halfday"] == 3
    assert got["departments"][0]["records"][0]["can_correct"] is True
    assert got["departments"][0]["records"][0]["shift_end"] == "19:00"

    x = client.get(f"/api/reports/{rid}/export?format=xlsx")
    assert x.status_code == 200
    assert "22 October 2026 Expatriates.xlsx" in x.headers["content-disposition"]
    wb = openpyxl.load_workbook(io.BytesIO(x.content))
    assert wb.worksheets[0]["C3"].value.endswith("22/10/2026")

    flagged = client.get(f"/api/reports/{rid}/export?format=xlsx&only=flagged&summary=false")
    ws = openpyxl.load_workbook(io.BytesIO(flagged.content)).worksheets[0]
    assert len([r for r in ws.iter_rows(min_row=6) if r[1].value]) == 9

    seg = client.get(f"/api/reports/{rid}/export?format=xlsx&segment=local&summary=false")
    assert "Expatriates Local" in seg.headers["content-disposition"]

    assert client.get(f"/api/reports/{rid}/export?format=csv").status_code == 200
    assert client.get(f"/api/reports/{rid}/source").content == raw_bytes

    # departments & shifts learnt from the import
    depts = client.get("/api/departments").json()
    assert any(d["code"] == "012" and d["name"] == "FURNACE" and d["headcount"] == 29 for d in depts)
    shifts = {s["code"]: s for s in client.get("/api/shifts").json()}
    assert shifts["M07"]["end"] == "19:00" and shifts["N19"]["is_night"] is True

    assert client.delete(f"/api/reports/{rid}").status_code == 204
    assert client.get(f"/api/reports/{rid}").status_code == 404


def test_upload_rejects_non_report(client):
    r = _upload(client, b"a,b,c\n1,2,3\n", "random.csv")
    assert r.status_code == 422
    assert "header row" in r.json()["detail"].lower()


def test_users_roles_and_scoping(client, raw_bytes):
    rid = _upload(client, raw_bytes, "daily format.xlsx").json()["id"]

    # --- create a HOD for FURNACE + HOSPITAL, an HR (indian) and an HR (local)
    r = client.post("/api/users", json={"username": "Furnace.HOD", "full_name": "Furnace Head", "role": "hod",
                                        "password": "Hod-Pass11", "departments": ["012", "013"], "must_change_password": False})
    assert r.status_code == 201, r.text
    hod = r.json()
    assert hod["username"] == "furnace.hod" and hod["departments"] == ["012", "013"]
    assert client.post("/api/users", json={"username": "nohod", "role": "hod", "password": "Hod-Pass11", "departments": []}).status_code == 400
    assert client.post("/api/users", json={"username": "weak", "role": "hr", "password": "short", "segment": "indian"}).status_code == 400
    r = client.post("/api/users", json={"username": "hr.india", "full_name": "HR India", "role": "hr", "segment": "indian",
                                        "password": "Hr-Pass111", "must_change_password": False})
    assert r.status_code == 201
    r = client.post("/api/users", json={"username": "hr.local", "full_name": "HR Local", "role": "hr", "segment": "local",
                                        "password": "Hr-Pass111", "must_change_password": False})
    assert r.status_code == 201
    assert client.post("/api/users", json={"username": "hr.local", "role": "hr", "password": "Hr-Pass111"}).status_code == 409
    assert len(client.get("/api/users").json()) == 4

    # --- HOD view
    with make_client() as hc:
        me = login(hc, "furnace.hod", "Hod-Pass11")
        hc.headers.update(HDR)
        assert me["scope"]["departments"] == ["012", "013"] and me["scope"]["can_upload"] is False
        rep = next(x for x in hc.get("/api/reports").json() if x["id"] == rid)
        assert rep["total"] == 29 + 4 and rep["pending"] == 1 and rep["halfday"] == 1  # HIRA RAJBHAR long span
        det = hc.get(f"/api/reports/{rid}").json()
        assert sorted(d["code"] for d in det["departments"]) == ["012", "013"]
        assert det["summary"]["total"] == 33 and det["summary"]["halfday"] == 1
        assert hc.post("/api/users", json={}).status_code == 403
        assert hc.get("/api/users").status_code == 403
        assert _upload(hc, raw_bytes, "x.xlsx").status_code == 403
        assert hc.delete(f"/api/reports/{rid}").status_code == 403
        assert hc.get(f"/api/reports/{rid}/source").status_code == 403
        exp = hc.get(f"/api/reports/{rid}/export?format=csv")
        assert exp.status_code == 200 and exp.text.count("\n") == 34  # header + 33 rows

        # cannot touch a record outside own departments (CANTEEN mispunch)
        full = client.get(f"/api/reports/{rid}").json()
        canteen = next(r for d in full["departments"] if d["code"] == "006" for r in d["records"] if r["flag_mispunch"])
        assert hc.post(f"/api/records/{canteen['id']}/correction", json={"action": "shift_time"}).status_code == 404

        # resolve HIRA RAJBHAR (long span 05:07 -> 07:19, machine P) with shift end
        hira = next(r for d in det["departments"] for r in d["records"] if r["paycode"] == "100238")
        assert hira["needs_action"] and hira["can_correct"] and hira["shift_end"] == "19:00"
        r = hc.post(f"/api/records/{hira['id']}/correction", json={"action": "shift_time", "field": "out", "remark": "night out punch belongs to next day"})
        assert r.status_code == 200, r.text
        fixed = r.json()
        assert fixed["is_corrected"] and fixed["time_out"] == "19:00" and fixed["time_in"] == "05:07"
        assert fixed["hrs"] == "13.53" and fixed["status"] == "P" and not fixed["needs_action"]
        assert fixed["original"]["time_out"] == "07:19" and fixed["correction"]["by"] == "Furnace Head"

        # manual time on the hospital half day -> set out 20:00 with status back to P
        pooja = next(r for d in det["departments"] for r in d["records"] if r["paycode"] == "100305")
        r = hc.post(f"/api/records/{pooja['id']}/correction", json={"action": "set_time", "field": "out", "time": "20:00"})
        assert r.status_code == 200 and r.json()["hrs"] == "11.41" and r.json()["status"] == "P"
        assert hc.post(f"/api/records/{pooja['id']}/correction", json={"action": "set_time", "field": "out", "time": "25:00"}).status_code == 400

        # mark absent -> punches dropped
        suraj = next(r for d in det["departments"] for r in d["records"] if r["paycode"] == "100045")
        r = hc.post(f"/api/records/{suraj['id']}/correction", json={"action": "absent"})
        assert r.json()["status"] == "A" and r.json()["time_in"] is None and r.json()["is_absent"]

        det2 = hc.get(f"/api/reports/{rid}").json()
        assert det2["summary"]["corrected"] == 3 and det2["summary"]["pending"] == 0 and det2["summary"]["halfday"] == 0
        log = hc.get("/api/corrections").json()
        assert len(log) == 3 and log[0]["action"] == "absent" and log[-1]["by"] == "Furnace Head"

        # revert one
        r = hc.delete(f"/api/records/{suraj['id']}/correction")
        assert r.json()["is_corrected"] is False and r.json()["status"] == "P"
        assert len(hc.get("/api/corrections").json()) == 4
        assert next(x for x in hc.get("/api/reports").json() if x["id"] == rid)["corrected"] == 2

    # --- export reflects corrections
    x = client.get(f"/api/reports/{rid}/export?format=xlsx&only=corrected")
    wb = openpyxl.load_workbook(io.BytesIO(x.content))
    assert "Corrections" in wb.sheetnames
    rows = [r for r in wb.worksheets[0].iter_rows(min_row=6) if r[1].value]
    assert len(rows) == 2
    hira_row = next(r for r in rows if r[1].value == "100238")
    assert hira_row[4].value.strftime("%H:%M") == "19:00" and hira_row[5].font.color.rgb == "FF2B8A3E"

    # --- HR india sees everything (all paycodes start with 1) but cannot correct
    with make_client() as hr:
        login(hr, "hr.india", "Hr-Pass111"); hr.headers.update(HDR)
        det = hr.get(f"/api/reports/{rid}").json()
        assert det["summary"]["total"] == 172 and det["departments"][0]["records"][0]["can_correct"] is False
        assert hr.post(f"/api/records/{hira['id']}/correction", json={"action": "shift_time"}).status_code == 403
        assert len(hr.get("/api/corrections").json()) == 4
        exp = hr.get(f"/api/reports/{rid}/export")
        assert "Expatriates Indian" in exp.headers["content-disposition"]

    # --- HR local sees nothing in this file (no paycodes starting with 2)
    with make_client() as hr:
        login(hr, "hr.local", "Hr-Pass111"); hr.headers.update(HDR)
        assert all(x["id"] != rid for x in hr.get("/api/reports").json())
        assert hr.get(f"/api/reports/{rid}").status_code == 404

    # --- admin edits: deactivate HOD -> their session dies
    with make_client() as hc:
        login(hc, "furnace.hod", "Hod-Pass11"); hc.headers.update(HDR)
        assert hc.get("/api/me").status_code == 200
        r = client.patch(f"/api/users/{hod['id']}", json={"is_active": False})
        assert r.json()["is_active"] is False
        assert hc.get("/api/me").status_code == 401
    r = client.post("/api/auth/login", json={"username": "furnace.hod", "password": "Hod-Pass11"})
    assert r.status_code == 401

    # admin cannot deactivate / delete self
    me_id = client.get("/api/me").json()["id"]
    assert client.patch(f"/api/users/{me_id}", json={"is_active": False}).status_code == 400
    assert client.delete(f"/api/users/{me_id}").status_code == 400
    assert client.delete(f"/api/users/{hod['id']}").status_code == 204

    client.delete(f"/api/reports/{rid}")


def test_password_change_flow(client):
    r = client.post("/api/users", json={"username": "temp.hod", "role": "hod", "password": "Temp-Pass1",
                                        "departments": ["001"], "must_change_password": True})
    uid = r.json()["id"]
    with make_client() as c:
        me = login(c, "temp.hod", "Temp-Pass1"); c.headers.update(HDR)
        assert me["must_change_password"] is True
        assert c.get("/api/reports").status_code == 428          # blocked until changed
        assert c.post("/api/me/password", json={"current_password": "nope", "new_password": "New-Pass22"}).status_code == 400
        assert c.post("/api/me/password", json={"current_password": "Temp-Pass1", "new_password": "weak"}).status_code == 400
        r = c.post("/api/me/password", json={"current_password": "Temp-Pass1", "new_password": "New-Pass22"})
        assert r.status_code == 200 and r.json()["must_change_password"] is False
        assert c.get("/api/reports").status_code == 200
    with make_client() as c:
        login(c, "temp.hod", "New-Pass22")
    client.delete(f"/api/users/{uid}")


def test_shift_master_admin(client):
    r = client.put("/api/shifts/M09", json={"name": "Morning 09", "start": "09:00", "end": "21:00"})
    assert r.status_code == 200 and r.json()["source"] == "admin" and r.json()["is_night"] is False
    assert client.put("/api/shifts/M09", json={"end": "9pm"}).status_code == 422
    r = client.put("/api/shifts/N21", json={"start": "21:00", "end": "09:00"})
    assert r.json()["is_night"] is True
    assert client.delete("/api/shifts/N21").status_code == 204
    assert client.delete("/api/shifts/N21").status_code == 404
    assert client.get("/").status_code == 200


def test_env_admin_password_applies_until_first_login(client):
    from sqlalchemy import select

    from app import auth
    from app.config import settings
    from app.db import SessionLocal
    from app.models import User

    with SessionLocal() as db:
        admin = db.scalar(select(User).where(User.username == "admin"))
        # simulate a never-used admin created with a generated password
        admin.password_hash = auth.hash_password("Generated-1")
        admin.must_change_password = True
        admin.last_login_at = None
        db.commit()
        old = settings.admin_password
        settings.admin_password = "Chosen-Pass9"
        try:
            auth.ensure_admin(db)
        finally:
            settings.admin_password = old
        db.refresh(admin)
        assert auth.verify_password("Chosen-Pass9", admin.password_hash)
        assert admin.must_change_password is False
        # once signed in, env no longer overrides
        admin.last_login_at = auth.utcnow()
        admin.password_hash = auth.hash_password("User-Picked1")
        db.commit()
        settings.admin_password = "Chosen-Pass9"
        try:
            auth.ensure_admin(db)
        finally:
            settings.admin_password = old
        db.refresh(admin)
        assert auth.verify_password("User-Picked1", admin.password_hash)
        # restore for the other tests
        admin.password_hash = auth.hash_password("Admin-Pass1")
        admin.must_change_password = False
        db.commit()
