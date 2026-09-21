"""pytest 共用：每個測試模組用自己的 /tmp 資料庫，不碰正式資料。"""
import os, sys, re, importlib
from typing import ClassVar

import pytest

os.environ.update(ATTEND_SECRET="pytest-secret", ATTEND_TEACHER_PASSWORD="pw", ATTEND_BASE_URL="http://test")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture()
def A(tmp_path):
    os.environ["ATTEND_DB"] = str(tmp_path / "t.db")
    import app
    importlib.reload(app)  # 重新載入讓 DB_PATH 指到這次的暫存庫
    return app


@pytest.fixture()
def client(A):
    from fastapi.testclient import TestClient
    return TestClient(A.app, base_url="http://test")


@pytest.fixture()
def teacher(client):
    r = client.post("/t/login", data={"password": "pw", "next": "/t"}, follow_redirects=False)
    assert r.status_code == 303
    return client


@pytest.fixture()
def seeded(A):
    """三門課＋名冊；回傳 {code: course_id}。"""
    with A.db() as con:
        for code, name in (("CD", "脈絡設計"), ("DT", "設計思考"), ("OOP", "物件導向")):
            con.execute("INSERT INTO courses(code,name) VALUES(?,?)", (code, name))
        cid = {r["code"]: r["id"] for r in con.execute("SELECT id, code FROM courses")}
        for sid in ("9994A001", "9994A002", "9994A003", "9994A004"):
            con.execute("INSERT INTO students(course_id,student_id,name,cls) VALUES(?,?,?,?)", (cid["CD"], sid, "學生" + sid[-1], "創科一甲"))
        for sid in ("9994A001", "1153A009"):
            con.execute("INSERT INTO students(course_id,student_id,name,cls) VALUES(?,?,?,?)", (cid["DT"], sid, "學生" + sid[-1], ""))
        for sid in ("999000025", "999000026"):
            con.execute("INSERT INTO students(course_id,student_id,name,cls) VALUES(?,?,?,?)", (cid["OOP"], sid, "清大" + sid[-1], ""))
    return cid


class Helpers:
    def __init__(self, A, client, cid):
        self.A, self.T, self.cid = A, client, cid

    def student(self):
        from fastapi.testclient import TestClient
        return TestClient(self.A.app, base_url="http://test")

    _devs: ClassVar[dict] = {}

    def dev(self, name):
        """一個「手機」＝一個保留 cookie 的 client；第一次會先開 /c 拿伺服器下發的裝置識別。"""
        key = (id(self.A), name)
        if key not in self._devs:
            cl = self.student(); cl.get("/c"); self._devs[key] = cl
        return self._devs[key]

    def open(self, course, kind="normal"):
        r = self.T.post(f"/t/course/{self.cid[course]}/open", data={"kind": kind}, follow_redirects=False)
        return int(r.headers["location"].rsplit("/", 1)[1])

    def grant(self, sid, client=None):
        code = self.T.get(f"/api/t/session/{sid}/qr").json()["code"]
        html = (client or self.student()).post("/c", data={"code": code}).text
        return re.search(r'GRANT="([^"]+)"', html).group(1)

    def checkin(self, sid, dev, stu, name="", pin="1234", pin2=None, ua="iPhone", ip="140.131.1.1", hints="h", grant=None):
        cl = self.dev(dev)
        return cl.post("/api/checkin", data={"grant": grant or self.grant(sid, cl), "student_id": stu, "name": name,
                                             "pin": pin, "pin2": pin if pin2 is None else pin2, "hints": hints},
                       headers={"user-agent": ua, "cf-connecting-ip": ip}).json()

    def state(self, sid): return self.T.get(f"/api/t/session/{sid}/state").json()
    def row(self, sid, stu): return next(x for x in self.state(sid)["rows"] if x["student_id"] == stu)
    def pk(self, course, stu):
        with self.A.db() as con:
            return con.execute("SELECT id FROM students WHERE course_id=? AND student_id=?", (self.cid[course], stu)).fetchone()["id"]
    def db_rows(self, sid):
        with self.A.db() as con:
            return {r["student_id"]: dict(r) for r in con.execute("SELECT st.student_id, k.* FROM checkins k JOIN students st ON st.id=k.student_pk WHERE k.session_id=?", (sid,))}


@pytest.fixture()
def h(A, teacher, seeded):
    return Helpers(A, teacher, seeded)
