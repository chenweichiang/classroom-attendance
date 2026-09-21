"""整體情境回歸測試：三門課交錯、跨課同學生同手機、多週、延長、自動結案、解綁重綁、併發、四方對帳。
跑法（在 services/attend）：.venv/bin/python tests/scenario.py   （需要 httpx：uv pip install -p .venv/bin/python httpx）
用獨立的 /tmp 資料庫，不碰正式資料。全部通過會印「全部通過」，否則列出失敗項並以非零離開。"""
import os, sys, threading, csv, io
os.environ.update(ATTEND_SECRET="scen", ATTEND_TEACHER_PASSWORD="pw", ATTEND_DB="/tmp/attend-scen.db", ATTEND_BASE_URL="http://test")
for f in ("/tmp/attend-scen.db", "/tmp/attend-scen.db-wal", "/tmp/attend-scen.db-shm"):
    try: os.remove(f)
    except FileNotFoundError: pass
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app as A
from fastapi.testclient import TestClient

FAIL = []
def check(cond, msg):
    print(("ok   " if cond else "FAIL ") + msg)
    if not cond: FAIL.append(msg)

# 名冊：CD 與 DT 共用 9994A001（模擬同一學生修兩門課）
with A.db() as con:
    for code, name in (("CD", "脈絡設計"), ("DT", "設計思考"), ("OOP", "物件導向")):
        con.execute("INSERT INTO courses(code,name) VALUES(?,?)", (code, name))
    cid = {r["code"]: r["id"] for r in con.execute("SELECT id, code FROM courses")}
    for sid in ("9994A001", "9994A002", "9994A003", "9994A004"):
        con.execute("INSERT INTO students(course_id,student_id,name,cls) VALUES(?,?,?,?)", (cid["CD"], sid, "學生" + sid[-1], ""))
    for sid in ("9994A001", "1153A009"):
        con.execute("INSERT INTO students(course_id,student_id,name,cls) VALUES(?,?,?,?)", (cid["DT"], sid, "學生" + sid[-1], ""))
    for sid in ("999000025", "999000026"):
        con.execute("INSERT INTO students(course_id,student_id,name,cls) VALUES(?,?,?,?)", (cid["OOP"], sid, "清大" + sid[-1], ""))

T = TestClient(A.app, base_url="http://test")
S = lambda: TestClient(A.app, base_url="http://test")  # 學生各自的 client（不帶老師 cookie）
_devs = {}
def D(name):  # 一支手機＝一個保留 cookie 的 client
    if name not in _devs:
        _devs[name] = S(); _devs[name].get("/c")
    return _devs[name]
r = T.post("/t/login", data={"password": "pw", "next": "/t"}, follow_redirects=False); check(r.status_code == 303, "老師登入")

def open_session(course):
    r = T.post(f"/t/course/{cid[course]}/open", follow_redirects=False)
    return int(r.headers["location"].rsplit("/", 1)[1])

def grant(sid, client=None):
    code = T.get(f"/api/t/session/{sid}/qr").json()["code"]
    html = (client or S()).post("/c", data={"code": code}).text
    import re
    return re.search(r'GRANT="([^"]+)"', html).group(1)

def checkin(sid, dev, stu, name="", pin="1234", pin2=None, ua="iPhone", ip="140.131.1.1", hints="h"):
    c = D(dev)
    return c.post("/api/checkin", data={"grant": grant(sid, c), "student_id": stu, "name": name, "pin": pin, "pin2": pin2 if pin2 is not None else pin, "hints": hints},
                  headers={"user-agent": ua, "cf-connecting-ip": ip}).json()

def state(sid): return T.get(f"/api/t/session/{sid}/state").json()
def row(sid, stu): return next(x for x in state(sid)["rows"] if x["student_id"] == stu)
def db_rows(sid):
    with A.db() as con:
        return {r["student_id"]: dict(r) for r in con.execute("SELECT st.student_id, k.* FROM checkins k JOIN students st ON st.id=k.student_pk WHERE k.session_id=?", (sid,))}

