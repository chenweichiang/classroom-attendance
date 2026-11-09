"""tools/session_report.py：唯讀場次診斷報告的回歸（出錯的地方都要能從 log 看到）。
不影響 app 的覆蓋率量測；工具本身只開唯讀連線、輸出匿名化。"""
import os
import subprocess
import sys
from pathlib import Path

TOOL = Path(__file__).resolve().parent.parent / "tools" / "session_report.py"


def run_tool(db_path, *args):
    env = {**os.environ, "ATTEND_DB": str(db_path)}
    r = subprocess.run([sys.executable, str(TOOL), *args], capture_output=True, text=True, env=env, timeout=30)
    assert r.returncode == 0, r.stderr
    return r.stdout


def _blocked_then_absent(h):
    """9994A001 在第一場綁定；第二場在別的瀏覽器只打錯 PIN、沒成功，結案記自動缺席。"""
    s1 = h.open("CD")
    assert h.checkin(s1, "sr-A", "9994A001", name="學生1", pin="1234")["ok"]
    h.T.post(f"/api/t/session/{s1}/close")
    s2 = h.open("CD")
    j1 = h.checkin(s2, "sr-B", "9994A001", name="學生1", pin="5678")          # 首綁表單打新 PIN → pin_exists
    j2 = h.checkin(s2, "sr-B", "9994A001", pin="9999", pin2="")                # 只輸 PIN、打錯 → pin_wrong
    assert not j1["ok"] and not j2["ok"], (j1, j2)
    h.dev("sr-C").post("/c", data={"code": "000000"})                          # 代碼錯 → code_bad（沒有場次號）
    h.T.post(f"/api/t/session/{s2}/close")
    return s1, s2


def test_lists_blocked_student_who_ended_auto_absent(h):
    _, s2 = _blocked_then_absent(h)
    out = run_tool(h.A.DB_PATH, str(s2))
    assert f"=== 場次 {s2}" in out, out
    assert "被拒原因：" in out and "pin_exists" in out and "pin_wrong" in out, out
    assert "被擋過但最後是自動缺席：1 人 S01" in out, out
    assert "code_bad" in out, out  # 沒對到場次、但在開場前後 10 分鐘內的事件也要列出來


def test_unbind_outside_live_session_is_listed_as_teacher_action(h):
    """沒有進行中場次時的解除綁定（事件沒有場次號）也要列在當天那一場的老師操作裡；別門課的不列。"""
    s1, s2 = _blocked_then_absent(h)
    with h.A.db() as con:
        pk = con.execute("SELECT id, course_id FROM students WHERE student_id='9994A001' ORDER BY id LIMIT 1").fetchone()
        other = con.execute("SELECT id FROM students WHERE course_id!=? ORDER BY id LIMIT 1", (pk["course_id"],)).fetchone()
    assert h.T.post(f"/api/t/student/{pk['id']}/unbind", data={"reset_pin": "1"}).status_code == 200
    if other is not None:
        assert h.T.post(f"/api/t/student/{other['id']}/unbind", data={"reset_pin": ""}).status_code == 200
    with h.A.db() as con:
        ev = con.execute("SELECT session_id, reason FROM events WHERE kind='unbind' ORDER BY id").fetchall()
    assert ev and all(e["session_id"] is None for e in ev), [dict(e) for e in ev]  # 前提：結案後按的，沒有場次號
    out = run_tool(h.A.DB_PATH, str(s2))
    teacher_part = out.split("-- 老師操作", 1)[1].split("-- 裝置層", 1)[0]
    assert "unbind" in teacher_part and "reset" in teacher_part and "（場次外）" in teacher_part, out
    assert teacher_part.count("（場次外）") == 1, out  # 別門課那筆不列


def test_output_is_anonymous(h):
    _, s2 = _blocked_then_absent(h)
    out = run_tool(h.A.DB_PATH, "--last", "2")
    for secret in ("9994A001", "學生1", "140.131.1.1"):
        assert secret not in out, (secret, out)


