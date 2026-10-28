"""模型式狀態機測試：用隨機操作序列找出「已知分支覆蓋 97% 但實地表現很差」的違反不變式狀態。

不靠 conftest 的 function-scoped fixture（Hypothesis stateful 每個 example 都要自己重建全新環境），
自己做 conftest.A 同款的事：設 ATTEND_DB 再 importlib.reload(app)。時間用 time_machine 凍結＋shift()
前進（app.now() 讀的是 time.time()）。

Rules 對應 app.py：
  1 開一場點名          → POST /t/course/{cid}/open（do_open_session；同課同時只會有一場 live，重按回同一場）
  1b 開一場綁定場次      → 同上帶 kind=bind（已有一場種類不同的 live 時回 409）
  2 時間前進 1–400 秒    → time_machine shift
  3 首次簽到（自己手機） → POST /api/checkin，do_checkin 的 first_time 分支
  4 已綁定回簽           → POST /api/checkin，已綁裝置不驗 PIN；本場已有一列時走 already 早返
  5 錯 PIN               → POST /api/checkin，非首次分支的 PIN 比對失敗
  6 代簽／借手機         → POST /api/checkin，other／same 判定
  7 換手機               → POST /api/checkin，學號已綁別支手機回 409
  8 老師手動改狀態       → POST /api/t/session/{sid}/status（api_set_status）
  9 延長／結束           → POST .../extend（do_extend）、.../close（api_close）
  10 解除綁定            → POST /api/t/student/{pk}/unbind（api_unbind）
  11 切換 allow_bind     → POST /api/t/course/{cid}/bind（api_toggle_bind）
  12 抽點＋裁定          → POST .../spotcheck（api_spotcheck）、POST /api/t/spotcheck/{scid}（api_spot_result）

Invariants：
  I1 每（場次,學生）checkins 最多一列（schema UNIQUE 約束）
  I2 state／report-csv／api_me／DB 四處一致（inv_state_db_csv_me）
  I3 活性（直接在 checkin 呼叫當下斷言，不另開 invariant）
  I4 老師手動狀態不會被學生端動作改掉（inv_manual_status_preserved；proxy same_device 例外見下）
  I5 借他人手機簽自己時不得直接出席（rule_borrow_phone 呼叫當下斷言）
  I6 無 5xx（每次 HTTP 呼叫後即斷言）
  I7 PIN 連錯達上限鎖定（inv_lock_reflected）

已知假設／簡化：
  - pin_fail 的「device_hash=''」總計欄（PIN_MAX_FAIL_TOTAL）不建模，25 步內幾乎不會摸到，
    只建模逐裝置上限（pin_max_fail，預設 3）。
  - rule_borrow_phone 造成的 same_device 降級（do_checkin 把同一支手機本場先簽的那一列也轉待確認）
    會讓 A、B 當場的 checkins 列合法地偏離老師手動設定，故該次呼叫後把 (sid, A)、(sid, B) 標記為
    I4 豁免（manual_conflict_cleared）：同一支手機簽了兩個學號，兩個人都要老師當場確認。
    rule_spotcheck_decide 也一樣，老師的抽點裁定本來就可以覆寫老師自己先前設的狀態，
    不算被學生端動作改掉。
  - 「同一支手機本場已用在別的學號」時，即使是手機真正主人自己回簽，也會被轉成待確認
    （do_checkin 的 same 判定不分先後順序）；用 checkins.device_hash 實際比對，追蹤哪些人
    真的在本場用過這支手機（register_phone_use／others_used_phone）。
  - 目標學生本場已有一列（老師手動設定，或之前自己簽過）時，do_checkin 走 already 早返，
    狀態沿用那一列（I4）；手機有衝突時不寫 PIN、不綁裝置，只在那一列補標衝突旗標。
    這時回傳的狀態不是這次簽到判出來的，rule_first_checkin／rule_borrow_phone 遇到這種情況
    不斷言 I5／I3，改由檔尾的 test_borrowed_phone_keeps_manual_status_and_flags_conflict 單獨鎖住這條路徑。
"""
from __future__ import annotations

import csv
import io
import os
import re
import sys
import tempfile
import importlib

import time_machine
from hypothesis import HealthCheck, settings, strategies as st
from hypothesis.stateful import RuleBasedStateMachine, rule, invariant

os.environ.setdefault("ATTEND_SECRET", "pytest-secret")
os.environ.setdefault("ATTEND_TEACHER_PASSWORD", "pw")
os.environ.setdefault("ATTEND_BASE_URL", "http://test")
_SERVICE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SERVICE_DIR not in sys.path:
    sys.path.insert(0, _SERVICE_DIR)

N_EXAMPLES = int(os.environ.get("ATTEND_SM_EXAMPLES", "40"))
START_T = 1_700_000_000.0
STUDENTS = [f"S{i:02d}" for i in range(1, 7)]
NAMES = {sid: f"學生{i}" for i, sid in enumerate(STUDENTS, 1)}
DIGITS = "0123456789"


def _digits(min_size=4, max_size=6):
    return st.text(alphabet=DIGITS, min_size=min_size, max_size=max_size)


@settings(max_examples=N_EXAMPLES, stateful_step_count=25, deadline=None,
          suppress_health_check=list(HealthCheck))