# ── 週 1：CD 與 DT 同時開，同一學生 A001 用同一支手機在兩門課首簽 ──
cd1 = open_session("CD"); dt1 = open_session("DT")
check(open_session("CD") == cd1, "CD 重按開始點名回同一場（不重複開）")
j = checkin(cd1, "phoneA-0000000000000000", "9994A001", "學生1"); check(j["ok"] and j["status"] == "出席" and j["first_time"], "A001 CD 首簽")
j = checkin(dt1, "phoneA-0000000000000000", "9994A001", "學生1", pin="5678"); check(j["ok"] and j["first_time"], "A001 DT 用同手機首簽（各課獨立 PIN）")
check(not row(dt1, "9994A001")["flags"] or "same_device" not in row(dt1, "9994A001")["flags"], "跨課同手機不算同裝置多人")
j = checkin(cd1, "phoneA-0000000000000000", "9994A001"); check(j["again"], "A001 CD 再簽回原收據")
# A2（SPEC 2026-09-21）：CD 這支手機已綁定，回簽免 PIN——即使帶的是 DT 的 PIN（甚至亂打）也成功，
# 「各課獨立 PIN」改由首綁那一刻（上面 dt1 那行）示範，回簽階段 PIN 已經不是判準
j = checkin(cd1, "phoneA-0000000000000000", "9994A001", pin="5678"); check(j["ok"] and j["again"], "A2：已綁裝置回簽免 PIN，帶錯 PIN 也成功")
# A002 用 A001 手機代簽
j = checkin(cd1, "phoneA-0000000000000000", "9994A002", "學生2"); check(j["ok"] and j["status"] == "待確認", "A002 用 A001 手機 → 待確認")
check(row(cd1, "9994A001")["status"] == "pending", "A001 也轉待確認（兩人都要看）")
check("was_late" not in row(cd1, "9994A002")["flags"], "出席窗內的待確認不帶 was_late")
f = row(cd1, "9994A002")["flags"]; check("same_device" in f and "device_bound_other" in f, "A002 flags 同裝置＋已綁他人")
# A003 正常，A004 沒簽；DT 的 A009 用另一手機
j = checkin(cd1, "phoneC-0000000000000000", "9994A003", "學生3"); check(j["ok"] and j["status"] == "出席", "A003 出席")
j = checkin(dt1, "phoneD-0000000000000000", "1153A009", "學生9"); check(j["ok"], "DT A009 出席")
# 抽點：A002 不在 → A002 缺席、A001 連坐
with A.db() as con:
    pk2 = con.execute("SELECT id FROM students WHERE course_id=? AND student_id='9994A002'", (cid["CD"],)).fetchone()["id"]
    scid = con.execute("INSERT INTO spotchecks(session_id,student_pk,ts) VALUES(?,?,?)", (cd1, pk2, A.now())).lastrowid
T.post(f"/api/t/spotcheck/{scid}", data={"present": "0"})
d = db_rows(cd1); check(d["9994A002"]["status"] == "absent" and d["9994A001"]["status"] == "absent" and "proxy_for" in d["9994A001"]["flags"], "抽點不在：A002 缺席、A001 連坐")
check(db_rows(dt1)["9994A001"]["status"] == "present", "連坐不跨課：DT 的 A001 仍出席")
with A.db() as con:
    dh2 = con.execute("SELECT device_hash FROM students WHERE course_id=? AND student_id='9994A002'", (cid["CD"],)).fetchone()["device_hash"]