def test_last_n_and_default_pick_latest_sessions(h):
    s1, s2 = _blocked_then_absent(h)
    out = run_tool(h.A.DB_PATH, "--last", "1")
    assert f"=== 場次 {s2}" in out and f"=== 場次 {s1}｜" not in out, out
    out_default = run_tool(h.A.DB_PATH)
    assert f"=== 場次 {s1}" in out_default and f"=== 場次 {s2}" in out_default, out_default


def test_read_only_does_not_touch_database(h):
    """主資料庫檔內容不變。WAL 模式的唯讀連線仍會建出 -shm 與空的 -wal（SQLite 的行為；正式站上
    app 開著時這兩個檔本來就在），所以只要求 -wal 沒有寫進任何東西。"""
    import hashlib
    _, s2 = _blocked_then_absent(h)
    db = Path(h.A.DB_PATH)
    wal = Path(str(db) + "-wal")
    wal_before = wal.read_bytes() if wal.exists() else b""
    digest = hashlib.sha256(db.read_bytes()).hexdigest()
    run_tool(db, str(s2))
    assert hashlib.sha256(db.read_bytes()).hexdigest() == digest
    assert (wal.read_bytes() if wal.exists() else b"") == wal_before


def test_missing_database_exits_nonzero(tmp_path):
    env = {**os.environ, "ATTEND_DB": str(tmp_path / "nope.db")}
    r = subprocess.run([sys.executable, str(TOOL)], capture_output=True, text=True, env=env, timeout=30)
    assert r.returncode == 2 and "找不到資料庫" in r.stderr, (r.returncode, r.stderr)


# 報告要看得到這些人

