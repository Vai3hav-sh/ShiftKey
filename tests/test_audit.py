"""Audit trail: coverage, scoping, hash chain, history endpoints."""
import io

from conftest import HDR, login, make_client


def _upload(client, data, name):
    return client.post("/api/reports/upload", files={"file": (name, data, "application/octet-stream")})


def _events(client, **params):
    params.setdefault("period", "all")
    params.setdefault("include_views", True)
    params.setdefault("limit", 500)
    r = client.get("/api/audit", params=params)
    assert r.status_code == 200, r.text
    return r.json()["events"]


def test_startup_and_auth_events(anon):
    login(anon, "admin", "Admin-Pass1")
    anon.headers.update(HDR)
    actions = [e["action"] for e in _events(anon)]
    assert "system.startup" in actions and "auth.login.success" in actions
    # failed login is recorded with the attempted username, outcome failure
    anon.post("/api/auth/login", json={"username": "ghost", "password": "nope"})
    ev = _events(anon, action="auth.login.failed")[0]
    assert ev["actor"]["username"] == "ghost" and ev["actor"]["id"] is None and ev["outcome"] == "failure"
    assert ev["meta"]["reason"] == "unknown user" and ev["target"]["label"] == "ghost"
    assert ev["ip"] and ev["request_id"] and ev["hash"] and ev["category"] == "auth"


def test_denied_requests_are_audited(client, anon):
    # anonymous hit on a protected endpoint -> access.denied (401)
    anon.get("/api/reports")
    # a signed-in HOD hitting an admin endpoint -> access.denied (403)
    client.post("/api/users", json={"username": "aud.hod", "role": "hod", "password": "Hod-Pass11",
                                    "departments": ["001"], "must_change_password": False})
    with make_client() as hc:
        login(hc, "aud.hod", "Hod-Pass11"); hc.headers.update(HDR)
        assert hc.get("/api/users").status_code == 403
    denied = _events(client, action="access.denied")
    assert any(e["actor"]["username"] is None and "/api/reports" in e["summary"] for e in denied)
    hod_denied = next(e for e in denied if e["actor"]["username"] == "aud.hod")
    assert hod_denied["outcome"] == "denied" and hod_denied["meta"]["status"] == 403
    # the HOD can see their own denied event, but not the admin's sign-in
    with make_client() as hc:
        login(hc, "aud.hod", "Hod-Pass11"); hc.headers.update(HDR)
        mine = _events(hc)
        bad = [e for e in mine if not (e["actor"]["username"] in ("aud.hod", None) or e["category"] in ("report", "correction"))]
        assert not bad, bad[:3]
        assert any(e["action"] == "access.denied" for e in mine)
        assert not any(e["action"] == "auth.login.success" and e["actor"]["username"] == "admin" for e in mine)
        assert hc.post("/api/audit/verify").status_code == 403
        assert hc.get("/api/audit/export").status_code == 403


