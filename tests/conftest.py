"""pytest 共用：每個測試模組用自己的 /tmp 資料庫，不碰正式資料。"""
import hashlib
import os, sys, re, importlib, tempfile, threading, time

import pytest

os.environ.update(ATTEND_SECRET="pytest-secret", ATTEND_TEACHER_PASSWORD="pw", ATTEND_BASE_URL="http://test")
# 任何測試在 A fixture 之前 import app，DB_PATH 會走預設、在工作樹建出 data/attend.db；
# 部署 rsync 的目的地 ~/server/attend/ 正是正式庫所在目錄。先給一個暫存路徑墊底，fixture 照舊各自覆寫。
os.environ.setdefault("ATTEND_DB", os.path.join(tempfile.gettempdir(), "attend-pytest-default.db"))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# mutmut 底下（它在每個階段都會設 MUTANT_UNDER_TEST）不 reload。突變版 app.py 把全部突變塞進同一檔、
# 四十幾 MB，每個測試 reload 一次光收集統計就兩小時以上；突變的切換靠 mutmut 的 trampoline 每次呼叫時讀環境變數，
# 本來就不用 reload。改成：照模組載入時建好（含遷移）的那個庫的 schema 建新庫、換 DB_PATH，再把模組變數還原成
# 第一次見到的樣子——測試會直接改 A.DISCORD_TOKEN、A.now，限流、事件 backlog、通知去重也都是模組層的字典。
# 快照存在 app 模組上，不放 conftest 的模組變數。pytest 9 每次 pytest.main 都重新 import conftest（模組變數歸零），
# app 卻一直留在 sys.modules；mutmut 在同一個行程先跑統計、乾淨測試，再分叉出每個突變的子行程，子行程裡的快照
# 會從「上一輪最後一個測試」的 app 重拍：DB_PATH 指向的暫存目錄被 pytest 清掉後全部 setup error，突變一律記成被殺。
_SNAP = "__conftest_snapshot__"


def _reset_in_place(app, path):
    import collections, copy, sqlite3
    g = vars(app)
    if _SNAP not in g:  # 這個行程第一次見到 app：剛載入，模組變數都還是原樣
        with sqlite3.connect(app.DB_PATH) as src:
            schema = [r[0] for r in src.execute(
                "SELECT sql FROM sqlite_master WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%' ORDER BY type='index', rowid")]
        # 雙底線開頭的不碰：`__builtins__` 本身就是一個字典，清掉它等於拔掉所有內建函式
        g[_SNAP] = (schema, {k: (v, copy.copy(v) if isinstance(v, (dict, list, set, collections.deque)) else None)
                             for k, v in g.items() if not k.startswith("__")})
    _schema_sql, _pristine = g[_SNAP]
    for k in [k for k in g if k not in _pristine and not k.startswith("__")]:
        del g[k]
    for k, (obj, saved) in _pristine.items():
        if saved is not None:
            obj.clear()
            obj.update(saved) if isinstance(obj, (dict, set)) else obj.extend(saved)
        g[k] = obj
    if app.DB_PATH == path:  # 第一個測試才 import app：模組載入時已經在這個路徑建好表
        return
    app.DB_PATH = path
    con = sqlite3.connect(path)
    for s in _schema_sql:
        con.execute(s)
    con.commit()
    con.close()


# 偶發 ERROR 的成因：結束點名時 app 起的 Discord 背景執行緒（daemon、沒人 join）活過自己的測試，
# 下一個測試 reload／重設 app 之後它才跑，讀到的是新測試的 DB_PATH 與新的模組全域：輕則把事件寫進別人的庫、丟
# `no such table`，重則新庫切 WAL 時被鎖住，下一個測試 setup 階段 `database is locked`（mutmut 帶 -x，
# 任何一個 ERROR 都把當下的突變記成被殺）。所以重設 app 之前、以及每個測試結束時，都等它們結束；逾時就讓測試失敗並印出執行緒名稱。
# 逾時秒數可用 ATTEND_TEST_BG_JOIN 覆寫（只有 test_harness 測逾時路徑時用）。
BG_JOIN_SECONDS = float(os.environ.get("ATTEND_TEST_BG_JOIN", "5"))


def _discord_bg_threads():
    """app 用 threading.Thread(target=lambda: _bg_discord_post(...)) 起的執行緒：認 target 的程式碼用到 _bg_discord_post。"""
    out = []
    for t in threading.enumerate():
        code = getattr(getattr(t, "_target", None), "__code__", None)
        if t is not threading.current_thread() and t.is_alive() and code is not None and "_bg_discord_post" in code.co_names:
            out.append(t)
    return out


def wait_discord_bg_threads(where):
    deadline = time.monotonic() + BG_JOIN_SECONDS
    for t in _discord_bg_threads():
        t.join(max(0.0, deadline - time.monotonic()))
    left = _discord_bg_threads()
    if left:
        pytest.fail(f"{where}：Discord 背景執行緒 {BG_JOIN_SECONDS:g} 秒內沒有結束：{[t.name for t in left]}", pytrace=False)


@pytest.fixture()
def A(tmp_path):
    wait_discord_bg_threads("測試開始前（重設 app 之前）")  # 上一個測試漏下的（沒用 A 的測試、或 teardown 被跳過）也收乾淨
    os.environ["ATTEND_DB"] = str(tmp_path / "t.db")
    import app
    if "MUTANT_UNDER_TEST" in os.environ:
        _reset_in_place(app, os.environ["ATTEND_DB"])
    else:
        importlib.reload(app)  # 重新載入讓 DB_PATH 指到這次的暫存庫
    return app


@pytest.hookimpl(wrapper=True)
def pytest_runtest_call(item):
    """測試本體一跑完就收（比所有 fixture 的 teardown 都早，monkeypatch 還沒還原，背景執行緒呼叫的還是測試換上的假函式）；
    逾時算在起執行緒的這個測試頭上。"""
    result = yield
    wait_discord_bg_threads("測試本體結束時")
    return result


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
        # 每個測試一份（h 是 function 級 fixture）。原本是 ClassVar、鍵用 id(app)，reload 後 id 不變，
        # 第二個測試用同樣的裝置名會拿到第一個測試那支手機與它的 cookie
        self._devs: dict = {}

    def student(self):
        from fastapi.testclient import TestClient
        return TestClient(self.A.app, base_url="http://test")

    def dev(self, name):
        """一個「手機」＝一個保留 cookie 的 client；第一次會先開 /c 拿伺服器下發的裝置識別。"""
        key = name
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

    def checkin(self, sid, dev, stu, name="", pin="1234", pin2=None, ua=None, ip="140.131.1.1", hints="h", grant=None):
        # 伺服器指紋＝IP＋UA＋語言，同指紋的另一個學號會被轉待確認。現實裡不同學生是不同手機，
        # 所以預設每個模擬裝置有自己的 UA（「iPhone/裝置名的雜湊」）；要兩個裝置同指紋，測試明確傳同一個 ua 與 ip
        ua = ua if ua is not None else "iPhone/" + hashlib.sha1(dev.encode()).hexdigest()[:8]
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