class AttendStateMachine(RuleBasedStateMachine):

    def __init__(self):
        super().__init__()
        self.tmpdir = tempfile.TemporaryDirectory()
        os.environ["ATTEND_DB"] = os.path.join(self.tmpdir.name, "t.db")
        import app
        importlib.reload(app)
        self.app = app
        # hash_pin 用 PBKDF2 600_000 輪（密碼學上正確、刻意慢）：實測單次 ~78ms，
        # 25 步 × 40 example 若多數是簽到類 rule，光雜湊就會撞上 pyproject.toml 的 pytest timeout=120。
        # 不改 app.py，只在本測試行程內把「hash_pin」這個全域名字換成低輪數版本。do_checkin
        # 內部呼叫的是裸名 hash_pin(...)，Python 對模組全域名字是呼叫當下才查找，換掉
        # app.hash_pin 對它後續呼叫立即生效。雜湊的「相同輸入同雜湊、不同輸入大機率不同雜湊」
        # 性質不變，只是不需要真的扛密碼學等級的慢，不影響任何業務不變式。
        app.hash_pin = lambda pin, salt: app.hashlib.pbkdf2_hmac("sha256", pin.encode(), salt.encode(), 1000).hex()

        self.traveler_cm = time_machine.travel(START_T, tick=False)
        self.traveler = self.traveler_cm.start()
        self.t = START_T

        from fastapi.testclient import TestClient
        self.teacher = TestClient(app.app, base_url="http://test")
        r = self.teacher.post("/t/login", data={"password": "pw", "next": "/t"}, follow_redirects=False)
        assert r.status_code == 303

        with app.db() as con:
            con.execute("INSERT INTO courses(code,name) VALUES(?,?)", ("CD", "脈絡設計"))
            self.cid = con.execute("SELECT id FROM courses WHERE code='CD'").fetchone()["id"]
            self.pk_of = {}
            for sid in STUDENTS:
                con.execute("INSERT INTO students(course_id,student_id,name,cls) VALUES(?,?,?,?)",
                            (self.cid, sid, NAMES[sid], "創科一甲"))
            for r2 in con.execute("SELECT id, student_id FROM students WHERE course_id=?", (self.cid,)):
                self.pk_of[r2["student_id"]] = r2["id"]

        self.devices: dict[str, object] = {}
        self.session_ids: list[int] = []          # 最多保留最近 2 場
        self.model_pin: dict[str, str | None] = {s: None for s in STUDENTS}
        self.phone_of: dict[str, str | None] = {s: None for s in STUDENTS}     # 目前綁定的手機名
        self.owner_of_phone: dict[str, str | None] = {}                        # 手機 -> 目前擁有者學號
        self.fail_by_phone: dict[tuple, int] = {}                              # (sid,stu,phone) -> 連錯次數
        self.allow_bind = True
        self.manual_status: dict[tuple, str] = {}          # (sid,stu) -> 老師手動設定的狀態
        self.manual_conflict_cleared: set = set()           # 曾被 same_device 降級合法覆蓋，I4 不再強制
        self.session_phone_users: dict[tuple, set] = {}      # (sid,phone) -> 本場真的用這支手機簽過的學號集合（DB 驗證）
        self.pending_spotchecks: list[dict] = []
        self.bind_revoked: set = set()                      # (sid,stu)：綁定場次抽點判不在、綁定被撤回
        self._spare_ctr = 0

    def teardown(self):
        try:
            self.traveler_cm.stop()
        finally:
            self.tmpdir.cleanup()

    # 共用小工具

    def dev(self, name):
        if name not in self.devices:
            from fastapi.testclient import TestClient
            cl = TestClient(self.app.app, base_url="http://test")
            cl.get("/c")
            self.devices[name] = cl
        return self.devices[name]

    def spare_phone(self, tag):
        self._spare_ctr += 1
        return f"spare-{tag}-{self._spare_ctr}"

    def target_sid(self):
        return self.session_ids[-1] if self.session_ids else None

    def phase(self, sid):
        with self.app.db() as con:
            row = con.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
        return None if row is None else self.app.session_phase(row)

    def live_kind(self):
        """目前這門課是否有一場未結案場次，有的話回它的 kind，沒有回 None。
        用來判斷開新場次時該沿用（同種類）還是 409（種類不同，不沿用也不新建）。"""
        with self.app.db() as con:
            row = con.execute(
                "SELECT kind FROM sessions WHERE course_id=? AND closed_at IS NULL AND late_until>?",
                (self.cid, self.t)).fetchone()
        return row["kind"] if row else None

    def kind_of(self, sid):
        """綁定場次不論 allow_bind 一律允許首次綁定，多條 rule 的 403 期望要看場次種類。"""
        if sid is None:
            return "normal"
        with self.app.db() as con:
            row = con.execute("SELECT kind FROM sessions WHERE id=?", (sid,)).fetchone()
        return row["kind"] if row else "normal"

    def is_live(self, sid):
        if sid is None:
            return False
        with self.app.db() as con:
            row = con.execute("SELECT closed_at, late_until FROM sessions WHERE id=?", (sid,)).fetchone()
        return row is not None and row["closed_at"] is None and row["late_until"] > self.t

    def get_grant(self, phone_client, sid):
        r = self.teacher.get(f"/api/t/session/{sid}/qr")
        assert r.status_code == 200 and r.status_code < 500
        code = r.json()["code"]
        r2 = phone_client.post("/c", data={"code": code})
        assert r2.status_code < 500, f"5xx (I6): {r2.status_code} {r2.text}"
        if r2.status_code != 200:
            return None, r2
        m = re.search(r'GRANT="([^"]+)"', r2.text)
        if not m:
            return None, r2
        return m.group(1), r2

    def submit_checkin(self, phone_client, grant, stu, name, pin, pin2, seat=""):
        r = phone_client.post("/api/checkin", data={
            "grant": grant, "student_id": stu, "name": name, "pin": pin, "pin2": pin2,
            "seat": seat, "hints": "h",
        }, headers={"user-agent": "iPhone", "cf-connecting-ip": "140.131.1.1"})
        assert r.status_code < 500, f"5xx (I6, app.py api_checkin): {r.status_code} {r.text}"
        return r

    def attempt_checkin(self, phone_client, sid, stu, name, pin, pin2):
        """回傳 (kind, resp)；kind ∈ {no_session, no_live, rate_limited, checkin_resp}。"""
        if sid is None:
            r = phone_client.post("/c", data={"code": "000000"})
            assert r.status_code < 500
            return "no_session", r
        grant, r = self.get_grant(phone_client, sid)
        if grant is None:
            if r.status_code == 429:
                return "rate_limited", r
            assert r.status_code in (400, 410), f"預期沒有 live session 時是 400/410，實得 {r.status_code}: {r.text}"
            return "no_live", r
        r2 = self.submit_checkin(phone_client, grant, stu, name, pin, pin2)
        if r2.status_code == 429:
            return "rate_limited", r2
        return "checkin_resp", r2

    def is_locked(self, sid, stu, phone):
        return self.fail_by_phone.get((sid, stu, phone), 0) >= self.app.DEFAULT_SETTINGS["pin_max_fail"]

    def bump_fail(self, sid, stu, phone):
        self.fail_by_phone[(sid, stu, phone)] = self.fail_by_phone.get((sid, stu, phone), 0) + 1

    def clear_fail(self, sid, stu, phone):
        self.fail_by_phone[(sid, stu, phone)] = 0

    # DB-truth 輔助：與其自己猜側寫，直接回查資料庫，降低「測試自己預測錯」的風險

    def phone_dh(self, phone_client):
        key = phone_client.cookies.get("attend_dk", "")
        return self.app.hash_device(key)

    def existing_checkin(self, sid, stu):
        if sid is None:
            return None
        with self.app.db() as con:
            return con.execute("SELECT * FROM checkins WHERE session_id=? AND student_pk=?",
                                (sid, self.pk_of[stu])).fetchone()

    def checkin_row_device_matches(self, sid, stu, phone_client):
        row = self.existing_checkin(sid, stu)
        return bool(row) and row["device_hash"] == self.phone_dh(phone_client)

    def student_bound_to(self, stu, phone_client):
        with self.app.db() as con:
            row = con.execute("SELECT device_hash FROM students WHERE id=?", (self.pk_of[stu],)).fetchone()
        return row is not None and row["device_hash"] == self.phone_dh(phone_client)

    def student_has_pin(self, stu):
        """首次簽到遇到 other/same 衝突時 do_checkin 不寫 PIN（該學號仍是「首次」）。
        別靠側寫猜，直接回查資料庫是不是真的有 pin_hash。"""
        with self.app.db() as con:
            row = con.execute("SELECT pin_hash FROM students WHERE id=?", (self.pk_of[stu],)).fetchone()
        return row is not None and row["pin_hash"] is not None

    def bind_revoked_here(self, sid, stu):
        """綁定場次抽點判不在之後，這個學號本場在還沒綁上裝置前都不能再綁，do_checkin 回 403
        （bind_revoked 檢查排在 PIN 鎖定與 bind_closed 之後、PIN 比對之前）。要在送出前算，
        否則 app 若誤收，裝置已被綁上，事後回查就看不出來。"""
        if sid is None or (sid, stu) not in self.bind_revoked:
            return False
        with self.app.db() as con:
            row = con.execute("SELECT device_hash FROM students WHERE id=?", (self.pk_of[stu],)).fetchone()
        return row is not None and row["device_hash"] is None

    def clear_me_throttle(self, phone_client):
        """/api/me 的限流桶是行程內記憶體（_checkin_hits 裡的 "me:"+device_key），
        invariant 每步都打一次，長序列會自己撞到限流。這裡清掉限流表，429 仍然算失敗。"""
        dk = phone_client.cookies.get("attend_dk", "")
        if dk:
            self.app._checkin_hits.pop("me:" + dk, None)

    def sync_phone_binding(self, stu, phone_name, phone_client):
        if self.student_bound_to(stu, phone_client):
            self.phone_of[stu] = phone_name
            self.owner_of_phone[phone_name] = stu

    def register_phone_use(self, sid, phone_name, stu, phone_client):
        """只有『這一列 checkins 的 device_hash 真的等於這支手機』才算數（DB 驗證，
        排除 manual 列 device_hash='' 或 already-shortcut 沒寫入的情況）。"""
        if self.checkin_row_device_matches(sid, stu, phone_client):
            self.session_phone_users.setdefault((sid, phone_name), set()).add(stu)

    def others_used_phone(self, sid, phone_name, stu):
        return bool(self.session_phone_users.get((sid, phone_name), set()) - {stu})

    # Rule 1: 開一場點名

    @rule()
    def rule_open_session(self):
        before_kind = self.live_kind()
        r = self.teacher.post(f"/t/course/{self.cid}/open", follow_redirects=False)
        assert r.status_code < 500
        if before_kind is not None and before_kind != "normal":
            # 已有一場未結案場次而種類不同，不沿用也不新建，回 409
            assert r.status_code == 409, f"種類不同時應 409（app.py do_open_session）: {r.status_code} {r.text}"
            return
        assert r.status_code == 303, r.text
        sid = int(r.headers["location"].rsplit("/", 1)[1])
        if sid not in self.session_ids:
            self.session_ids.append(sid)
            if len(self.session_ids) > 2:
                self.session_ids.pop(0)

    # Rule 1b: 開一場綁定場次

    @rule()
    def rule_open_bind_session(self):
        before_kind = self.live_kind()
        r = self.teacher.post(f"/t/course/{self.cid}/open", data={"kind": "bind"}, follow_redirects=False)
        assert r.status_code < 500
        if before_kind is not None and before_kind != "bind":
            # 已有一場未結案場次而種類不同，不沿用也不新建，回 409
            assert r.status_code == 409, f"種類不同時應 409（app.py do_open_session）: {r.status_code} {r.text}"
            return
        assert r.status_code == 303, r.text
        sid = int(r.headers["location"].rsplit("/", 1)[1])
        with self.app.db() as con:
            row = con.execute("SELECT kind FROM sessions WHERE id=?", (sid,)).fetchone()
        # 同課同時只會有一場 live：若目前已有一場 live（不論種類），這裡拿到的就是既有那場，
        # kind 不一定是 'bind'——只有真的拿到一場 kind='bind' 的場次才記進 session_ids／視為綁定場次驗證對象。
        if row["kind"] == "bind" and sid not in self.session_ids:
            self.session_ids.append(sid)
            if len(self.session_ids) > 2:
                self.session_ids.pop(0)

    # Rule 2: 時間前進

    @rule(secs=st.integers(min_value=1, max_value=400))
    def rule_advance_time(self, secs):
        self.t += secs
        self.traveler.shift(secs)

    # Rule 3: 首次簽到（自己手機）

    @rule(data=st.data())
    def rule_first_checkin(self, data):
        candidates = [s for s in STUDENTS if self.model_pin[s] is None]
        if not candidates:
            return
        stu = data.draw(st.sampled_from(candidates), label="fc_student")
        pin = data.draw(_digits(), label="fc_pin")
        phone_name = f"phone-{stu}"    # 這位學生從未設過 PIN，phone-{stu} 保證還沒被任何人綁過
        phone = self.dev(phone_name)
        sid = self.target_sid()

        if sid is not None and self.is_locked(sid, stu, phone_name):
            kind, r = self.attempt_checkin(phone, sid, stu, NAMES[stu], pin, pin)
            if kind == "checkin_resp":
                assert r.status_code == 423, f"I7 違反：已鎖定卻不是 423（do_checkin 的 PIN 鎖定檢查）: {r.status_code} {r.text}"
            return

        revoked = self.bind_revoked_here(sid, stu)
        kind, r = self.attempt_checkin(phone, sid, stu, NAMES[stu], pin, pin)
        if kind in ("no_session", "no_live", "rate_limited"):
            return
        assert kind == "checkin_resp"
        if not self.allow_bind and self.kind_of(sid) != "bind":  # 綁定場次不論 allow_bind 一律允許首次綁定
            assert r.status_code == 403, f"允許首次綁定關閉，預期 403（do_checkin 的 bind_closed 檢查）: {r.status_code} {r.text}"
            return
        if revoked:
            assert r.status_code == 403, f"本場抽點判不在後不能再綁，預期 403（bind_revoked）: {r.status_code} {r.text}"
            return
        assert r.status_code == 200, f"合法首次簽到卻失敗: {r.status_code} {r.text}"
        j = r.json()
        if self.student_has_pin(stu):
            # 這支手機本場若已被別的學號用過（same 衝突），do_checkin 不寫 PIN，該學號仍算「首次」；
            # 以資料庫為準決定要不要記進模型，否則之後 rule_wrong_pin 會預期 401 卻拿到 200
            self.model_pin[stu] = pin
        self.sync_phone_binding(stu, phone_name, phone)
        self.register_phone_use(sid, phone_name, stu, phone)
        if j.get("again"):
            # 本場已有一列（老師手動設定，或重設 PIN 前自己簽過），回的是那一列的狀態；
            # 這條路徑由檔尾的 test_borrowed_phone_keeps_manual_status_and_flags_conflict 鎖住，這裡不重複斷言。
            return
        phase = self.phase(sid)
        expect_status = {"open": "出席", "late": "遲到"}.get(phase)
        if self.others_used_phone(sid, phone_name, stu):
            expect_status = "待確認"  # 本場這支手機已簽過別的學號
        assert j["status"] == expect_status, (
            f"I3 違反：首次簽到、無既有列、phase={phase}，預期 {expect_status}，"
            f"實得 {j['status']}（do_checkin 決定 status 的那段）")

    # Rule 4: 已綁定回簽

    @rule(data=st.data())
    def rule_rebind_checkin(self, data):
        candidates = [s for s in STUDENTS if self.phone_of[s] is not None]
        if not candidates:
            return
        stu = data.draw(st.sampled_from(candidates), label="rb_student")
        phone_name = self.phone_of[stu]
        phone = self.dev(phone_name)
        sid = self.target_sid()

        if sid is not None and self.is_locked(sid, stu, phone_name):
            kind, r = self.attempt_checkin(phone, sid, stu, NAMES[stu], self.model_pin[stu], self.model_pin[stu])
            if kind == "checkin_resp":
                assert r.status_code == 423, f"I7 違反: {r.status_code} {r.text}"
            return

        kind, r = self.attempt_checkin(phone, sid, stu, NAMES[stu], self.model_pin[stu], self.model_pin[stu])
        if kind in ("no_session", "no_live", "rate_limited"):
            return
        assert kind == "checkin_resp"
        assert r.status_code == 200, f"已綁定同一手機用正確 PIN 回簽應成功: {r.status_code} {r.text}"
        self.clear_fail(sid, stu, phone_name)  # 已綁裝置不驗 PIN、也不受 PIN 鎖定影響（do_checkin 的 same_device_as_bound），模型一併歸零
        j = r.json()
        self.register_phone_use(sid, phone_name, stu, phone)
        if j.get("again"):
            return  # 已有一列（可能 manual／pending），依 I4 不應被此呼叫改動，另由 inv 檢查
        phase = self.phase(sid)
        expect_status = {"open": "出席", "late": "遲到"}.get(phase)
        if self.others_used_phone(sid, phone_name, stu):
            expect_status = "待確認"  # 本場這支手機已簽過別的學號
        assert j["status"] == expect_status, (
            f"I3 違反：已綁定、PIN 正確、phase={phase}，預期 {expect_status}，實得 {j['status']}（do_checkin 決定 status 的那段）")

    # Rule 5: 錯 PIN

    @rule(data=st.data())
    def rule_wrong_pin(self, data):
        candidates = [s for s in STUDENTS if self.model_pin[s] is not None]
        if not candidates:
            return
        stu = data.draw(st.sampled_from(candidates), label="wp_student")
        real = self.model_pin[stu]
        wrong = data.draw(_digits().filter(lambda p: p != real), label="wp_pin")
        # 已綁裝置（phone_of[stu]）回簽不驗 PIN，用它測不出 PIN 錯誤。這條規則要驗 PIN 驗證本身，
        # 所以固定用另一支探測手機；送出的 PIN 一定是錯的，do_checkin 在 PIN 比對失敗時就回 401，
        # 走不到綁裝置那一步，這支手機不會被綁走。
        phone_name = f"wrongpin-{stu}"
        phone = self.dev(phone_name)
        sid = self.target_sid()

        if sid is not None and self.is_locked(sid, stu, phone_name):
            kind, r = self.attempt_checkin(phone, sid, stu, NAMES[stu], wrong, wrong)
            if kind == "checkin_resp":
                assert r.status_code == 423, f"I7 違反: {r.status_code} {r.text}"
            return

        revoked = self.bind_revoked_here(sid, stu)
        kind, r = self.attempt_checkin(phone, sid, stu, NAMES[stu], wrong, wrong)
        if kind in ("no_session", "no_live", "rate_limited"):
            return
        assert kind == "checkin_resp"
        if self.phone_of[stu] is None and not self.allow_bind and self.kind_of(sid) != "bind":
            # 閘門（first_time or device_hash is None）在 PIN 驗證之前，未綁裝置時 allow_bind
            # 關閉會直接 403，不會走到 PIN 比對（do_checkin 的 bind_closed 檢查在前）。
            # 綁定場次不論 allow_bind 一律允許首次綁定，不受這個閘門擋
            assert r.status_code == 403, f"未綁裝置＋allow_bind 關閉應 403: {r.status_code} {r.text}"
            return
        if revoked:
            assert r.status_code == 403, f"本場抽點判不在後不能再綁，預期 403（bind_revoked）: {r.status_code} {r.text}"
            return
        assert r.status_code == 401, f"錯 PIN 應 401（do_checkin 的 PIN 比對）: {r.status_code} {r.text}"
        self.bump_fail(sid, stu, phone_name)

    # Rule 6: 代簽／借手機

    @rule(data=st.data())
    def rule_borrow_phone(self, data):
        owners = [s for s in STUDENTS if self.phone_of[s] is not None]
        if len(owners) < 1:
            return
        a = data.draw(st.sampled_from(owners), label="bp_owner")
        others = [s for s in STUDENTS if s != a]
        b = data.draw(st.sampled_from(others), label="bp_target")
        phone_name = self.phone_of[a]
        phone = self.dev(phone_name)
        sid = self.target_sid()

        b_registered = self.model_pin[b] is not None
        b_bound = self.phone_of[b] is not None
        pin = self.model_pin[b] if b_registered else data.draw(_digits(), label="bp_pin")

        if sid is not None and self.is_locked(sid, b, phone_name):
            kind, r = self.attempt_checkin(phone, sid, b, NAMES[b], pin, pin)
            if kind == "checkin_resp":
                assert r.status_code == 423, f"I7 違反: {r.status_code} {r.text}"
            return

        # 借用前 B 本場是否已有一列：有的話 do_checkin 走 already 早返、沿用那一列的狀態，
        # 回傳的狀態不是這次判出來的，這種情況不斷言 I5（由檔尾的獨立測試鎖住）。
        pre_existing = sid is not None and self.existing_checkin(sid, b) is not None
        revoked = self.bind_revoked_here(sid, b)

        kind, r = self.attempt_checkin(phone, sid, b, NAMES[b], pin, pin)
        if kind in ("no_session", "no_live", "rate_limited"):
            return
        assert kind == "checkin_resp"

        if b_bound and self.phone_of[b] != phone_name:
            # B 自己已綁其他手機，用 A 的手機送出＝換手機衝突（do_checkin 回 409），
            # 這個判定在 already 早返之前，不受 pre_existing 影響
            assert r.status_code == 409, f"I5/裝置衝突應 409: {r.status_code} {r.text}"
            return
        # 閘門（first_time or device_hash is None）在 PIN 驗證與 already 早返之前
        # （do_checkin 的 bind_closed 檢查），只要 B 還沒被自己的裝置綁定就適用，不分是否已註冊
        gate_active = (not b_registered) or (not b_bound)
        if gate_active and not self.allow_bind and self.kind_of(sid) != "bind":  # 綁定場次不受這個閘門擋
            assert r.status_code == 403, f"allow_bind 關閉時應 403: {r.status_code} {r.text}"
            return
        if revoked:
            assert r.status_code == 403, f"本場抽點判不在後不能再綁，預期 403（bind_revoked）: {r.status_code} {r.text}"
            return

        assert r.status_code == 200, f"預期借手機仍應收單: {r.status_code} {r.text}"
        if b_registered:
            self.clear_fail(sid, b, phone_name)  # 非首次分支 PIN 比對通過後會清掉這支裝置的 pin_fail
        j = r.json()
        if not b_registered and self.student_has_pin(b):
            # 只有 other/same 都不成立時 do_checkin 才會寫 PIN（`if not (other or same): ...
            # UPDATE students SET pin_hash=...`）；借手機時 phone_name 是 A 已綁定的手機，
            # 幾乎必然 other=True，PIN 不會寫入，該學號仍是「首次」，所以用 DB 回查決定要不要記進模型。
            self.model_pin[b] = pin
        self.register_phone_use(sid, phone_name, b, phone)  # DB 驗證，pre_existing 時本來就不會誤登記
        # A、B 這次都可能因 same_device 被降級（do_checkin 的 same 分支），I4 對他們暫時豁免
        self.manual_conflict_cleared.add((sid, a))
        self.manual_conflict_cleared.add((sid, b))
        if pre_existing:
            return  # already 早返沿用既有列的狀態，見檔尾的獨立測試
        assert j["status"] != "出席" and j["status"] != "遲到", (
            f"I5 違反：借用他人已綁定手機替 {b} 送出，卻直接拿到「{j['status']}」而非待確認/拒絕"
            f"（do_checkin 標了 device_bound_other/same_device 應強制 pending）")

    # Rule 7: 換手機

    @rule(data=st.data())
    def rule_switch_phone(self, data):
        candidates = [s for s in STUDENTS if self.model_pin[s] is not None]
        if not candidates:
            return
        stu = data.draw(st.sampled_from(candidates), label="sw_student")
        new_phone_name = self.spare_phone(stu)
        phone = self.dev(new_phone_name)
        sid = self.target_sid()
        pin = self.model_pin[stu]

        if sid is not None and self.is_locked(sid, stu, new_phone_name):
            kind, r = self.attempt_checkin(phone, sid, stu, NAMES[stu], pin, pin)
            if kind == "checkin_resp":
                assert r.status_code == 423, f"I7 違反: {r.status_code} {r.text}"
            return

        revoked = self.bind_revoked_here(sid, stu)
        kind, r = self.attempt_checkin(phone, sid, stu, NAMES[stu], pin, pin)
        if kind in ("no_session", "no_live", "rate_limited"):
            return
        assert kind == "checkin_resp"

        if self.phone_of[stu] is not None:
            assert r.status_code == 409, (
                f"換手機（未解除綁定）應 409（do_checkin 的 bound_other_device），實得 {r.status_code} {r.text}")
            return
        # 老師已解除綁定（或從未綁定過）：全新手機、無衝突，應成功並綁上新手機
        if not self.allow_bind and self.kind_of(sid) != "bind":  # 綁定場次不受這個閘門擋
            assert r.status_code == 403, f"allow_bind 關閉時應 403: {r.status_code} {r.text}"
            return
        if revoked:
            assert r.status_code == 403, f"本場抽點判不在後不能再綁，預期 403（bind_revoked）: {r.status_code} {r.text}"
            return
        assert r.status_code == 200, f"解除綁定後換新手機應成功: {r.status_code} {r.text}"
        self.clear_fail(sid, stu, new_phone_name)  # 非首次分支 PIN 比對通過後會清掉這支裝置的 pin_fail
        self.sync_phone_binding(stu, new_phone_name, phone)
        self.register_phone_use(sid, new_phone_name, stu, phone)

    # Rule 8: 老師手動改狀態

    @rule(data=st.data())
    def rule_manual_status(self, data):
        if not self.session_ids:
            return
        sid = data.draw(st.sampled_from(self.session_ids), label="ms_sid")
        stu = data.draw(st.sampled_from(STUDENTS), label="ms_student")
        status = data.draw(st.sampled_from(list(self.app.STATUS_LABEL)), label="ms_status")
        pk = self.pk_of[stu]
        r = self.teacher.post(f"/api/t/session/{sid}/status", data={"pk": pk, "status": status})
        assert r.status_code < 500
        if self.kind_of(sid) == "bind":
            # 綁定場次不接受手動改狀態（含手動簽到走的同一支端點）
            assert r.status_code == 409, f"綁定場次手動改狀態應 409: {r.status_code} {r.text}"
            return
        assert r.status_code == 200, f"手動設狀態任何時候都應合法（api_set_status）: {r.status_code} {r.text}"
        self.manual_status[(sid, stu)] = status
        self.manual_conflict_cleared.discard((sid, stu))

    # Rule 9: 延長／結束

    @rule(data=st.data())
    def rule_extend_or_close(self, data):
        if not self.session_ids:
            return
        sid = data.draw(st.sampled_from(self.session_ids), label="ec_sid")
        action = data.draw(st.sampled_from(["extend", "close"]), label="ec_action")
        phase_before = self.phase(sid)
        if action == "extend":
            minutes = data.draw(st.integers(min_value=1, max_value=60), label="ec_minutes")
            r = self.teacher.post(f"/api/t/session/{sid}/extend", data={"minutes": minutes})
            assert r.status_code < 500
            if phase_before == "closed":
                assert r.status_code == 409, f"已結束不能延長（do_extend）: {r.status_code} {r.text}"
            else:
                assert r.status_code == 200, f"延長應成功: {r.status_code} {r.text}"
        else:
            r = self.teacher.post(f"/api/t/session/{sid}/close")
            assert r.status_code < 500
            assert r.status_code == 200, f"結束點名永遠合法（含重複結束，api_close）: {r.status_code} {r.text}"

    # Rule 10: 解除綁定

    @rule(data=st.data())
    def rule_unbind(self, data):
        stu = data.draw(st.sampled_from(STUDENTS), label="ub_student")
        reset_pin = data.draw(st.booleans(), label="ub_reset_pin")
        pk = self.pk_of[stu]
        r = self.teacher.post(f"/api/t/student/{pk}/unbind", data={"reset_pin": "1" if reset_pin else ""})
        assert r.status_code < 500
        assert r.status_code == 200, f"解除綁定任何時候都應合法（api_unbind）: {r.status_code} {r.text}"
        old_phone = self.phone_of[stu]
        if old_phone is not None:
            self.owner_of_phone.pop(old_phone, None)
        self.phone_of[stu] = None
        if reset_pin:
            self.model_pin[stu] = None

    # Rule 11: 切換 allow_bind

    @rule(data=st.data())
    def rule_toggle_bind(self, data):
        allow = data.draw(st.booleans(), label="tb_allow")
        r = self.teacher.post(f"/api/t/course/{self.cid}/bind", data={"allow": "1" if allow else "0"})
        assert r.status_code < 500
        assert r.status_code == 200, f"切換 allow_bind 應合法（api_toggle_bind）: {r.status_code} {r.text}"
        self.allow_bind = allow

    # Rule 12a: 抽點

    @rule(data=st.data())
    def rule_spotcheck_pick(self, data):
        if not self.session_ids:
            return
        sid = data.draw(st.sampled_from(self.session_ids), label="sp_sid")
        phase_before = self.phase(sid)
        r = self.teacher.post(f"/api/t/session/{sid}/spotcheck")
        assert r.status_code < 500
        # 綁定場次也開放抽點，只有已結束的場次擋抽點
        if phase_before == "closed":
            assert r.status_code == 409, f"已結束不能抽點（api_spotcheck）: {r.status_code} {r.text}"
            return
        assert r.status_code == 200, f"抽點應成功（無可抽時也回 200 帶 note）: {r.status_code} {r.text}"
        for p in r.json()["picks"]:
            self.pending_spotchecks.append({"scid": p["id"], "sid": sid, "stu": p["student_id"]})

    # Rule 12b: 裁定抽點結果

    @rule(data=st.data())
    def rule_spotcheck_decide(self, data):
        if not self.pending_spotchecks:
            return
        idx = data.draw(st.integers(min_value=0, max_value=len(self.pending_spotchecks) - 1), label="sd_idx")
        item = self.pending_spotchecks.pop(idx)
        present = data.draw(st.booleans(), label="sd_present")
        phase_before = self.phase(item["sid"])
        r = self.teacher.post(f"/api/t/spotcheck/{item['scid']}", data={"present": "1" if present else "0"})
        assert r.status_code < 500
        if phase_before == "closed":
            assert r.status_code == 409, f"已結束不能裁定抽點（api_spot_result）: {r.status_code} {r.text}"
            return
        assert r.status_code == 200, f"裁定抽點應成功: {r.status_code} {r.text}"
        # 抽點裁定是老師自己的動作，可以合法覆寫老師先前設定的狀態（不算被「學生端」動作改掉）；
        # present=False 還會連坐同裝置其他人（api_spot_result 的 proxy_for），一併豁免該場所有人保守處理
        self.manual_conflict_cleared.add((item["sid"], item["stu"]))
        if not present:
            for s in STUDENTS:
                self.manual_conflict_cleared.add((item["sid"], s))
            # 綁定場次抽點不在場會撤回綁定（device_hash/pin_hash 都清空），跟老師手動解除
            # 綁定＋reset_pin=True 是同一件事，model 要照 rule_unbind 那套同步，否則之後的規則
            # 會以為這支手機還是這個學生的。
            if self.kind_of(item["sid"]) == "bind":
                assert r.json().get("unbound") is True, f"綁定場次抽點不在場應回 unbound: {r.text}"
                self.bind_revoked.add((item["sid"], item["stu"]))
                old_phone = self.phone_of[item["stu"]]
                if old_phone is not None:
                    self.owner_of_phone.pop(old_phone, None)
                self.phone_of[item["stu"]] = None
                self.model_pin[item["stu"]] = None
            else:
                assert "unbound" not in r.json(), f"一般場次抽點不應回 unbound: {r.text}"

    # Invariants

    @invariant()
    def inv_state_db_csv_me(self):
        for sid in self.session_ids:
            data_state = self.teacher.get(f"/api/t/session/{sid}/state")
            assert data_state.status_code < 500
            assert data_state.status_code == 200
            js = data_state.json()
            rows = {r["student_id"]: r for r in js["rows"]}

            # I1：一場一生最多一列
            with self.app.db() as con:
                dup = con.execute(
                    "SELECT st.student_id, COUNT(*) c FROM checkins k JOIN students st ON st.id=k.student_pk "
                    "WHERE k.session_id=? GROUP BY st.student_id HAVING COUNT(*)>1", (sid,)).fetchall()
            assert not dup, f"I1 違反（schema UNIQUE(session_id,student_pk) 竟被繞過）: sid={sid} {[dict(d) for d in dup]}"

            # I2a：state 的彙總計數 = rows 逐列統計
            recompute = {k: 0 for k in self.app.STATUS_LABEL}
            recompute["none"] = 0
            for row in js["rows"]:
                recompute[row["status"] or "none"] += 1
            assert recompute == js["counts"], f"I2 違反：state 彙總計數與逐列統計不符 sid={sid}: {recompute} vs {js['counts']}"

            # I2b：state 與 DB 原始 status 一致
            with self.app.db() as con:
                db_rows = {r["student_id"]: r["status"] for r in con.execute(
                    "SELECT st.student_id, k.status FROM checkins k JOIN students st ON st.id=k.student_pk WHERE k.session_id=?",
                    (sid,))}
            for stu, dbstatus in db_rows.items():
                assert rows[stu]["status"] == dbstatus, f"I2 違反：state 與 DB 不一致 sid={sid} stu={stu}: {rows[stu]['status']} vs {dbstatus}"

            # I2c：對已結束場次，state 與 api_me／CSV 一致（跨代表一致性）；
            # 綁定場次與已作廢場次不計入出缺勤，反過來要驗它們完全不出現在 /api/me 與 CSV
            # （counts_toward_attendance 就是 app.py 共用定義本身，兩邊用同一個判準）。
            with self.app.db() as con:
                srow = con.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
            if srow["closed_at"] is not None:
                counted = self.app.counts_toward_attendance(srow)
                for stu in STUDENTS:
                    label = self.app.STATUS_LABEL.get(rows[stu]["status"], "") if rows[stu]["status"] else "缺席"
                    if self.phone_of[stu] is not None:
                        phone_client = self.dev(self.phone_of[stu])
                        self.clear_me_throttle(phone_client)
                        r_me = phone_client.post("/api/me")
                        assert r_me.status_code < 500 and r_me.status_code == 200
                        me = r_me.json()
                        for course in me["courses"]:
                            if course["student_id"] != stu:
                                continue
                            with self.app.db() as con:
                                ordered = [r["id"] for r in con.execute(
                                    f"SELECT id FROM sessions WHERE course_id=? AND (closed_at IS NOT NULL OR late_until<?) "
                                    f"AND {self.app.COUNTED_SESSION_SQL} ORDER BY opened_at",
                                    (self.cid, self.t))]
                            if not counted:
                                assert sid not in ordered, (
                                    f"I2 違反：不計入出缺勤的場次 sid={sid} kind={srow['kind']} "
                                    f"voided_at={srow['voided_at']} 卻出現在 /api/me 可列出的場次清單裡")
                                continue
                            if sid in ordered:
                                idx = ordered.index(sid)
                                if idx < len(course["items"]):
                                    assert course["items"][idx]["label"] == label, (
                                        f"I2 違反：/api/me 與 state 不一致 sid={sid} stu={stu}: "
                                        f"{course['items'][idx]['label']} vs {label}（api_me vs api_state）")

                with self.app.db() as con:
                    closed_ids_ordered = [r["id"] for r in con.execute(
                        f"SELECT id FROM sessions WHERE course_id=? AND closed_at IS NOT NULL AND {self.app.COUNTED_SESSION_SQL} "
                        "ORDER BY opened_at", (self.cid,))]
                if not counted:
                    assert sid not in closed_ids_ordered, (
                        f"I2 違反：不計入出缺勤的場次 sid={sid} kind={srow['kind']} "
                        f"voided_at={srow['voided_at']} 卻出現在報表／CSV 可列出的場次清單裡")
                elif sid in closed_ids_ordered:
                    col = 3 + closed_ids_ordered.index(sid)
                    csv_resp = self.teacher.get(f"/t/course/{self.cid}/export/csv")
                    assert csv_resp.status_code < 500 and csv_resp.status_code == 200
                    text = csv_resp.text.lstrip("﻿")
                    csv_rows = list(csv.reader(io.StringIO(text)))
                    for row_vals in csv_rows[1:]:
                        stu = row_vals[0].lstrip("'")
                        if stu not in STUDENTS:
                            continue
                        expect_label = self.app.STATUS_LABEL.get(rows[stu]["status"], "") if rows[stu]["status"] else "缺席"
                        assert row_vals[col] == expect_label, (
                            f"I2 違反：CSV 與 state 不一致 sid={sid} stu={stu}: {row_vals[col]} vs {expect_label}"
                            f"（build_report vs api_state）")

    @invariant()
    def inv_manual_status_preserved(self):
        for (sid, stu), status in list(self.manual_status.items()):
            if (sid, stu) in self.manual_conflict_cleared:
                continue
            with self.app.db() as con:
                row = con.execute(
                    "SELECT k.status FROM checkins k WHERE k.session_id=? AND k.student_pk=?",
                    (sid, self.pk_of[stu])).fetchone()
            assert row is not None and row["status"] == status, (
                f"I4 違反：老師手動設定 sid={sid} stu={stu} 為「{status}」，之後被學生端動作改成 "
                f"「{row['status'] if row else None}」（api_set_status 設的狀態，do_checkin 的 already 早返應保持不變）")

    @invariant()
    def inv_lock_reflected(self):
        tracked_by_sid: dict[int, set] = {}
        for (sid, stu, _phone), n in self.fail_by_phone.items():
            if n >= self.app.DEFAULT_SETTINGS["pin_max_fail"]:
                tracked_by_sid.setdefault(sid, set()).add(stu)
        for sid, stus in tracked_by_sid.items():
            # close_session_rows 結案時會清掉本場 pin_fail（rule_extend_or_close 手動結案或
            # finalize_expired 自動結案都會觸發）；結案後任何簽到都先被 phase=="closed" 擋下，
            # 走不到 PIN 鎖定檢查，鎖定狀態不再可觀測也不影響任何行為，跳過不追。
            if self.phase(sid) == "closed":
                continue
            with self.app.db() as con:
                locked_pks = {r["student_pk"] for r in con.execute(
                    "SELECT student_pk FROM pin_fail WHERE session_id=? AND device_hash!='' AND n>=?",
                    (sid, self.app.DEFAULT_SETTINGS["pin_max_fail"]))}
            for stu in stus:
                assert self.pk_of[stu] in locked_pks, (
                    f"I7 違反：模型判定 sid={sid} stu={stu} 已連錯達上限，但 pin_fail 表未反映"
                    f"（do_checkin 寫 pin_fail 的那段）")