def test_full_lifecycle_is_traceable(client, raw_bytes):
    rid = _upload(client, raw_bytes, "daily format.xlsx").json()["id"]
    up = _events(client, action="report.uploaded", report_id=rid)[0]
    assert up["after"]["total"] == 172 and len(up["after"]["source_sha256"]) == 64
    assert up["target"] == {"type": "report", "id": str(rid), "label": "22 Oct 2026"}

    # HOD for FURNACE resolves a record, then reverts; HR local sees neither (segment indian)
    client.post("/api/users", json={"username": "trace.hod", "full_name": "Trace Head", "role": "hod",
                                    "password": "Hod-Pass11", "departments": ["012"], "must_change_password": False})
    client.post("/api/users", json={"username": "trace.hr", "role": "hr", "segment": "local",
                                    "password": "Hr-Pass111", "must_change_password": False})
    with make_client() as hc:
        login(hc, "trace.hod", "Hod-Pass11"); hc.headers.update(HDR)
        det = hc.get(f"/api/reports/{rid}").json()
        hira = next(r for d in det["departments"] for r in d["records"] if r["paycode"] == "100238")
        assert hc.post(f"/api/records/{hira['id']}/correction", json={"action": "shift_time", "remark": "ok"}).status_code == 200
        assert hc.post(f"/api/records/{hira['id']}/correction", json={"action": "set_time", "field": "out", "time": "99:99"}).status_code == 400
        assert hc.delete(f"/api/records/{hira['id']}/correction").status_code == 200

    applied = _events(client, action="correction.applied", target=f"record:{hira['id']}", report_id=rid)
    ok = [e for e in applied if e["outcome"] == "success"][0]
    bad = [e for e in applied if e["outcome"] == "failure"][0]
    assert ok["before"]["time_out"] == "07:19" and ok["after"]["time_out"] == "19:00"
    assert ok["changes"] == [{"field": "time_out", "from": "07:19", "to": "19:00"},
                             {"field": "hrs", "from": "26.12", "to": "13.53"},
                             {"field": "corrected", "from": False, "to": True}]
    assert ok["dept_code"] == "012" and ok["segment"] == "indian" and ok["paycode"] == "100238"
    assert "Trace Head resolved HIRA RAJBHAR" in ok["summary"] and '"ok"' in ok["summary"]
    assert "99:99" in bad["summary"] or bad["meta"]["time"] == "99:99"
    reverted = _events(client, action="correction.reverted", target=f"record:{hira['id']}", report_id=rid)[0]
    assert reverted["after"]["time_out"] == "07:19"

    # record history endpoint tells the whole story of one row
    hist = client.get(f"/api/records/{hira['id']}/history").json()
    assert hist["imported"]["action"] == "report.uploaded"
    assert [c["action"] for c in hist["corrections"]] == ["shift_time", "revert"]
    assert [e["action"] for e in hist["events"]] == ["correction.applied", "correction.applied", "correction.reverted"]
    assert hist["report"]["source_sha256"] == up["after"]["source_sha256"]
    assert hist["other_days"][0]["record_id"] == hira["id"]

    # HR local: segment scope hides the furnace corrections (all indian)
    with make_client() as hr:
        login(hr, "trace.hr", "Hr-Pass111"); hr.headers.update(HDR)
        assert not any(e["category"] == "correction" for e in _events(hr))
        assert any(e["action"] == "report.uploaded" for e in _events(hr))  # lifecycle visible to all

    # exports, views and source downloads are logged with their filters
    client.get(f"/api/reports/{rid}/export?format=csv&only=pending&segment=indian")
    client.get(f"/api/reports/{rid}/source")
    exp = _events(client, action="report.exported", report_id=rid)[0]
    assert exp["meta"]["format"] == "csv" and exp["meta"]["only"] == "pending" and exp["meta"]["rows"] == 6
    assert len(exp["meta"]["sha256"]) == 64
    assert _events(client, action="report.source.downloaded")[0]["meta"]["sha256"] == up["after"]["source_sha256"]
    assert any(e["actor"]["username"] == "trace.hod" for e in _events(client, action="report.viewed", report_id=rid))
    rh = client.get(f"/api/reports/{rid}/history").json()
    assert {e["action"] for e in rh} >= {"report.uploaded", "correction.applied", "report.exported", "report.viewed"}

    # replace keeps a snapshot of what was there; delete too
    _upload(client, raw_bytes, "daily format.xlsx")
    rep = _events(client, action="report.replaced")[0]
    assert rep["before"]["total"] == 172 and rep["before"]["corrections"] == 0
    new_id = client.get("/api/reports").json()[0]["id"]
    client.delete(f"/api/reports/{new_id}")
    de = _events(client, action="report.deleted")[0]
    assert de["severity"] == "critical" and de["before"]["source_filename"] == "daily format.xlsx"

    # user management: diff recorded
    uid = client.get("/api/users").json()
    hod_id = next(u["id"] for u in uid if u["username"] == "trace.hod")
    client.patch(f"/api/users/{hod_id}", json={"departments": ["012", "013"], "is_active": False, "password": "New-Pass22"})
    upd = _events(client, action="user.updated", target=f"user:{hod_id}")[0]
    assert {"field": "departments", "from": ["012"], "to": ["012", "013"]} in upd["changes"]
    assert _events(client, action="user.disabled", target=f"user:{hod_id}")
    assert _events(client, action="user.password.reset", target=f"user:{hod_id}")
    client.delete(f"/api/users/{hod_id}")
    assert _events(client, action="user.deleted")[0]["before"]["username"] == "trace.hod"

    # shifts
    client.put("/api/shifts/M07", json={"end": "19:30"})
    sh = _events(client, action="shift.updated")[0]
    assert sh["changes"] == [{"field": "end", "from": "19:00", "to": "19:30"}]
    client.put("/api/shifts/M07", json={"end": "19:00"})

    # stats / actors / actions / filters
    st = client.get("/api/audit/stats?period=all").json()
    assert st["corrections"] >= 2 and st["exports"] >= 2 and st["total_events"] > 20
    assert any(a["username"] == "trace.hod" for a in client.get("/api/audit/actors").json())
    assert any(a["action"] == "correction.applied" for a in client.get("/api/audit/actions").json()["actions"])
    assert all(e["category"] == "export" for e in _events(client, category="export"))
    assert all("HIRA" in e["summary"] or e["paycode"] == "100238" for e in _events(client, q="HIRA"))
    page = client.get("/api/audit?period=all&include_views=true&limit=5").json()
    assert len(page["events"]) == 5 and page["has_more"] and page["next_before_id"] == page["events"][-1]["id"]
    page2 = client.get(f"/api/audit?period=all&include_views=true&limit=5&before_id={page['next_before_id']}").json()
    assert page2["events"][0]["id"] < page["events"][-1]["id"]