check(dh2 is None, "代簽者的手機不會被綁到被代簽者的學號")
check("same_device" in db_rows(cd1)["9994A001"]["flags"], "同裝置標記雙向：先簽的 A001 也有")
w = D("phoneA-0000000000000000").post("/api/whoami", data={"course_id": cid["CD"]}).json(); check(w["known"] and w["student_id"] == "9994A001", "whoami 只認 A001")
# 老師把 A001 改回出席（連坐誤判）→ manual
with A.db() as con: pk1 = con.execute("SELECT id FROM students WHERE course_id=? AND student_id='9994A001'", (cid["CD"],)).fetchone()["id"]
T.post(f"/api/t/session/{cd1}/status", data={"pk": pk1, "status": "present"})
check(db_rows(cd1)["9994A001"]["status"] == "present" and db_rows(cd1)["9994A001"]["source"] == "manual", "老師改回出席 → manual")
# 關 CD：A004 自動缺席；DT 不受影響
T.post(f"/api/t/session/{cd1}/close")
d = db_rows(cd1); check(d["9994A004"]["status"] == "absent" and d["9994A004"]["source"] == "auto", "A004 自動缺席")
check(state(dt1)["phase"] == "open" and "1153A009" in db_rows(dt1), "關 CD 不影響 DT")
check(T.post(f"/api/t/session/{cd1}/close").json().get("note"), "重複 close 有 note")
check(not T.post(f"/api/t/session/{cd1}/extend", data={"minutes": 2}).json()["ok"], "已關不能延長")
# DT：延長後 late_until 照設定
with A.db() as con: s = dict(con.execute("SELECT * FROM sessions WHERE id=?", (dt1,)).fetchone())
T.post(f"/api/t/session/{dt1}/extend", data={"minutes": 2})
with A.db() as con: s2 = dict(con.execute("SELECT * FROM sessions WHERE id=?", (dt1,)).fetchone())
check(s2["open_until"] > s["open_until"] and s2["late_until"] >= s2["open_until"] + 12 * 60, "延長：出席窗＋2 分、遲到區照設定 12 分")
# DT 忘了關：模擬逾時 → 自動結案
with A.db() as con: con.execute("UPDATE sessions SET late_until=? WHERE id=?", (A.now() - 1, dt1))
T.get("/t")
with A.db() as con: s3 = dict(con.execute("SELECT * FROM sessions WHERE id=?", (dt1,)).fetchone())
check(s3["closed_at"] is not None and abs(s3["closed_at"] - s3["late_until"]) < 1, "逾時場次自動結案，closed_at＝late_until")
# 自動結案後再開 DT 是新場
dt2 = open_session("DT"); check(dt2 != dt1, "自動結案後可開新場")

# ── 週 2：CD 第二次；A003 換手機；A002 被老師 reset；allow_bind 關閉 ──
T.post(f"/api/t/course/{cid['CD']}/bind", data={"allow": "0"})
# 名冊頁「儲存設定」不會動到首次綁定開關，也不會抹掉表單沒有的設定
T.post(f"/t/course/{cid['CD']}/settings", data={"open_minutes": 3, "late_minutes": 15, "qr_interval": 10, "qr_grace": 5, "spot_n": 3, "ip_check": "1", "discord_channel": ""})
with A.db() as con: st_ = A.course_settings(A.get_course(con, cid["CD"]))
check(st_["allow_bind"] is False and st_["grant_seconds"] == 180, "儲存設定不覆寫 allow_bind 與其他設定")
cd2 = open_session("CD")
j = checkin(cd2, "phoneC2-000000000000000", "9994A003"); check(not j["ok"] and "另一支手機" in j["error"], "A003 換手機未解綁被擋")
with A.db() as con: pk3 = con.execute("SELECT id FROM students WHERE course_id=? AND student_id='9994A003'", (cid["CD"],)).fetchone()["id"]
T.post(f"/api/t/student/{pk3}/unbind", data={"reset_pin": ""})
j = checkin(cd2, "phoneC2-000000000000000", "9994A003"); check(not j["ok"] and "不開放首次綁定" in j["error"], "解綁後但 allow_bind 關 → 擋")
T.post(f"/api/t/course/{cid['CD']}/bind", data={"allow": "1"})
j = checkin(cd2, "phoneC2-000000000000000", "9994A003"); check(j["ok"] and not j.get("first_time"), f"開放後 A003 用舊 PIN 綁新手機 {j}")
check(checkin(cd2, "phoneC2-000000000000000", "9994A003")["again"], "新手機再簽回收據")
T.post(f"/api/t/student/{pk2}/unbind", data={"reset_pin": "1"})
j = checkin(cd2, "phoneB-0000000000000000", "9994A002", "學生2", pin="9999"); check(j["ok"] and j["first_time"], "A002 重設後當首次，新 PIN")
# 併發：A004 同一手機連送 5 次
res = []
E = D("phoneE"); g4 = grant(cd2, E)
def go(): res.append(E.post("/api/checkin", data={"grant": g4, "student_id": "9994A004", "name": "學生4", "pin": "1234", "pin2": "1234"}).json())
ths = [threading.Thread(target=go) for _ in range(5)]; [t.start() for t in ths]; [t.join() for t in ths]
check(all(x["ok"] for x in res) and sum(1 for x in res if not x["again"]) == 1, f"併發 5 送：1 筆新、其餘回收據（{[x.get('again') for x in res]}）")
with A.db() as con: check(con.execute("SELECT COUNT(*) FROM checkins WHERE session_id=? AND student_pk=(SELECT id FROM students WHERE course_id=? AND student_id='9994A004')", (cd2, cid["CD"])).fetchone()[0] == 1, "併發只留一筆")
# 老師先請假 A001，A001 之後掃碼 → 仍請假
T.post(f"/api/t/session/{cd2}/status", data={"pk": pk1, "status": "excused"})
j = checkin(cd2, "phoneA-0000000000000000", "9994A001"); check(j["again"] and j["status"] == "請假", "老師先記請假，學生再掃仍請假")
T.post(f"/api/t/session/{cd2}/close")

