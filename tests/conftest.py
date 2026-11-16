import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_tmp = tempfile.mkdtemp(prefix="shiftkey-test-")
os.environ["SHIFTKEY_DATABASE_URL"] = f"sqlite:///{_tmp}/test.db"
os.environ["SHIFTKEY_ADMIN_PASSWORD"] = "Admin-Pass1"
os.environ["SHIFTKEY_DATA_DIR"] = _tmp
os.environ["SHIFTKEY_SCHEDULER_ENABLED"] = "false"
os.environ["SHIFTKEY_SYNC_ASYNC"] = "false"
EXTERNAL_DB = os.path.join(_tmp, "starlink.db")

import pytest  # noqa: E402

DATASET = os.path.join(ROOT, "dataset")
RAW = os.path.join(DATASET, "daily format.xlsx")
FORMATTED = os.path.join(DATASET, "21 October 2026 Expatriates.xlsx")
HDR = {"X-Requested-With": "ShiftKey"}


@pytest.fixture(scope="session")
def raw_bytes():
    with open(RAW, "rb") as fh:
        return fh.read()


@pytest.fixture(scope="session")
def formatted_bytes():
    with open(FORMATTED, "rb") as fh:
        return fh.read()


def make_client():
    from fastapi.testclient import TestClient

    from app.main import app

    return TestClient(app)


def login(client, username, password):
    r = client.post("/api/auth/login", json={"username": username, "password": password}, headers=HDR)
    assert r.status_code == 200, r.text
    return r.json()


@pytest.fixture()
def anon():
    with make_client() as c:
        yield c


@pytest.fixture()
def client():
    """Signed-in admin client."""
    with make_client() as c:
        login(c, "admin", "Admin-Pass1")
        c.headers.update(HDR)
        yield c