def test_chain_verification_and_tamper_detection(client):
    r = client.post("/api/audit/verify")
    assert r.status_code == 200 and r.json()["ok"] is True and r.json()["checked"] > 5
    verified = _events(client, action="audit.verified")[0]
    assert "intact" in verified["summary"]

    # tamper with an old event directly in the database -> detected
    from sqlalchemy import select, update

    from app.db import SessionLocal
    from app.models import AuditEvent

    with SessionLocal() as db:
        victim = db.scalar(select(AuditEvent).where(AuditEvent.action == "auth.login.failed").order_by(AuditEvent.id))
        vid = victim.id
        original = victim.summary
        db.execute(update(AuditEvent).where(AuditEvent.id == vid).values(summary="nothing happened here"))
        db.commit()
    r = client.post("/api/audit/verify").json()
    assert r["ok"] is False and r["broken_at"] == vid and "modified" in r["reason"]
    with SessionLocal() as db:
        db.execute(update(AuditEvent).where(AuditEvent.id == vid).values(summary=original))
        db.commit()
    # deleting an event breaks the link of the next one
    with SessionLocal() as db:
        db.execute(AuditEvent.__table__.delete().where(AuditEvent.id == vid))
        db.commit()
    r = client.post("/api/audit/verify").json()
    assert r["ok"] is False and "removed" in r["reason"]


def test_audit_export_formats(client):
    r = client.get("/api/audit/export?format=csv&period=all")
    assert r.status_code == 200 and "ShiftKey audit" in r.headers["content-disposition"]
    lines = r.text.splitlines()
    assert lines[0].startswith("﻿id,timestamp_utc,actor") and len(lines) > 10
    r = client.get("/api/audit/export?format=json&period=all&category=audit")
    body = r.json()
    assert body["exported_by"] == "admin" and all(e["category"] == "audit" for e in body["events"])
    assert _events(client, action="audit.exported")[0]["meta"]["format"] == "json"


def test_sessions_admin(client):
    with make_client() as other:
        login(other, "admin", "Admin-Pass1"); other.headers.update(HDR)
        sessions = [s for s in client.get("/api/sessions").json() if s["username"] == "admin"]
        assert len(sessions) >= 2 and all(s["ip"] for s in sessions)
        assert other.get("/api/me").status_code == 200
        # the newest admin session belongs to `other`; revoking it signs that client out
        victim = sessions[0]
        assert client.delete(f"/api/sessions/{victim['id']}").status_code == 204
        assert other.get("/api/me").status_code == 401
        ev = _events(client, action="auth.session.revoked")[0]
        assert ev["target"]["label"] == "admin" and ev["meta"]["session_ip"]