TestAttendStateMachine = AttendStateMachine.TestCase


# 借別人的手機替已有手動狀態的學生做首次綁定

def test_borrowed_phone_keeps_manual_status_and_flags_conflict(h):
    """老師手動設的狀態不能因為別人拿別支手機來簽而被改掉（I4）；這次也不寫 PIN、不綁裝置，
    並在那一列標裝置衝突旗標，讓老師看得到。"""
    sid = h.open("CD")
    # A 用自己的手機先綁定
    h.checkin(sid, "devA", "9994A001", name="學生1", pin="1111")
    # 老師手動把 B（9994A002）記成出席——B 本人根本還沒簽到過
    b_pk = h.pk("CD", "9994A002")
    r = h.T.post(f"/api/t/session/{sid}/status", data={"pk": b_pk, "status": "present"})
    assert r.status_code == 200

    # 有人拿 A 的手機、以 B 的學號＋自訂一組新 PIN 來做「首次綁定」
    resp = h.checkin(sid, "devA", "9994A002", name="學生2", pin="9999")
    assert resp["ok"] is True
    # I4：老師手動設的狀態不得被學生端動作改掉，狀態維持「出席」
    assert resp.get("again") is True and resp["status"] == "出席"

    with h.A.db() as con:
        b_row = con.execute("SELECT pin_hash, device_hash FROM students WHERE id=?", (b_pk,)).fetchone()
    assert b_row["pin_hash"] is None, "借用他人已綁定手機替本來就有紀錄的學生做首次綁定，不該寫入 PIN"
    assert b_row["device_hash"] is None, "同理，不該綁裝置"

    row = h.row(sid, "9994A002")
    assert row["status"] == "present", "I4：老師手動設的狀態不得被學生端動作改掉"
    assert "device_bound_other" in row["flags"] or "same_device" in row["flags"], (
        f"應標旗給老師看到裝置衝突，即使狀態維持不變（此處 flags={row['flags']!r}）")