# ── 對帳：report / CSV / me / summary / state 四方一致 ──
with A.db() as con:
    sessions, rows = A.build_report(con, cid["CD"])
check(len(sessions) == 2, f"CD 報表 2 場（{len(sessions)}）")
rep = {r["stu"]["student_id"]: r for r in rows}
exp = {"9994A001": ["present", "excused"], "9994A002": ["absent", "present"], "9994A003": ["present", "present"], "9994A004": ["absent", "present"]}
for k, v in exp.items(): check(rep[k]["cells"] == v, f"報表 {k} {rep[k]['cells']}")
check(rep["9994A001"]["rate"] == 1.0 and rep["9994A002"]["rate"] == 0.5, "出席率：A001 請假不算分母＝100%，A002 50%")
csv_text = T.get(f"/t/course/{cid['CD']}/export/csv").text.lstrip("﻿"); rdr = list(csv.reader(io.StringIO(csv_text)))
check(rdr[1][0] == "9994A001" and rdr[1][3:5] == ["出席", "請假"] and rdr[1][-1] == "100%", "CSV 與報表一致")
me = D("phoneA-0000000000000000").post("/api/me").json()["courses"]
check({c["course"] for c in me} == {"脈絡設計", "設計思考"}, "同手機自查看到兩門課")
mecd = next(c for c in me if c["course"] == "脈絡設計")
check([i["status"] for i in mecd["items"]] == ["present", "excused"] and mecd["rate"] == 1.0, "自查與報表一致")
with A.db() as con: ch, txt = A.session_summary(con, cd2)
check("缺席 0" in txt and "請假 1" in txt and "出席 3" in txt, f"Discord 摘要一致：{txt.splitlines()[0]}")
st = state(cd2)["counts"]; check(st["present"] == 3 and st["excused"] == 1 and st["absent"] == 0 and st["none"] == 0, f"state counts {st}")

# ── 刪除場次無殘留 ──
oop1 = open_session("OOP"); checkin(oop1, "phoneO-0000000000000000", "999000025", "清大5"); T.post(f"/api/t/session/{oop1}/spotcheck")
T.post(f"/api/t/session/{oop1}/delete")
with A.db() as con:
    left = [con.execute(f"SELECT COUNT(*) FROM {t} WHERE session_id=?", (oop1,)).fetchone()[0] for t in ("checkins", "spotchecks", "pin_fail")] + [con.execute("SELECT COUNT(*) FROM sessions WHERE id=?", (oop1,)).fetchone()[0]]
check(left == [0, 0, 0, 0], f"刪除後無殘留 {left}")
check(D("phoneO-0000000000000000").post("/api/me").json()["courses"][0]["items"] == [], "刪除後自查無該場")
# 點名中途改設定：qr_interval 改 20 → 當下 QR 仍可用？
oop2 = open_session("OOP"); code_before = T.get(f"/api/t/session/{oop2}/qr").json()["code"]
T.post(f"/t/course/{cid['OOP']}/settings", data={"open_minutes": 3, "late_minutes": 15, "qr_interval": 20, "qr_grace": 5, "spot_n": 2, "ip_check": "1", "allow_bind": "1", "discord_channel": ""})
r = S().post("/c", data={"code": code_before}); check(r.status_code == 400, f"改 QR 秒數後舊碼失效（{r.status_code}）")
check(T.get(f"/api/t/session/{oop2}/qr").json()["interval"] == 20, "新設定即時生效")
T.post(f"/api/t/session/{oop2}/delete")

print("\n==>", "全部通過" if not FAIL else f"{len(FAIL)} 項失敗：{FAIL}")
sys.exit(1 if FAIL else 0)