def _came_but_not_signed(h):
    """甲（9994A001）上週在環境 A 綁定簽到；本週環境 A 認得是他（whoami known）、也落地了，但沒有送出簽到，結案記自動缺席。"""
    s1 = h.open("CD")
    assert h.checkin(s1, "sr2-A", "9994A001", name="學生1", pin="1234")["ok"]
    h.T.post(f"/api/t/session/{s1}/close")
    s2 = h.open("CD")
    cl = h.dev("sr2-A")
    assert cl.post("/api/whoami", data={"course_id": h.cid["CD"]}).json()["known"] is True
    assert cl.post("/api/whoami", data={"course_id": h.cid["CD"]}).json()["known"] is True
    with h.A.db() as con:
        s = h.A.get_session(con, s2)
    slot = int(h.A.now() // 10)
    cl.get(f"/s/{s2}/{slot}/{h.A.slot_sig(s, slot)}", headers={"user-agent": "iPhone"})
    h.T.post(f"/api/t/session/{s2}/close")
    return s1, s2


def test_section_2b_lists_known_student_who_never_signed_without_identity(h):
    _, s2 = _came_but_not_signed(h)
    out = run_tool(h.A.DB_PATH, str(s2))
    assert "-- 掃到了（認得是誰）卻沒有簽到成功：1 人" in out, out
    part = out.split("-- 掃到了（認得是誰）卻沒有簽到成功", 1)[1].split("\n-- ", 1)[0]
    assert "S0" in part and "缺席" in part, part
    for secret in ("9994A001", "學生1"):
        assert secret not in out, (secret, out)


def test_section_2b_matches_by_landing_device_even_without_whoami(h):
    """沒有 whoami、只有落地（land_ok／land_closed）：落地的 device8 等於他目前綁定的裝置前 8 碼，也算認得是誰。"""
    s1 = h.open("CD")
    assert h.checkin(s1, "sr3-A", "9994A001", name="學生1", pin="1234")["ok"]
    h.T.post(f"/api/t/session/{s1}/close")
    s2 = h.open("CD")
    with h.A.db() as con:
        s = h.A.get_session(con, s2)
    slot = int(h.A.now() // 10)
    h.dev("sr3-A").get(f"/s/{s2}/{slot}/{h.A.slot_sig(s, slot)}", headers={"user-agent": "iPhone"})
    h.T.post(f"/api/t/session/{s2}/close")
    h.dev("sr3-A").get(f"/s/{s2}/{slot}/{h.A.slot_sig(s, slot)}", headers={"user-agent": "iPhone"})  # 結束後又掃一次：land_closed
    out = run_tool(h.A.DB_PATH, str(s2))
    assert "卻沒有簽到成功：1 人" in out, out
    assert "land_closed" in out.split("-- 裝置層", 1)[1], out


def test_section_2b_excludes_students_who_signed(h):
    s1 = h.open("CD")
    assert h.checkin(s1, "sr4-A", "9994A001", name="學生1", pin="1234")["ok"]
    h.dev("sr4-A").post("/api/whoami", data={"course_id": h.cid["CD"]})
    h.T.post(f"/api/t/session/{s1}/close")
    out = run_tool(h.A.DB_PATH, str(s1))
    assert "卻沒有簽到成功：0 人" in out, out


def test_reject_reasons_line_shows_code_and_chinese_label(h):
    s1 = h.open("CD")
    assert h.checkin(s1, "sr5-A", "9994A001", name="學生1", pin="1234")["ok"]
    h.T.post(f"/api/t/session/{s1}/close")
    s2 = h.open("CD")
    assert h.checkin(s2, "sr5-A", "9994A001", pin="1234")["ok"]
    r = h.dev("sr5-B").post("/api/checkin", data={"grant": h.grant(s2, h.dev("sr5-B")), "student_id": "9994A001", "pin": "1234", "pin2": "", "hints": "h"})
    assert r.status_code == 409, r.text
    out = run_tool(h.A.DB_PATH, str(s2))
    line = next(x for x in out.splitlines() if x.startswith("被拒原因："))
    assert "already_recorded" in line and "本場已有紀錄" in line, line


def test_report_label_dict_covers_every_app_reason_label(h):
    import importlib.util
    spec = importlib.util.spec_from_file_location("session_report", TOOL)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    for code, label in h.A.REJECT_REASON_LABEL.items():
        assert mod.REJECT_REASON_ZH.get(code) == label, (code, label, mod.REJECT_REASON_ZH.get(code))


def test_session_deleted_event_is_listed_in_teacher_actions_same_course_same_day_only(h):
    gone = h.open("CD")
    h.T.post(f"/api/t/session/{gone}/delete")
    other_gone = h.open("DT")
    h.T.post(f"/api/t/session/{other_gone}/delete")
    s2 = h.open("CD")
    out = run_tool(h.A.DB_PATH, str(s2))
    teacher_part = out.split("-- 老師操作", 1)[1].split("-- ", 1)[0]
    assert teacher_part.count("session_deleted") == 1 and "（場次外）" in teacher_part, teacher_part


def test_reopen_event_is_listed_in_teacher_actions(h):
    sid = h.open("CD")
    h.T.post(f"/api/t/session/{sid}/close")
    assert h.T.post(f"/api/t/session/{sid}/reopen").status_code == 200
    out = run_tool(h.A.DB_PATH, str(sid))
    teacher_part = out.split("-- 老師操作", 1)[1].split("-- ", 1)[0]
    assert "reopen" in teacher_part, teacher_part


# 刪除後重用的場次編號與重開場次

def test_report_ignores_events_from_before_a_deleted_and_reused_sid(h):
    import time_machine
    with time_machine.travel(h.A.now(), tick=False) as tr:
        sid = h.open("CD")
        cl = h.dev("sr-reuse")
        grant = h.grant(sid, cl)
        tr.shift(5)
        assert h.T.post(f"/api/t/session/{sid}/delete").json()["ok"] is True
        tr.shift(5)
        r = cl.post("/api/checkin", data={"grant": grant, "student_id": "9994A001", "name": "學生1", "pin": "1234", "pin2": "1234", "hints": "h"})
        assert r.status_code == 410, r.text  # 舊表單：場次已刪 → closed 事件記在這個場次號底下
        with h.A.db() as con:
            assert con.execute("SELECT COUNT(*) FROM events WHERE session_id=? AND reason='closed'", (sid,)).fetchone()[0] == 1
        tr.shift(5)
        sid2 = h.open("CD")
        assert sid2 == sid, "前提：刪除後重開沿用同一個場次號"
        tr.shift(5)
        out = run_tool(h.A.DB_PATH, str(sid2))
        assert "被拒原因" not in out and "checkin_reject" not in out and "closed" not in out.replace("closed_at", ""), out


def test_reopen_event_records_original_close_time_and_report_prints_it(h):
    import time_machine
    with time_machine.travel(h.A.now(), tick=False) as tr:
        sid = h.open("CD")
        tr.shift(60)
        h.T.post(f"/api/t/session/{sid}/close")
        closed_at = h.A.now()
        tr.shift(30)
        assert h.T.post(f"/api/t/session/{sid}/reopen").status_code == 200
        with h.A.db() as con:
            reason = con.execute("SELECT reason FROM events WHERE session_id=? AND kind='reopen'", (sid,)).fetchone()["reason"]
        assert reason == f"4｜原本 {h.A.fmt(closed_at)} 結束", reason
        out = run_tool(h.A.DB_PATH, str(sid))
        assert "原本" in out and h.A.fmt(closed_at) in out, out


# 旗標彙總、land_revisit

FLAG_KEYS = ("same_device", "device_bound_other", "name_variant", "proxy_for", "spot_absent", "qr_after_manual", "same_fp")


def _flag_line(out):
    return next(x for x in out.splitlines() if x.startswith("旗標彙總："))


def test_flag_summary_line_counts_each_flag_before_trouble_section(h):
    import json
    sid = h.open("CD")
    for i, stu in enumerate(("9994A001", "9994A002", "9994A003", "9994A004"), 1):
        assert h.checkin(sid, f"sr6-{i}", stu, name=f"學生{i}", pin="1234")["ok"]
    plan = {"9994A001": ["same_device", "same_fp"], "9994A002": ["same_device", "name_variant"],
            "9994A003": ["proxy_for", "spot_absent", "qr_after_manual", "device_bound_other"], "9994A004": ["ip_differs"]}
    with h.A.db() as con:
        for stu, flags in plan.items():
            con.execute("UPDATE checkins SET flags=? WHERE session_id=? AND student_pk=?", (json.dumps(flags), sid, h.pk("CD", stu)))
    out = run_tool(h.A.DB_PATH, str(sid))
    line = _flag_line(out)
    assert line == "旗標彙總：same_device 2、device_bound_other 1、name_variant 1、proxy_for 1、spot_absent 1、qr_after_manual 1、same_fp 1", line
    assert out.index("旗標彙總：") < out.index("-- 有狀況的學生"), out  # 在「有狀況的學生」之前
    for secret in ("9994A001", "學生1"):
        assert secret not in out


def test_flag_summary_line_present_with_zeros_when_no_flags(h):
    sid = h.open("CD")
    assert h.checkin(sid, "sr7-1", "9994A001", name="學生1", pin="1234")["ok"]
    line = _flag_line(run_tool(h.A.DB_PATH, str(sid)))
    assert line == "旗標彙總：" + "、".join(f"{k} 0" for k in FLAG_KEYS), line


def test_land_revisit_is_not_listed_as_device_failure(h):
    """已簽到的人回到舊的 QR 分頁，不是失敗。它出現在事件數裡可以，但不可列進「裝置層的失敗」。"""
    import importlib.util
    spec = importlib.util.spec_from_file_location("session_report", TOOL)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert "land_revisit" not in mod.DEVICE_FAIL_KINDS
    sid = h.open("CD")
    assert h.checkin(sid, "sr8-1", "9994A001", name="學生1", pin="1234")["ok"]
    with h.A.db() as con:
        con.execute("INSERT INTO events(session_id,kind,reason,device8,ua_class,ts) VALUES(?,?,?,?,?,?)",
                    (sid, "land_revisit", "", "abcd1234", "iOS", h.A.now() + 1))
    out = run_tool(h.A.DB_PATH, str(sid))
    assert "land_revisit" in out.split("-- 裝置層", 1)[0], "前提：事件數那一行有它"
    assert "land_revisit" not in out.split("-- 裝置層", 1)[1], out
