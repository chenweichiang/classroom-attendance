"""實地簽到摩擦的回歸測試。
每條測試對應一個實地摩擦的一句話根因。"""
import json
import re
import threading
from pathlib import Path

import time_machine

TEMPLATES = Path(__file__).parent.parent / "templates"


# favicon

def test_favicon_no_longer_404s(client):
    r = client.get("/favicon.ico")
    assert r.status_code != 404
    assert r.status_code == 204
    assert "max-age" in r.headers.get("cache-control", "")


# qr_grace 可以大於 qr_interval

def test_current_slots_grace_exceeds_interval_boundary(A):
    """interval=10、grace=30 的具體邊界，片結束後 29.9 秒仍有效、30.1 秒失效。"""
    sess = {"id": 1, "secret": "s"}
    settings = {"qr_interval": 10, "qr_grace": 30}
    slot_end = 100.0  # 片 9＝[90,100) 在 t=100 結束
    with time_machine.travel(slot_end + 29.9, tick=False):
        slots = A.current_slots(sess, settings)
        assert 9 in slots, f"29.9 秒內應仍有效: {slots}"
    with time_machine.travel(slot_end + 30.1, tick=False):
        slots = A.current_slots(sess, settings)
        assert 9 not in slots, f"30.1 秒應已失效: {slots}"


def test_current_slots_grace_spans_multiple_intervals(A):
    """grace 大於 interval 時要往前找不只一片，不是被夾在 interval 內就切斷。"""
    sess = {"id": 1, "secret": "s"}
    with time_machine.travel(105.0, tick=False):  # cur 片＝[100,110)，編號 10
        slots = A.current_slots(sess, {"qr_interval": 10, "qr_grace": 25})
        # 片 9=[90,100) 結束於 100，105-100=5<25 有效；片 8=[80,90) 結束於 90，105-90=15<25 有效；
        # 片 7=[70,80) 結束於 80，105-80=25，不 <25，失效
        assert sorted(slots) == [8, 9, 10], slots
        assert 10 in slots and 9 in slots and 8 in slots and 7 not in slots, slots


def test_course_settings_grace_clamp_is_120_not_interval(A, seeded):
    with A.db() as con:
        con.execute("UPDATE courses SET settings=? WHERE id=?",
                    (json.dumps({"qr_interval": 5, "qr_grace": 200}), seeded["CD"]))
        st = A.course_settings(A.get_course(con, seeded["CD"]))
    assert st["qr_grace"] == 120, "上限是 120，不是被 qr_interval（5）夾住"


def test_save_settings_grace_not_clamped_to_interval(h):
    cid = h.cid["CD"]
    h.T.post(f"/t/course/{cid}/settings", data={
        "open_minutes": 3, "late_minutes": 15, "qr_interval": 10, "qr_grace": 60,
        "spot_n": 3, "ip_check": "", "discord_channel": ""})
    with h.A.db() as con:
        st = h.A.course_settings(h.A.get_course(con, cid))
    assert st["qr_grace"] == 60, "grace（60）大於 interval（10）應該原樣保留，不是被夾到 10"


# QR 時效預設 10／30

def test_default_qr_grace_is_30_seconds(A):
    """qr_grace 預設 30、qr_interval 預設 10。"""
    assert A.DEFAULT_SETTINGS["qr_grace"] == 30
    assert A.DEFAULT_SETTINGS["qr_interval"] == 10


def test_no_context_pages_dont_hardcode_ten_seconds():
    """code.html/help.html/home.html 沒有課程脈絡（qr_grace 可能被個別課程改掉），
    寫死「每十秒」會在 grace 改大後講錯話——這幾頁一律改成不帶數字的說法。"""
    for name, snippets in (
        ("code.html", ["數字每十秒換一次"]),
        ("help.html", ["數字每十秒換一次", "QR 每十秒換"]),
        ("home.html", ["數字每十秒換"]),
    ):
        text = (TEMPLATES / name).read_text(encoding="utf-8")
        for bad in snippets:
            assert bad not in text, f"{name} 仍寫死「{bad}」"
        assert "每十秒" not in text, f"{name} 仍出現「每十秒」"


def test_help_anti_forward_paragraph_keeps_meaning():
    """help.html 的防轉傳那句保留原意：QR 會換、表單只能在開它的那支手機送出、老師會抽點。"""
    text = (TEMPLATES / "help.html").read_text(encoding="utf-8")
    assert "QR 會" in text and "換" in text
    assert "只能在開它的那支手機送出" in text
    assert "隨機抽人看" in text or "抽點" in text


def test_default_settings_grace_boundary_29s_valid_31s_invalid(A):
    """預設課程（未覆寫設定）：某時間片結束後第 29 秒該片仍有效、第 31 秒無效；六位數同理（code6 是同一個 sig 算出來的）。"""
    sess = {"id": 1, "secret": "s"}
    settings = dict(A.DEFAULT_SETTINGS)
    slot_end = 100.0  # 片 9=[90,100) 結束於 100
    with time_machine.travel(slot_end + 29, tick=False):
        slots = A.current_slots(sess, settings)
        assert 9 in slots, f"29 秒內應仍有效: {slots}"
    with time_machine.travel(slot_end + 31, tick=False):
        slots = A.current_slots(sess, settings)
        assert 9 not in slots, f"31 秒應已失效: {slots}"


def test_manage_set_qr_grace_allows_above_interval(A, seeded, capsys):
    import manage
    with A.db() as con:
        con.execute("INSERT INTO courses(code,name) VALUES('MNG','手動設定測試') ON CONFLICT(code) DO NOTHING")
    class Args:
        code = "MNG"; key = "qr_grace"; value = "80"
    manage.set_setting(Args())
    with A.db() as con:
        c = con.execute("SELECT * FROM courses WHERE code='MNG'").fetchone()
        st = A.course_settings(c)
    assert st["qr_grace"] == 80


# 换瀏覽器被誤導成首次綁定

def test_wrong_pin_with_pin2_always_explains_existing_pin(h):
    """非首次、帶了 pin2、PIN 錯 → 一律「已經設過 PIN」，不看是否已綁裝置（舊行為只在 device_hash 為空時給）。
    已綁裝置回簽免 PIN，這裡改用另一支裝置模擬換瀏覽器（不同 cookie 罐）才吃得到 PIN 驗證。"""
    sid = h.open("CD")
    h.checkin(sid, "devA", "9994A001", name="學生1", pin="1111")  # devA 綁定成功，device_hash 已設
    # 換瀏覽器＝不同 cookie 罐（用另一支裝置模擬 LINE 內建瀏覽器）：
    # 帶著 pin2（前端以為是首次），PIN 錯
    r = h.dev("devA-line").post("/api/checkin", data={
        "grant": h.grant(sid, h.dev("devA-line")), "student_id": "9994A001", "name": "學生1",
        "pin": "9999", "pin2": "9999"})
    assert r.status_code == 401
    assert "已經設過 PIN" in r.json()["error"]
    assert "原本的 PIN" in r.json()["error"]


def test_wrong_pin_without_pin2_gives_normal_countdown(h):
    """對照組：沒有帶 pin2（換手機情境）PIN 錯，訊息應是正常的「剩 N 次」，不是「已經設過 PIN」那句。
    已綁裝置回簽免 PIN，這裡改用另一支裝置才吃得到 PIN 驗證。"""
    sid = h.open("CD")
    h.checkin(sid, "devB", "9994A002", name="學生2", pin="2222")
    r = h.dev("devB2").post("/api/checkin", data={
        "grant": h.grant(sid, h.dev("devB2")), "student_id": "9994A002", "pin": "0000"})
    assert r.status_code == 401
    assert "剩" in r.json()["error"] and "已經設過 PIN" not in r.json()["error"]


def test_pin_wrong_message_has_exit(h):
    """pin_wrong 補出路：「忘記了請找老師重設」；鎖定訊息不變。"""
    sid = h.open("CD")
    h.checkin(sid, "cs9-owner", "9994A002", name="學生2", pin="2222")
    r = h.dev("cs9-wrong").post("/api/checkin", data={
        "grant": h.grant(sid, h.dev("cs9-wrong")), "student_id": "9994A002", "pin": "0000"})
    assert r.status_code == 401
    assert r.json()["error"] == "PIN 錯誤，剩 2 次。先確認學號沒打錯；忘記了請找老師重設"  # 照新文字改


# 姓名 NFKC 正規化

def test_norm_name_nfkc_and_zero_width(A):
    # 全形英數收斂
    assert A.norm_name("ＡＢ") == A.norm_name("AB")
    # 零寬字元（U+200B）不影響比對
    assert A.norm_name("王\u200b大明") == A.norm_name("王大明")
    # 不斷連字（U+200C／U+200D）與 BOM（U+FEFF）同理
    assert A.norm_name("王‌大‍明﻿") == A.norm_name("王大明")
    # 空白仍照舊被去掉
    assert A.norm_name("  王 大 明 ") == "王大明"


def test_checkin_first_time_name_with_zero_width_and_fullwidth_passes(h):
    sid = h.open("CD")
    r = h.dev("devZ").post("/api/checkin", data={
        "grant": h.grant(sid, h.dev("devZ")), "student_id": "9994A003",
        "name": "學\u200b生３", "pin": "3333", "pin2": "3333"})  # 名冊姓名是「學生3」，這裡夾零寬字元＋全形 3
    j = r.json()
    assert j["ok"] is True, j


# 學號全形英數轉半形

def test_to_halfwidth_converts_fullwidth_ascii(A):
    assert A.to_halfwidth("９９９４Ａ００１") == "9994A001"
    assert A.to_halfwidth("999000025") == "999000025"  # 半形字串不動
    assert A.to_halfwidth("王大明") == "王大明"  # 中文不動


def test_checkin_accepts_fullwidth_student_id(h):
    sid = h.open("CD")
    fw = "９９９４Ａ００１"  # 名冊裡是半形 9994A001
    r = h.dev("devFW").post("/api/checkin", data={
        "grant": h.grant(sid, h.dev("devFW")), "student_id": fw,
        "name": "學生1", "pin": "4444", "pin2": "4444"})
    j = r.json()
    assert j["ok"] is True and j["student_id"] == "9994A001", j


# 錯誤頁也下發裝置 cookie；沒 cookie 的限流放寬到 IP 同級

def test_expired_qr_page_sets_device_cookie(h):
    sid = h.open("CD")
    fresh = h.student()
    r = fresh.get(f"/s/{sid}/1/deadbeef00")  # 合法場次、假簽章 → 410（不是找不到場次的 404）
    assert r.status_code == 410
    assert any(c.startswith("attend_dk") for c in r.headers.get_list("set-cookie"))


def test_closed_session_page_sets_device_cookie(h):
    sid = h.open("CD")
    h.T.post(f"/api/t/session/{sid}/close")
    with h.A.db() as con:
        s = h.A.get_session(con, sid)
    slot = int(h.A.now() // 10)
    sig = h.A.slot_sig(s, slot)
    fresh = h.student()
    r = fresh.get(f"/s/{sid}/{slot}/{sig}")
    assert r.status_code == 200 and "點名已結束" in r.text
    assert any(c.startswith("attend_dk") for c in r.headers.get_list("set-cookie"))


def test_code_error_paths_set_device_cookie(A):
    from fastapi.testclient import TestClient
    for payload in ({"code": "000000"}, {"code": "not-a-live-session"}):
        fresh = TestClient(A.app, base_url="http://test")  # 每個情境各自一支「還沒有 cookie」的手機
        r = fresh.post("/c", data=payload)
        assert r.status_code in (400, 429)
        assert any(c.startswith("attend_dk") for c in r.headers.get_list("set-cookie")), payload


def test_no_cookie_fp_throttle_relaxed_to_ip_level(h):
    """同 IP 同 UA 的 30 個沒 cookie 的 client 依序打 /c 錯碼，不得有人 429。
    30 人同款手機同出口 IP 曾全部被 429。要有進行中的點名，錯碼才會真的落到節流計數（否則
    code_submit 在「沒有進行中的點名」就先回了，根本不會記錄失敗次數）。"""
    from fastapi.testclient import TestClient
    h.open("CD")
    hdr = {"cf-connecting-ip": "203.0.113.9", "user-agent": "iPhone-friction-test"}
    got_429 = []
    for i in range(30):
        c = TestClient(h.A.app, base_url="http://test")  # 每人一支全新手機，都不先拿 cookie
        r = c.post("/c", data={"code": "000000"}, headers=hdr)
        if r.status_code == 429:
            got_429.append(i)
    assert not got_429, f"沒有 cookie 的 30 人裡有人被 429：{got_429}"


# 首次綁定被裝置衝突擋下時不寫 PIN、不綁裝置

def test_first_time_bind_blocked_by_other_device_does_not_write_pin(h):
    """借用別人已綁定的手機、以一個從未登記過的學號做首次綁定 → 不寫 PIN、不綁裝置、狀態待確認。"""
    sid = h.open("CD")
    h.checkin(sid, "devA", "9994A001", name="學生1", pin="1111")  # devA 已綁 A
    r = h.dev("devA").post("/api/checkin", data={
        "grant": h.grant(sid, h.dev("devA")), "student_id": "9994A002", "name": "學生2",
        "pin": "8888", "pin2": "8888"})
    j = r.json()
    assert j["ok"] is True and j["status"] == "待確認", j
    with h.A.db() as con:
        b = con.execute("SELECT pin_hash, device_hash FROM students WHERE course_id=? AND student_id=?",
                        (h.cid["CD"], "9994A002")).fetchone()
    assert b["pin_hash"] is None, "手機是別人的，不該側寫 PIN"
    assert b["device_hash"] is None, "手機是別人的，不該綁裝置"


# 遲到改看「掃到」時間

def test_scanned_in_open_window_submitted_after_it_closes_is_present_no_was_late(h):
    """掃到（拿到 grant）當下在開放窗內，送出時窗已過但仍在 grant 時效內 → 出席、無 was_late 旗標。"""
    cid = h.cid["CD"]
    h.T.post(f"/t/course/{cid}/settings", data={
        "open_minutes": 1, "late_minutes": 15, "qr_interval": 10, "qr_grace": 30,
        "spot_n": 3, "ip_check": "", "discord_channel": ""})
    sid = h.open("CD")
    with time_machine.travel(h.A.now(), tick=False) as trav:
        phone = h.dev("a3-scan-open")
        grant = h.grant(sid, phone)  # 掃到＝現在，還在 1 分鐘開放窗內
        trav.shift(90)  # 送出時已過開放窗（60 秒），但還在 grant_seconds（預設 180 秒）內
        j = h.checkin(sid, "a3-scan-open", "9994A001", "學生1", pin="1234", grant=grant)
    assert j["ok"] is True
    assert j["status"] == "出席", j
    row = h.row(sid, "9994A001")
    assert "was_late" not in row["flags"], row["flags"]


def test_scanned_after_open_window_is_late(h):
    """掃到（拿到 grant）已經在遲到窗，即使立刻送出 → 遲到。"""
    cid = h.cid["CD"]
    h.T.post(f"/t/course/{cid}/settings", data={
        "open_minutes": 1, "late_minutes": 15, "qr_interval": 10, "qr_grace": 30,
        "spot_n": 3, "ip_check": "", "discord_channel": ""})
    sid = h.open("CD")
    with time_machine.travel(h.A.now(), tick=False) as trav:
        trav.shift(90)  # 已過開放窗（60 秒），還在遲到窗（15 分鐘）內
        j = h.checkin(sid, "a3-scan-late", "9994A002", "學生2", pin="1234")
    assert j["ok"] is True
    assert j["status"] == "遲到", j
    row = h.row(sid, "9994A002")
    assert "was_late" in row["flags"], row["flags"]


def test_past_late_until_still_closed_regardless_of_scan_time(h):
    """不變的行為：場次已過遲到窗時送出，仍回「點名已結束」，不因掃到時間在窗內而放行。"""
    cid = h.cid["CD"]
    with h.A.db() as con:
        con.execute("UPDATE courses SET settings=? WHERE id=?",
                    (json.dumps({"open_minutes": 1, "late_minutes": 2, "grant_seconds": 600}), cid))
    sid = h.open("CD")
    with time_machine.travel(h.A.now(), tick=False) as trav:
        phone = h.dev("a3-past-late")
        grant = h.grant(sid, phone)  # 掃到在開放窗（60 秒）內
        trav.shift(150)  # 遠超遲到窗（2 分鐘＝120 秒），但還在 grant_seconds（600 秒）內
        r = phone.post("/api/checkin", data={"grant": grant, "student_id": "9994A001", "name": "學生1",
                                              "pin": "1234", "pin2": "1234", "seat": "", "hints": "h"})
    assert r.status_code == 410
    # 沒有新場次時的訊息是「點名已經結束。有到課但沒簽到的同學，請直接找老師。」
    assert "點名已經結束" in r.json()["error"], r.json()


def test_old_three_part_grant_is_invalid(h):
    """三段式舊憑證（沒有發證時間那一段）不做相容，視為無效，回「填表時間已到，請重新掃描 QR」。"""
    sid = h.open("CD")
    A = h.A
    old_grant = A.make_token("g", str(sid), A.secrets.token_hex(6), exp=A.now() + 180)
    phone = h.dev("a3-old-grant")
    r = phone.post("/api/checkin", data={"grant": old_grant, "student_id": "9994A001", "name": "學生1",
                                         "pin": "1234", "pin2": "1234", "seat": "", "hints": "h"})
    assert r.status_code == 410
    assert "填表時間已到，請重新掃描 QR" in r.json()["error"]


# 姓名寬鬆比對

def _add_student(h, course, stu, name):
    with h.A.db() as con:
        con.execute("INSERT INTO students(course_id, student_id, name, cls) VALUES(?,?,?,?)",
                    (h.cid[course], stu, name, ""))


def test_exact_match_passes_no_flag(h):
    sid = h.open("CD")
    j = h.checkin(sid, "a4-exact", "9994A001", name="學生1", pin="1234")
    assert j["ok"] is True, j
    row = h.row(sid, "9994A001")
    assert "name_variant" not in row["flags"], row["flags"]


def test_variant_character_not_at_first_position_passes_with_flag(h):
    """溫/温 這類異體字打法（差異不在第一個字）：字數相同、第一個字相同 → 通過並帶 name_variant 旗標。"""
    _add_student(h, "CD", "A4S01", "林溫蒂")
    sid = h.open("CD")
    j = h.checkin(sid, "a4-variant", "A4S01", name="林温蒂", pin="1234")
    assert j["ok"] is True, j
    row = h.row(sid, "A4S01")
    assert "name_variant" in row["flags"], row["flags"]


def test_same_surname_same_length_different_name_passes_with_flag_known_tradeoff(h):
    """同姓同字數的不同名字也會通過並帶旗標——這是刻意放寬的已知代價，不是漏洞。"""
    _add_student(h, "CD", "A4S02", "王小明")
    sid = h.open("CD")
    j = h.checkin(sid, "a4-samesurname", "A4S02", name="王小華", pin="1234")
    assert j["ok"] is True, j
    row = h.row(sid, "A4S02")
    assert "name_variant" in row["flags"], row["flags"]


def test_different_length_is_rejected(h):
    _add_student(h, "CD", "A4S03", "王小明")
    sid = h.open("CD")
    j = h.checkin(sid, "a4-lendiff", "A4S03", name="王小明華", pin="1234")
    assert j["ok"] is False and "學號或姓名" in j["error"], j
    assert not h.row(sid, "A4S03")["status"]


def test_different_first_char_is_rejected(h):
    _add_student(h, "CD", "A4S04", "王小明")
    sid = h.open("CD")
    j = h.checkin(sid, "a4-firstdiff", "A4S04", name="李小明", pin="1234")
    assert j["ok"] is False and "學號或姓名" in j["error"], j
    assert not h.row(sid, "A4S04")["status"]


def test_existing_row_from_resubmit_not_retroactively_flagged(h):
    """已存在的列（already 早退路徑）不補這個旗標：正常首簽（exact）之後再次送出，即使這次帶了
    一個「字數相同、第一個字相同」的不同名字，既有列也不會因此被補上 name_variant（因為 already
    早退不會重新跑姓名比對）。"""
    sid = h.open("CD")
    j1 = h.checkin(sid, "a4-again", "9994A001", name="學生1", pin="1234")
    assert j1["ok"] is True and "name_variant" not in h.row(sid, "9994A001")["flags"]
    j2 = h.checkin(sid, "a4-again", "9994A001", name="學生九", pin="1234")  # 已綁定同手機，回原收據
    assert j2["ok"] is True and j2.get("again") is True
    assert "name_variant" not in h.row(sid, "9994A001")["flags"]


# 過期頁與錯誤頁加「找老師」

FIND_TEACHER_TEXT = "手機掃不過，不要借同學的手機，直接舉手找老師。"


def test_find_teacher_text_present_in_code_and_student_templates():
    code_text = (TEMPLATES / "code.html").read_text(encoding="utf-8")
    student_text = (TEMPLATES / "student.html").read_text(encoding="utf-8")
    assert FIND_TEACHER_TEXT in code_text, "code.html 說明段缺「找老師」文案"
    assert student_text.count(FIND_TEACHER_TEXT) >= 2, (
        "student.html 應該在過期頁與表單下方小字都出現「找老師」文案")


def test_expired_page_shows_find_teacher_text(h):
    sid = h.open("CD")
    bad_sig = "0" * 10
    r = h.dev("a5-expired").get(f"/s/{sid}/0/{bad_sig}")
    assert r.status_code == 410
    assert FIND_TEACHER_TEXT in r.text


# 已綁裝置回簽免 PIN

def test_bound_device_checkin_without_pin_succeeds(h):
    sid0 = h.open("CD")
    h.checkin(sid0, "a2-bound", "9994A001", name="學生1", pin="1234")  # 首次綁定
    h.T.post(f"/api/t/session/{sid0}/close")
    sid = h.open("CD")
    dev = h.dev("a2-bound")
    r = dev.post("/api/checkin", data={
        "grant": h.grant(sid, dev), "student_id": "9994A001", "name": "學生1", "pin": "", "seat": "", "hints": "h"})
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["ok"] is True and j["status"] == "出席"


def test_bound_device_wrong_pin_still_succeeds_and_no_fail_count(h):
    sid0 = h.open("CD")
    h.checkin(sid0, "a2-wrongpin", "9994A002", name="學生2", pin="1234")
    h.T.post(f"/api/t/session/{sid0}/close")
    sid = h.open("CD")
    dev = h.dev("a2-wrongpin")
    r = dev.post("/api/checkin", data={
        "grant": h.grant(sid, dev), "student_id": "9994A002", "name": "學生2", "pin": "0000", "seat": "", "hints": "h"})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "出席"
    with h.A.db() as con:
        rows = con.execute("SELECT n FROM pin_fail WHERE session_id=?", (sid,)).fetchall()
    assert not rows, "已綁裝置帶錯 PIN 不該計入 pin_fail"


def test_other_device_without_pin_still_rejected(h):
    sid0 = h.open("CD")
    h.checkin(sid0, "a2-owner", "9994A003", name="學生3", pin="1234")
    h.T.post(f"/api/t/session/{sid0}/close")
    sid = h.open("CD")
    dev = h.dev("a2-stranger")
    r = dev.post("/api/checkin", data={
        "grant": h.grant(sid, dev), "student_id": "9994A003", "name": "學生3", "pin": "", "seat": "", "hints": "h"})
    assert r.status_code == 400
    assert "PIN 要 4 到 6 位數字" in r.json()["error"]


def test_unbound_after_teacher_unbind_requires_old_pin(h):
    sid0 = h.open("CD")
    h.checkin(sid0, "a2-unbind-old", "9994A004", name="學生4", pin="1234")
    pk = h.pk("CD", "9994A004")
    h.T.post(f"/api/t/student/{pk}/unbind", data={"reset_pin": ""})  # 只解裝置，保留 PIN
    h.T.post(f"/api/t/session/{sid0}/close")
    sid = h.open("CD")
    newphone = h.dev("a2-newphone")
    r1 = newphone.post("/api/checkin", data={
        "grant": h.grant(sid, newphone), "student_id": "9994A004", "name": "學生4", "pin": "", "seat": "", "hints": "h"})
    assert r1.status_code == 400, "新裝置不帶 PIN 應該被拒"
    r2 = newphone.post("/api/checkin", data={
        "grant": h.grant(sid, newphone), "student_id": "9994A004", "name": "學生4", "pin": "1234", "seat": "", "hints": "h"})
    assert r2.status_code == 200, r2.text
    with h.A.db() as con:
        row = con.execute("SELECT device_hash FROM students WHERE id=?", (pk,)).fetchone()
    assert row["device_hash"] is not None, "帶對舊 PIN 應該成功並綁定新裝置"


def test_bound_device_not_locked_out_by_others_wrong_pins(h):
    """平日免 PIN 之後，PIN 鎖定不該擋到本人已綁的那支手機。
    不然同學從別支裝置亂猜某人的 PIN 猜到全場上限，就能讓他本人這堂課簽不了到。"""
    sid0 = h.open("CD")
    h.checkin(sid0, "a2-victim", "9994A001", name="學生1", pin="1234")
    h.T.post(f"/api/t/session/{sid0}/close")
    sid = h.open("CD")
    pk = h.pk("CD", "9994A001")
    with h.A.db() as con:  # 直接把「全場該生總計」灌到上限，等同別支裝置已猜錯這麼多次
        con.execute("INSERT INTO pin_fail(session_id, student_pk, device_hash, n) VALUES(?,?,'',?)",
                    (sid, pk, h.A.PIN_MAX_FAIL_TOTAL))
    stranger = h.dev("a2-attacker")
    r = stranger.post("/api/checkin", data={
        "grant": h.grant(sid, stranger), "student_id": "9994A001", "name": "學生1", "pin": "9999", "seat": "", "hints": "h"})
    assert r.status_code == 423, "別支裝置仍應被鎖"
    owner = h.dev("a2-victim")
    r = owner.post("/api/checkin", data={
        "grant": h.grant(sid, owner), "student_id": "9994A001", "name": "學生1", "pin": "", "seat": "", "hints": "h"})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "出席"


# 姓名寫法不同的首綁一律轉待確認


def test_variant_first_bind_is_pending_with_both_flags(h):
    """姓名 variant 的首次綁定：跟 same_device 同一套待確認機制，狀態記 pending，
    且同時帶 late_bind 與 name_variant 兩個旗標；PIN 與裝置照常綁定，不擋。"""
    _add_student(h, "CD", "CS1S01", "林溫蒂")
    sid = h.open("CD")
    j = h.checkin(sid, "cs1-variant", "CS1S01", name="林温蒂", pin="1234")
    assert j["ok"] is True, j
    row = h.row(sid, "CS1S01")
    assert row["status"] == "pending", row
    assert "name_variant" in row["flags"] and "late_bind" in row["flags"], row["flags"]
    with h.A.db() as con:
        stu = con.execute("SELECT device_hash, pin_hash FROM students WHERE student_id='CS1S01'").fetchone()
    assert stu["device_hash"] is not None and stu["pin_hash"] is not None, "PIN 與裝置仍應照常綁定"


def test_exact_first_bind_still_present(h):
    """exact 首綁行為不變：沒有 name_variant，狀態照舊是出席（開放窗內）。"""
    sid = h.open("CD")
    j = h.checkin(sid, "cs1-exact", "9994A001", name="學生1", pin="1234")
    assert j["ok"] is True, j
    row = h.row(sid, "9994A001")
    assert row["status"] == "present", row
    assert "name_variant" not in row["flags"], row["flags"]


def test_variant_spotcheck_present_restores_was_late(h):
    """抽點判在場後依 was_late 還原成出席／遲到（既有邏輯，這裡補斷言）：variant 首綁若掃到時
    已過開放窗、落在遲到窗內，pending 狀態帶 was_late；老師抽點判在場時要還原成遲到，不是出席。"""
    cid = h.cid["CD"]
    h.T.post(f"/t/course/{cid}/settings", data={
        "open_minutes": 1, "late_minutes": 15, "qr_interval": 10, "qr_grace": 30,
        "spot_n": 3, "ip_check": "", "discord_channel": ""})
    _add_student(h, "CD", "CS1S02", "林溫蒂")
    sid = h.open("CD")
    with time_machine.travel(h.A.now(), tick=False) as trav:
        trav.shift(90)  # 已過開放窗（60 秒），還在遲到窗（15 分鐘）內
        j = h.checkin(sid, "cs1-variant-late", "CS1S02", name="林温蒂", pin="1234")
    assert j["ok"] is True, j
    row = h.row(sid, "CS1S02")
    assert row["status"] == "pending" and "was_late" in row["flags"], row
    pick = h.T.post(f"/api/t/session/{sid}/spotcheck").json()["picks"][0]
    assert pick["student_id"] == "CS1S02", pick
    r = h.T.post(f"/api/t/spotcheck/{pick['id']}", data={"present": "1"})
    assert r.status_code == 200, r.text
    row2 = h.row(sid, "CS1S02")
    assert row2["status"] == "late", row2


# 簽到的讀後寫放進同一個寫入交易

def test_race_same_device_two_students_first_bind_only_one_wins(h):
    """兩個執行緒用同一支裝置 cookie 同時替兩個學號首綁，結果必須跟循序送出一致：
    只有一人真的綁到這支裝置，兩筆簽到都帶 same_device 旗標且都是待確認。跑 20 輪不得有一輪兩人都綁上。"""
    for i in range(20):
        sid = h.open("CD")
        dev = h.dev(f"racedev{i}")
        g1 = h.grant(sid, dev)
        g2 = h.grant(sid, dev)
        results = {}
        barrier = threading.Barrier(2)

        def go(tag, grant, stu, nm):
            barrier.wait()
            results[tag] = dev.post("/api/checkin", data={
                "grant": grant, "student_id": stu, "name": nm, "pin": "1111", "pin2": "1111", "hints": "h"},
                headers={"user-agent": "iPhone", "cf-connecting-ip": "140.1.1.1"})

        t1 = threading.Thread(target=go, args=("A", g1, "9994A001", "學生1"))
        t2 = threading.Thread(target=go, args=("B", g2, "9994A002", "學生2"))
        t1.start(); t2.start(); t1.join(10); t2.join(10)
        for tag in ("A", "B"):
            assert results[tag].status_code == 200, (i, tag, results[tag].text)
        with h.A.db() as con:
            bound = [r["student_id"] for r in con.execute(
                "SELECT student_id FROM students WHERE course_id=? AND device_hash IS NOT NULL", (h.cid["CD"],))]
        assert len(bound) == 1, f"第 {i} 輪：應只有一人綁到這支裝置，實得 {bound}"
        rows = h.db_rows(sid)
        for stu in ("9994A001", "9994A002"):
            assert "same_device" in json.loads(rows[stu]["flags"]), (i, stu, rows[stu])
            assert rows[stu]["status"] == "pending", (i, stu, rows[stu])
        with h.A.db() as con:  # 清乾淨，下一輪重來
            con.execute("UPDATE students SET device_hash=NULL, bound_at=NULL, pin_hash=NULL, pin_salt=NULL WHERE course_id=?",
                        (h.cid["CD"],))
        h.T.post(f"/api/t/session/{sid}/close")


# 簽到憑證在下發時就綁裝置

def test_relayed_grant_rejected_on_first_use_by_other_device(h):
    """教室內同學把 GRANT 字串轉給場外的人，場外那支手機第一次使用就該被拒，且不能佔走 grants 表
    （被拒之後，真正掃到的那支手機自己送出仍應成功）。"""
    sid = h.open("CD")
    devA = h.dev("s5-a")
    devB = h.dev("s5-b")
    grant = h.grant(sid, devA)
    r = devB.post("/api/checkin", data={"grant": grant, "student_id": "9994A001", "name": "學生1",
                                         "pin": "1234", "pin2": "1234", "hints": "h"})
    assert r.status_code == 410, r.text
    # 這句是中性訊息，不講「別支手機掃到的」（像在指控本人）
    assert r.json()["error"] == "這份簽到表單和這支手機對不上（可能重新整理過或開了兩個分頁），請重新掃描 QR"
    with h.A.db() as con:
        gsig = grant.rsplit(":", 1)[-1]
        row = con.execute("SELECT device_hash FROM grants WHERE sig=?", (gsig,)).fetchone()
    assert row is None, "被拒的別支裝置不該佔走 grants 表"
    r2 = devA.post("/api/checkin", data={"grant": grant, "student_id": "9994A001", "name": "學生1",
                                          "pin": "1234", "pin2": "1234", "hints": "h"})
    assert r2.status_code == 200, r2.text


def test_first_load_without_cookie_cookie_and_grant_match(h):
    """`/c` 六位數路徑走同一個 render_student：沒有 cookie 的首次載入，回應裡下發的 cookie
    與 grant 第五段的裝置雜湊要互相對得上。"""
    sid = h.open("CD")
    code = h.T.get(f"/api/t/session/{sid}/qr").json()["code"]
    fresh = h.student()  # 全新 client，完全沒有 cookie
    assert fresh.cookies.get("attend_dk") is None
    r = fresh.post("/c", data={"code": code})
    assert r.status_code == 200, r.text
    ck = fresh.cookies.get("attend_dk")
    assert ck, "第一次載入也該下發裝置 cookie"
    grant = re.search(r'GRANT="([^"]+)"', r.text).group(1)
    dh16 = grant.split(":")[4]
    assert dh16 == h.A.hash_device(ck)[:16], "同一個回應裡的 cookie 與憑證要對得上"


def test_four_part_legacy_grant_is_invalid(h):
    sid = h.open("CD")
    dev = h.dev("s5-legacy")
    old_grant = h.A.make_token("g", str(sid), "abcdef123456", str(h.A.now()), exp=h.A.now() + 180)
    r = dev.post("/api/checkin", data={"grant": old_grant, "student_id": "9994A001", "name": "學生1",
                                        "pin": "1234", "pin2": "1234", "hints": "h"})
    assert r.status_code == 410, r.text


# 出口 IP 限流桶不擋已綁裝置

def test_bound_device_exempt_from_ip_throttle(h):
    sid0 = h.open("CD")
    boundee = h.dev("s3-bound")
    resp0 = h.checkin(sid0, "s3-bound", "9994A001", name="學生1", pin="1234")
    assert resp0["ok"] is True
    h.T.post(f"/api/t/session/{sid0}/close")
    sid = h.open("CD")
    NAT = "140.9.9.9"
    last_status = None
    # 21 支未綁裝置各打 12 次＝252 次，超過每 IP 240 次／分的上限（20×12 剛好卡在邊界不會觸發）
    for j in range(21):
        cl = h.student()
        cl.cookies.set("attend_dk", f"unbound{j:028d}")
        code = h.T.get(f"/api/t/session/{sid}/qr").json()["code"]
        html = cl.post("/c", data={"code": code}, headers={"user-agent": "atk", "cf-connecting-ip": NAT}).text
        grant = re.search(r'GRANT="([^"]+)"', html).group(1)
        for _ in range(12):
            r = cl.post("/api/checkin", data={"grant": grant, "student_id": "S999", "name": "", "pin": "0000",
                                                "pin2": "", "hints": "h"}, headers={"user-agent": "atk", "cf-connecting-ip": NAT})
            last_status = r.status_code
            if last_status == 429:
                break
        if last_status == 429:
            break
    assert last_status == 429, "應該已經把出口 IP 的未綁裝置桶灌爆"
    r_bound = boundee.post("/api/checkin", data={
        "grant": h.grant(sid, boundee), "student_id": "9994A001", "name": "", "pin": "", "pin2": "", "hints": "h"},
        headers={"user-agent": "iPhone", "cf-connecting-ip": NAT})
    assert r_bound.status_code == 200, r_bound.text
    fresh_dev = h.dev("s3-fresh")
    r_unbound = fresh_dev.post("/api/checkin", data={
        "grant": h.grant(sid, fresh_dev), "student_id": "9994A002", "name": "學生2", "pin": "5555", "pin2": "5555", "hints": "h"},
        headers={"user-agent": "iPhone", "cf-connecting-ip": NAT})
    assert r_unbound.status_code == 429, r_unbound.text


# 裝置 cookie 使用 __Host- 前綴

class _FakeReq:
    """函式層測試：只需要 .cookies，不必真的發 HTTP 請求。"""
    def __init__(self, cookies):
        self.cookies = cookies


def test_http_mode_keeps_legacy_cookie_name(A):
    """本機與測試走 http，瀏覽器不收沒有 Secure 的 __Host- cookie，http 下維持舊名。"""
    assert A.DK_COOKIE == "attend_dk"
    assert A.BASE_URL.startswith("http://")


def test_https_mode_new_name_preferred_over_legacy(A, monkeypatch):
    monkeypatch.setattr(A, "DK_COOKIE", "__Host-attend_dk")
    req = _FakeReq({"__Host-attend_dk": "newname000000000000000000", "attend_dk": "oldname000000000000000000"})
    assert A.effective_device_key(req) == "newname000000000000000000"


def test_https_mode_legacy_adopted_only_if_bound(A, monkeypatch):
    monkeypatch.setattr(A, "DK_COOKIE", "__Host-attend_dk")
    legacy = "boundoldname0000000000000"
    with A.db() as con:
        con.execute("INSERT INTO courses(code,name) VALUES('CD','脈絡設計')")
        cid = con.execute("SELECT id FROM courses").fetchone()["id"]
        con.execute("INSERT INTO students(course_id,student_id,name,device_hash) VALUES(?,?,?,?)",
                    (cid, "9994A001", "學生1", A.hash_device(legacy)))
    A_req = _FakeReq({"attend_dk": legacy})
    assert A.effective_device_key(A_req) == legacy, "9/10 已綁的舊名裝置不必重綁"


def test_https_mode_legacy_adopted_with_explicit_con(A, monkeypatch):
    """con 有給就沿用呼叫端的連線查舊名有沒有綁，不必臨時開一條（student_entry／render_student
    都是這樣用的，真實請求走的是這條，不是上一條測試用的臨時開連線那條）。"""
    monkeypatch.setattr(A, "DK_COOKIE", "__Host-attend_dk")
    legacy = "boundoldname0000000000001"
    with A.db() as con:
        con.execute("INSERT INTO courses(code,name) VALUES('CD','脈絡設計')")
        cid = con.execute("SELECT id FROM courses").fetchone()["id"]
        con.execute("INSERT INTO students(course_id,student_id,name,device_hash) VALUES(?,?,?,?)",
                    (cid, "9994A002", "學生2", A.hash_device(legacy)))
        A_req = _FakeReq({"attend_dk": legacy})
        assert A.effective_device_key(A_req, con) == legacy, "con 有給時應沿用它查綁定，不臨時另開連線"


def test_https_mode_malformed_legacy_cookie_ignored(A, monkeypatch):
    """格式不合法（太短）的舊名 cookie 一律當沒有 cookie，不查資料庫。"""
    monkeypatch.setattr(A, "DK_COOKIE", "__Host-attend_dk")
    A_req = _FakeReq({"attend_dk": "too-short"})
    assert A.effective_device_key(A_req) == ""


def test_https_mode_legacy_ignored_if_unbound(A, monkeypatch):
    monkeypatch.setattr(A, "DK_COOKIE", "__Host-attend_dk")
    req = _FakeReq({"attend_dk": "unboundoldname0000000000000"})
    assert A.effective_device_key(req) == "", "未綁的舊名一律忽略、當作沒有 cookie"


def test_student_entry_calls_effective_device_key_once(h, monkeypatch):
    """student_entry 自己的函式主體舊版呼叫了兩次（device8 那一行算一次、if 條件又算一次），
    要收斂成一次；下游 render_student／ensure_device_cookie_value 各自為了自己的目的另外呼叫，
    不算在 student_entry 這一支的次數內，所以用呼叫端frame 名稱過濾，只數「直接由 student_entry
    呼叫」的那幾次。"""
    import traceback
    dev = h.dev("s7-entry")  # 先暖身拿到裝置 cookie，monkeypatch 之後才不會把 /c 的呼叫也算進去
    sid = h.open("CD")
    calls = []
    orig = h.A.effective_device_key

    def counting(req, con=None):
        caller = traceback.extract_stack()[-2].name
        if caller == "student_entry":
            calls.append(1)
        return orig(req, con)

    monkeypatch.setattr(h.A, "effective_device_key", counting)
    with h.A.db() as con:
        srow = h.A.get_session(con, sid)
        crow = h.A.get_course(con, srow["course_id"])
        stt = h.A.course_settings(crow)
    iv = stt["qr_interval"]
    slot = int(h.A.now() // iv)
    sig = h.A.slot_sig(srow, slot)
    r = dev.get(f"/s/{sid}/{slot}/{sig}")
    assert r.status_code == 200, r.text
    assert len(calls) == 1, f"student_entry 應該只呼叫一次 effective_device_key，實得 {len(calls)}"


# 出席／遲到看同一支手機本場最早掃到的時間

def _land(h, sid, dev):
    """透過 /s/{sid}/{slot}/{sig} 落地（跟真正掃 QR 同一條路徑，會留下 land_ok 事件），回傳這次的 GRANT。"""
    with h.A.db() as con:
        s = h.A.get_session(con, sid)
        c = h.A.get_course(con, s["course_id"])
        st = h.A.course_settings(c)
    iv = st["qr_interval"]
    slot = int(h.A.now() // iv)
    sig = h.A.slot_sig(s, slot)
    html = h.dev(dev).get(f"/s/{sid}/{slot}/{sig}").text
    m = re.search(r'GRANT="([^"]+)"', html)
    assert m, html
    return m.group(1)


def _set_open_60s(h):
    h.T.post(f"/t/course/{h.cid['CD']}/settings", data={
        "open_minutes": 1, "late_minutes": 15, "qr_interval": 10, "qr_grace": 30,
        "spot_n": 3, "ip_check": "", "discord_channel": ""})


def test_earliest_land_within_window_is_present_no_was_late(h):
    """開放窗 60 秒：同一裝置開場 50 秒落地、70 秒重新落地並用第二張憑證送出 → 出席、無 was_late。"""
    _set_open_60s(h)
    with time_machine.travel(h.A.now(), tick=False) as trav:
        sid = h.open("CD")
        trav.shift(50)
        _land(h, sid, "dev2-a")  # 第一次落地：開場後 50 秒，還在開放窗內
        trav.shift(20)  # 現在開場後 70 秒
        grant2 = _land(h, sid, "dev2-a")  # 同一支裝置重新落地，第二張憑證
        j = h.dev("dev2-a").post("/api/checkin", data={
            "grant": grant2, "student_id": "9994A001", "name": "學生1", "pin": "1111", "pin2": "1111", "hints": "h"}).json()
    assert j["ok"] is True, j
    assert j["status"] == "出席", j
    row = h.row(sid, "9994A001")
    assert "was_late" not in row["flags"], row["flags"]


def test_only_late_land_is_still_late(h):
    """只在 70 秒落地一次 → 遲到＋was_late（原行為不變）。"""
    _set_open_60s(h)
    with time_machine.travel(h.A.now(), tick=False) as trav:
        sid = h.open("CD")
        trav.shift(70)
        grant = _land(h, sid, "dev2-b")
        j = h.dev("dev2-b").post("/api/checkin", data={
            "grant": grant, "student_id": "9994A002", "name": "學生2", "pin": "2222", "pin2": "2222", "hints": "h"}).json()
    assert j["ok"] is True, j
    assert j["status"] == "遲到", j
    row = h.row(sid, "9994A002")
    assert "was_late" in row["flags"], row["flags"]


def test_other_device_early_land_does_not_help_this_device(h):
    """另一支裝置在 50 秒落地，不影響這支裝置 70 秒落地的判定（仍是遲到）。"""
    _set_open_60s(h)
    with time_machine.travel(h.A.now(), tick=False) as trav:
        sid = h.open("CD")
        trav.shift(50)
        _land(h, sid, "dev2-other")  # 別支手機 50 秒落地
        trav.shift(20)
        grant = _land(h, sid, "dev2-c")  # 這支手機只在 70 秒落地一次
        j = h.dev("dev2-c").post("/api/checkin", data={
            "grant": grant, "student_id": "9994A003", "name": "學生3", "pin": "3333", "pin2": "3333", "hints": "h"}).json()
    assert j["ok"] is True, j
    assert j["status"] == "遲到", j
    row = h.row(sid, "9994A003")
    assert "was_late" in row["flags"], row["flags"]


def test_previous_session_land_ok_not_counted(h):
    """上一場的 land_ok 不算進這一場。"""
    _set_open_60s(h)
    sid0 = h.open("CD")
    _land(h, sid0, "dev2-d")  # 上一場的落地（此刻）
    h.T.post(f"/api/t/session/{sid0}/close")
    with time_machine.travel(h.A.now(), tick=False) as trav:
        sid = h.open("CD")
        trav.shift(70)  # 這一場只在 70 秒落地一次
        grant = _land(h, sid, "dev2-d")  # 同一支裝置
        j = h.dev("dev2-d").post("/api/checkin", data={
            "grant": grant, "student_id": "9994A004", "name": "學生4", "pin": "4444", "pin2": "4444", "hints": "h"}).json()
    assert j["ok"] is True, j
    assert j["status"] == "遲到", j


def test_c_dot_code_path_has_no_land_ok_unaffected(h):
    """經 /c（六位數）拿到的憑證沒有 land_ok 事件，行為不變：一樣看 issued（掃到時間）。"""
    _set_open_60s(h)
    with time_machine.travel(h.A.now(), tick=False) as trav:
        sid = h.open("CD")
        trav.shift(70)  # 這個管道本來就沒有落地事件，直接在遲到窗內才拿代碼
        phone = h.dev("dev2-e")
        grant = h.grant(sid, phone)  # 走 /c，不是 /s/.../.../...，沒有 land_ok 事件；issued 就是這一刻（70 秒）
        j = phone.post("/api/checkin", data={
            "grant": grant, "student_id": "9994A001", "name": "學生1", "pin": "1111", "pin2": "1111", "hints": "h"}).json()
    assert j["ok"] is True, j
    assert j["status"] == "遲到", j


# 同一個錯 PIN 的重送不重複計次

def test_same_wrong_pin_resend_not_double_counted(h):
    sid0 = h.open("CD")
    h.checkin(sid0, "dev3-owner", "9994A001", name="學生1", pin="1234")
    h.T.post(f"/api/t/session/{sid0}/close")
    sid = h.open("CD")
    attacker = h.dev("dev3-attacker")

    def wrong(pin):
        return attacker.post("/api/checkin", data={
            "grant": h.grant(sid, attacker), "student_id": "9994A001", "name": "學生1", "pin": pin}).json()

    r1 = wrong("0000")
    assert r1["ok"] is False and "剩 2 次" in r1["error"], r1
    r2 = wrong("0000")  # 同一個錯 PIN 60 秒內重送，不重複計次
    assert "剩 2 次" in r2["error"], r2
    r3 = wrong("9999")  # 換一組錯的 PIN，照現在規則正常累加
    assert "剩 1 次" in r3["error"], r3


def test_same_wrong_pin_after_dedup_window_counts_again(h):
    sid0 = h.open("CD")
    h.checkin(sid0, "dev3b-owner", "9994A002", name="學生2", pin="1234")
    h.T.post(f"/api/t/session/{sid0}/close")
    sid = h.open("CD")
    attacker = h.dev("dev3b-attacker")
    r1 = attacker.post("/api/checkin", data={
        "grant": h.grant(sid, attacker), "student_id": "9994A002", "name": "學生2", "pin": "0000"}).json()
    assert "剩 2 次" in r1["error"], r1
    with time_machine.travel(h.A.now(), tick=False) as trav:
        trav.shift(61)  # 超過 60 秒的重送窗
        r2 = attacker.post("/api/checkin", data={
            "grant": h.grant(sid, attacker), "student_id": "9994A002", "name": "學生2", "pin": "0000"}).json()
    assert "剩 1 次" in r2["error"], r2


# 待確認的收據要說對原因

def test_pending_reason_name_variant(h):
    _add_student(h, "CD", "DEV1S01", "林溫蒂")
    sid = h.open("CD")
    j = h.checkin(sid, "dev4-variant", "DEV1S01", name="林温蒂", pin="1234")
    assert j["ok"] is True, j
    assert j["status"] == "待確認", j
    assert j["pending_reason"] == "name_variant", j


def test_pending_reason_same_device(h):
    sid = h.open("CD")
    dev = h.dev("dev4-same")
    h.checkin(sid, "dev4-same", "9994A001", name="學生1", pin="1234")
    j2 = dev.post("/api/checkin", data={
        "grant": h.grant(sid, dev), "student_id": "9994A002", "name": "學生2", "pin": "5555", "pin2": "5555"}).json()
    assert j2["ok"] is True, j2
    assert j2["status"] == "待確認", j2
    assert j2["pending_reason"] == "same_device", j2


def test_student_html_notes_distinguish_reasons():
    """name_variant 的文案是提醒核對學號、多半是打錯。"""
    text = (TEMPLATES / "student.html").read_text(encoding="utf-8")
    assert "你填的姓名和名冊的寫法不完全一樣。請先看上面的學號和姓名是不是你本人：不是的話馬上舉手找老師（多半是學號打錯）；是的話老師會當場確認，確認前不算出席。" in text
    assert "系統看到這支手機簽過別的學號，老師會當場確認，確認前不算出席。" in text
    assert "pending_reason" in text


# /me 逾時與倒數餘裕

def test_student_html_countdown_starts_at_grant_seconds_minus_5():
    text = (TEMPLATES / "student.html").read_text(encoding="utf-8")
    assert "Math.max(10,GRANT_SECONDS-5)" in text, "倒數初始值沒有改成 GRANT_SECONDS-5（最少 10 秒）"


def test_me_html_has_10s_fetch_timeout():
    text = (TEMPLATES / "me.html").read_text(encoding="utf-8")
    assert "timeoutSignal(10000)" in text, "/api/me 沒有加 10 秒逾時"


# 打錯學號撞到同學、PIN 錯誤訊息講清楚次數
# 每個測試用獨一無二的裝置名稱（h.dev 的 cookie 罐跨測試共用）。

def _ci10(h, sid, dev, stu, name="", pin="", pin2=""):
    cl = h.dev(dev)
    return cl.post("/api/checkin", data={"grant": h.grant(sid, cl), "student_id": stu, "name": name, "pin": pin,
                                         "pin2": pin2, "hints": "h"},
                   headers={"user-agent": "iPhone", "cf-connecting-ip": "140.131.1.1"})


def _two_bound(h, tag, pin_a="1111", pin_b="2222"):
    """甲（9994A001）、乙（9994A002）各自首綁並簽到，結束；回本週新的一場 sid。
    夾具名冊的「學生1」「學生2」同字數同首字，彼此就是 variant（寬鬆比對放行），測不出姓名不符，
    所以把兩人改成首字不同的名字。"""
    with h.A.db() as con:
        con.execute("UPDATE students SET name='王小明' WHERE course_id=? AND student_id='9994A001'", (h.cid["CD"],))
        con.execute("UPDATE students SET name='李大華' WHERE course_id=? AND student_id='9994A002'", (h.cid["CD"],))
    s1 = h.open("CD")
    assert h.checkin(s1, f"{tag}-甲", "9994A001", name="王小明", pin=pin_a)["ok"]
    assert h.checkin(s1, f"{tag}-乙", "9994A002", name="李大華", pin=pin_b)["ok"]
    h.T.post(f"/api/t/session/{s1}/close")
    return h.open("CD")


def _dev_hash_of(h, stu):
    with h.A.db() as con:
        return con.execute("SELECT device_hash FROM students WHERE course_id=? AND student_id=?",
                           (h.cid["CD"], stu)).fetchone()["device_hash"]


def _pin_fail_rows(h, stu):
    with h.A.db() as con:
        return con.execute("SELECT * FROM pin_fail WHERE student_pk=?", (h.pk("CD", stu),)).fetchall()


def test_typo_id_with_own_name_rejected_without_touching_classmate(h):
    s2 = _two_bound(h, "twobound-a")
    before = _dev_hash_of(h, "9994A002")
    r = _ci10(h, s2, "twobound-a-new", "9994A002", name="王小明", pin="1111", pin2="1111")
    assert r.status_code == 400, r.text
    assert "名冊不符" in r.json()["error"], r.text
    assert _pin_fail_rows(h, "9994A002") == []
    assert _dev_hash_of(h, "9994A002") == before


def test_typo_id_same_pin_does_not_move_classmates_binding(h):
    s2 = _two_bound(h, "twobound-b", pin_a="1111", pin_b="1111")
    before = _dev_hash_of(h, "9994A002")
    r = _ci10(h, s2, "twobound-b-new", "9994A002", name="王小明", pin="1111", pin2="1111")
    assert r.status_code == 400, r.text
    assert _dev_hash_of(h, "9994A002") == before
    assert "9994A002" not in h.db_rows(s2)


def test_own_id_and_roster_name_still_rebinds(h):
    s2 = _two_bound(h, "twobound-c")
    r = _ci10(h, s2, "twobound-c-new", "9994A001", name="王小明", pin="1111", pin2="1111")
    assert r.status_code == 200, r.text
    assert r.json()["rebound"] is True, r.text


def test_variant_name_rebinds_as_pending(h):
    # 換瀏覽器改綁時姓名只是同姓同字數：PIN 對照樣改綁，但轉待確認
    s2 = _two_bound(h, "twobound-d")
    r = _ci10(h, s2, "twobound-d-new", "9994A001", name="王小華", pin="1111", pin2="1111")  # 同字數、同首字
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["rebound"] is True and j["status"] == "待確認" and j.get("pending_reason") == "name_variant", j


def test_pin_only_mode_empty_name_not_compared(h):
    s2 = _two_bound(h, "twobound-e")
    r = _ci10(h, s2, "twobound-e-new", "9994A001", name="", pin="1111", pin2="")
    assert r.status_code == 200, r.text


def test_pin_exists_message_counts_the_attempt(h):
    s2 = _two_bound(h, "pinmsg-a")
    r = _ci10(h, s2, "pinmsg-a-new", "9994A001", name="王小明", pin="9991", pin2="9991")
    assert r.status_code == 401, r.text
    err = r.json()["error"]
    assert "已經設過 PIN" in err and "剩 2 次" in err, err
    assert err == ("這個學號已經設過 PIN，你輸入的不是原本那組（剩 2 次）。先確認學號沒打錯，再輸入原本的 PIN；忘記了請找老師重設")
    r = _ci10(h, s2, "pinmsg-a-new", "9994A001", pin="9992")
    assert r.status_code == 401 and r.json()["error"] == "PIN 錯誤，剩 1 次。先確認學號沒打錯；忘記了請找老師重設", r.text
    r = _ci10(h, s2, "pinmsg-a-new", "9994A001", pin="9993")
    print("第三次錯：", r.status_code, r.json()["error"])
    assert "錯太多次" in r.json()["error"], r.text
    r = _ci10(h, s2, "pinmsg-a-new", "9994A001", pin="9994")
    print("第四次：", r.status_code, r.json()["error"])
    assert r.status_code == 423, r.text


def test_pin_exists_message_when_already_at_limit(h):
    s2 = _two_bound(h, "pinmsg-b")
    for p in ("9991", "9992"):
        assert _ci10(h, s2, "pinmsg-b-new", "9994A001", pin=p).status_code == 401
    r = _ci10(h, s2, "pinmsg-b-new", "9994A001", name="王小明", pin="9993", pin2="9993")
    assert r.status_code == 401, r.text
    # 鎖定字句提到「重設」，前半保留
    assert r.json()["error"] == ("這個學號已經設過 PIN，你輸入的不是原本那組，PIN 錯太多次，這一場請找老師手動簽到。"
                                 "忘了 PIN 的話，請老師在名冊頁按「重設」，重設後重新掃 QR 設一組新的。"), r.text


def test_supplement_typo_id_reject_is_not_attributed_to_classmate(h):
    """姓名對不上就不知道是誰——被擋那一筆不能歸在同學名下（tried／卡住名單都不能指向同學）。"""
    s2 = _two_bound(h, "twobound-f")
    pk_b = h.pk("CD", "9994A002")
    for _ in range(2):  # 兩次才夠卡住名單的門檻，沒修之前同學乙會被列進去
        r = _ci10(h, s2, "twobound-f-new", "9994A002", name="王小明", pin="1111", pin2="1111")
        assert r.status_code == 400, r.text
    with h.A.db() as con:
        ev = con.execute("SELECT student_pk FROM events WHERE session_id=? AND kind='checkin_reject' AND reason='not_match'", (s2,)).fetchall()
    assert len(ev) == 2 and all(e["student_pk"] is None for e in ev), [dict(e) for e in ev]
    st = h.state(s2)
    assert h.row(s2, "9994A002")["tried"] == "", h.row(s2, "9994A002")
    assert all(x["pk"] != pk_b for x in st["stuck"]), st["stuck"]


# 本人換環境再掃一次，老師頁不亮紅字

def _stuck_list(h, sid, stu="9994A001"):
    with h.A.db() as con:
        s = h.A.get_session(con, sid)
        return [x for x in h.A.compute_stuck(con, sid, s["opened_at"]) if x["student_id"] == stu]


def _prior_binding(h, tag, stu="9994A001", name="學生1", pin="1234"):
    """上週：環境 A 首綁並簽到、結束；本週開新的一場。回 (本週 sid, A 名, B 名, C 名)。"""
    s1 = h.open("CD")
    assert h.checkin(s1, f"{tag}-A", stu, name=name, pin=pin)["ok"]
    h.T.post(f"/api/t/session/{s1}/close")
    return h.open("CD"), f"{tag}-A", f"{tag}-B", f"{tag}-C"


def test_1_rescan_in_new_environment_after_rebind_is_gray_info(h):
    s2, a, b, c = _prior_binding(h, "prior-a")
    assert _ci10(h, s2, b, "9994A001", pin="1234").json()["rebound"] is True
    r = _ci10(h, s2, c, "9994A001", pin="1234")
    assert r.status_code == 409, r.text
    entry = _stuck_list(h, s2)
    assert len(entry) == 1, entry
    assert entry[0]["conflict"] is False and entry[0]["info"] is True, entry
    assert "本人可能又掃了一次" in entry[0]["reason_label"], entry
    assert entry[0]["reason"] == "already_recorded", entry


def test_2_old_environment_after_rebind_is_red(h):
    s2, a, b, c = _prior_binding(h, "prior-b")
    assert _ci10(h, s2, b, "9994A001", pin="1234").json()["rebound"] is True
    r = _ci10(h, s2, a, "9994A001", pin="1234")  # 機主用原本那支來，發現被改綁走了
    assert r.status_code == 409, r.text
    entry = _stuck_list(h, s2)
    assert len(entry) == 1 and entry[0]["conflict"] is True, entry
    assert "搶先改綁" in entry[0]["reason_label"], entry
    assert not entry[0].get("info"), entry


def test_3_signed_then_correct_pin_from_stranger_environment_is_gray(h):
    s2 = h.open("CD")
    assert h.checkin(s2, "prior-c-A", "9994A001", name="學生1", pin="1234")["ok"]
    r = _ci10(h, s2, "prior-c-C", "9994A001", pin="1234")
    assert r.status_code == 409, r.text
    entry = _stuck_list(h, s2)
    assert len(entry) == 1 and entry[0]["conflict"] is False and entry[0]["info"] is True, entry


def test_4_wrong_pin_from_other_environment_stays_red(h):
    s2 = h.open("CD")
    assert h.checkin(s2, "prior-d-A", "9994A001", name="學生1", pin="1234")["ok"]
    r = _ci10(h, s2, "prior-d-C", "9994A001", pin="9999")
    assert r.status_code == 401, r.text
    entry = _stuck_list(h, s2)
    assert len(entry) == 1 and entry[0]["conflict"] is True and entry[0]["reason"] == "pin_wrong", entry


def test_5_manual_row_never_goes_gray(h):
    """手動列不含 already_recorded，所以根本不進灰群。"""
    s2, a, b, c = _prior_binding(h, "prior-e")
    h.T.post(f"/api/t/session/{s2}/status", data={"pk": h.pk("CD", "9994A001"), "status": "present"})
    assert _ci10(h, s2, b, "9994A001", pin="1234").status_code == 409
    assert _stuck_list(h, s2) == []


def test_6_mixed_red_wins_over_gray(h):
    """同一人既有灰（新環境打對 PIN）又有紅（PIN 打錯）：照紅輸出，不降成灰。"""
    s2 = h.open("CD")
    assert h.checkin(s2, "prior-f-A", "9994A001", name="學生1", pin="1234")["ok"]
    assert _ci10(h, s2, "prior-f-C", "9994A001", pin="1234").status_code == 409
    assert _ci10(h, s2, "prior-f-D", "9994A001", pin="9999").status_code == 401
    entry = _stuck_list(h, s2)
    assert len(entry) == 1 and entry[0]["conflict"] is True and not entry[0].get("info"), entry


def test_7_template_stuck_groups(h):
    text = (TEMPLATES / "session.html").read_text(encoding="utf-8")
    assert '<p class="muted sm" style="margin:10px 0 4px">多半是本人，不用處理：</p>' in text  # R7：標題改成能涵蓋「新瀏覽器又掃一次」與「剛處理過又試」兩種灰字的中性說法
    assert "x.info" in text


# 前幾天被解除綁定的學生，PIN 打對就能綁

def _unbound_yesterday(h, tag, reset=False):
    """甲上週綁定簽到、結束；老師解除綁定（或重設），事件改成昨天；首次綁定關著；開今天的新一場。"""
    s1 = h.open("CD")
    assert h.checkin(s1, f"{tag}-A", "9994A001", name="學生1", pin="1234")["ok"]
    h.T.post(f"/api/t/session/{s1}/close")
    pk = h.pk("CD", "9994A001")
    assert h.T.post(f"/api/t/student/{pk}/unbind", data={"reset_pin": "1" if reset else ""}).status_code == 200
    with h.A.db() as con:
        con.execute("UPDATE events SET ts=? WHERE kind='unbind' AND student_pk=?", (h.A.today_start() - 1, pk))
    h.T.post(f"/api/t/course/{h.cid['CD']}/bind", data={"allow": "0"})
    return h.open("CD")


def _last_reject_reason(h):
    with h.A.db() as con:
        return con.execute("SELECT reason FROM events WHERE kind='checkin_reject' ORDER BY id DESC LIMIT 1").fetchone()["reason"]


def test_1_unbound_earlier_day_correct_pin_binds_new_device(h):
    s2 = _unbound_yesterday(h, "unboundy-a")
    r = _ci10(h, s2, "unboundy-a-new", "9994A001", pin="1234")
    assert r.status_code == 200, r.text
    assert _dev_hash_of(h, "9994A001") == h.A.hash_device(h.dev("unboundy-a-new").cookies.get(h.A.DK_COOKIE))


def test_2_unbound_earlier_day_wrong_pin_is_401_not_403(h):
    s2 = _unbound_yesterday(h, "unboundy-b")
    r = _ci10(h, s2, "unboundy-b-new", "9994A001", pin="9999")
    assert r.status_code == 401, r.text
    assert _last_reject_reason(h) == "pin_wrong"


def test_3_reset_earlier_day_still_bind_closed(h):
    s2 = _unbound_yesterday(h, "unboundy-c", reset=True)
    r = _ci10(h, s2, "unboundy-c-new", "9994A001", name="學生1", pin="1234", pin2="1234")
    assert r.status_code == 403, r.text
    assert _last_reject_reason(h) == "bind_closed"


def test_4_reset_today_still_exempt(h):
    s1 = h.open("CD")
    assert h.checkin(s1, "unboundy-d-A", "9994A001", name="學生1", pin="1234")["ok"]
    h.T.post(f"/api/t/session/{s1}/close")
    assert h.T.post(f"/api/t/student/{h.pk('CD', '9994A001')}/unbind", data={"reset_pin": "1"}).status_code == 200
    h.T.post(f"/api/t/course/{h.cid['CD']}/bind", data={"allow": "0"})
    s2 = h.open("CD")
    r = _ci10(h, s2, "unboundy-d-new", "9994A001", name="學生1", pin="5555", pin2="5555")
    assert r.status_code == 200, r.text


def test_5_revoked_today_wins_over_bind_closed(h):
    """今天綁定場次判不在（PIN 一併清掉）、首次綁定關著：說的是 bind_revoked，不是 bind_closed。"""
    sid = h.open("CD", kind="bind")
    assert h.checkin(sid, "unboundy-e-A", "9994A001", name="學生1", pin="9999")["ok"] is True
    pick = h.T.post(f"/api/t/session/{sid}/spotcheck").json()["picks"][0]
    assert h.T.post(f"/api/t/spotcheck/{pick['id']}", data={"present": "0"}).json()["unbound"] is True
    h.T.post(f"/api/t/session/{sid}/close")
    h.T.post(f"/api/t/course/{h.cid['CD']}/bind", data={"allow": "0"})
    s2 = h.open("CD")
    r = _ci10(h, s2, "unboundy-e-new", "9994A001", name="學生1", pin="1234", pin2="1234")
    assert r.status_code == 403, r.text
    assert _last_reject_reason(h) == "bind_revoked"


# 姓名不符不歸到同學、綁定場次提示、重綁旗標
# 假學號用 999 開頭；每個測試用獨一無二的裝置名稱。

def _add_rows(h, rows):
    with h.A.db() as con:
        for stu, nm in rows:
            con.execute("INSERT INTO students(course_id,student_id,name,cls) VALUES(?,?,?,?)", (h.cid["CD"], stu, nm, ""))


def _reject_pks(h, sid, reason=None):
    q, a = "SELECT reason, student_pk FROM events WHERE session_id=? AND kind='checkin_reject'", [sid]
    if reason:
        q += " AND reason=?"
        a.append(reason)
    with h.A.db() as con:
        return [dict(r) for r in con.execute(q + " ORDER BY id", a)]


def _variant_pair(h, tag, sign_b=True, pin_a="1111", pin_b="2222"):
    """甲（999000001 王測甲）、乙（999000002 王測乙）各自綁定並設 PIN、結束；開新的一場，乙視需要先簽到。回 sid。"""
    _add_rows(h, [("999000001", "王測甲"), ("999000002", "王測乙")])
    s1 = h.open("CD")
    assert h.checkin(s1, f"{tag}-甲", "999000001", name="王測甲", pin=pin_a)["ok"]
    assert h.checkin(s1, f"{tag}-乙", "999000002", name="王測乙", pin=pin_b)["ok"]
    h.T.post(f"/api/t/session/{s1}/close")
    s2 = h.open("CD")
    if sign_b:
        assert h.checkin(s2, f"{tag}-乙", "999000002", name="王測乙", pin=pin_b)["ok"]
    return s2


def test_1_variant_wrong_pin_not_attributed_but_pin_fail_counted(h):
    s2 = _variant_pair(h, "variant-a")
    r = _ci10(h, s2, "variant-a-new", "999000002", name="王測甲", pin="9999", pin2="")
    assert r.status_code == 401, r.text
    assert r.json()["error"] == "PIN 錯誤，剩 2 次。先確認學號沒打錯；忘記了請找老師重設", r.text
    ev = _reject_pks(h, s2, "pin_wrong")
    assert len(ev) == 1 and ev[0]["student_pk"] is None, ev
    assert _stuck_list(h, s2, "999000002") == [], _stuck_list(h, s2, "999000002")
    rows = [x for x in _pin_fail_rows(h, "999000002") if x["device_hash"] != ""]
    assert len(rows) == 1 and rows[0]["n"] == 1, [dict(x) for x in _pin_fail_rows(h, "999000002")]


def test_2_variant_with_matching_pin_becomes_pending_name_variant(h):
    s2 = _variant_pair(h, "variant-b", sign_b=False)
    r = _ci10(h, s2, "variant-b-new", "999000002", name="王測甲", pin="2222", pin2="")
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["status"] == "待確認" and j["pending_reason"] == "name_variant", j
    row = h.db_rows(s2)["999000002"]
    flags = json.loads(row["flags"])
    assert row["status"] == "pending" and "name_variant" in flags and "rebind" in flags, row


def test_3_owner_with_exact_name_still_present(h):
    s2 = _variant_pair(h, "variant-c", sign_b=False)
    r = _ci10(h, s2, "variant-c-new", "999000002", name="王測乙", pin="2222", pin2="")
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "出席", r.text
    assert "name_variant" not in json.loads(h.db_rows(s2)["999000002"]["flags"])


def test_4_claimed_pk_only_exact_name_counts(h):
    s2 = _variant_pair(h, "variant-d", sign_b=False)
    assert h.A._claimed_pk(s2, "999000002", "王測甲") is None
    assert h.A._claimed_pk(s2, "999000002", "王測乙") == h.pk("CD", "999000002")


def test_5_variant_wrong_pins_still_lock_the_device(h):
    s2 = _variant_pair(h, "variant-e", sign_b=False)
    for p in ("9991", "9992", "9993"):
        assert _ci10(h, s2, "variant-e-new", "999000002", name="王測甲", pin=p, pin2="").status_code == 401
    r = _ci10(h, s2, "variant-e-new", "999000002", name="王測甲", pin="2222", pin2="")
    assert r.status_code == 423, r.text


# 姓名對不上，被擋那一筆不歸到那個學號（不論擋在哪一關）

def _mismatch_pair(h):
    _add_rows(h, [("999000001", "王測甲"), ("999000002", "李測乙")])
    return h.open("CD")


def test_1_first_time_name_mismatch_not_attributed(h):
    sid = _mismatch_pair(h)
    for _ in range(2):
        r = _ci10(h, sid, "dev5a-new", "999000002", name="王測甲", pin="1234", pin2="1234")
        assert r.status_code == 400 and "名冊不符" in r.json()["error"], r.text
    ev = _reject_pks(h, sid, "not_match")
    assert len(ev) == 2 and all(e["student_pk"] is None for e in ev), ev
    assert all(x["student_id"] != "999000002" for x in h.state(sid)["stuck"])
    h.T.post(f"/api/t/session/{sid}/close")
    assert h.row(sid, "999000002")["tried"] == "", h.row(sid, "999000002")


def test_2_short_pin_with_wrong_name_says_not_match(h):
    sid = _mismatch_pair(h)
    r = _ci10(h, sid, "dev5b-new", "999000002", name="王測甲", pin="12", pin2="12")
    assert r.status_code == 400 and "名冊不符" in r.json()["error"], r.text
    assert all(e["student_pk"] is None for e in _reject_pks(h, sid))


def test_3_bind_closed_course_says_not_match_not_403(h):
    sid = _mismatch_pair(h)
    h.T.post(f"/api/t/course/{h.cid['CD']}/bind", data={"allow": "0"})
    r = _ci10(h, sid, "dev5c-new", "999000002", name="王測甲", pin="1234", pin2="1234")
    assert r.status_code == 400 and "名冊不符" in r.json()["error"], r.text
    assert all(e["student_pk"] is None for e in _reject_pks(h, sid))


def test_4_revoked_today_says_not_match_for_wrong_name(h):
    _add_rows(h, [("999000001", "王測甲"), ("999000002", "李測乙")])
    bs = h.open("CD", kind="bind")
    assert h.checkin(bs, "dev5d-乙", "999000002", name="李測乙", pin="2222")["ok"]
    pick = h.T.post(f"/api/t/session/{bs}/spotcheck").json()["picks"][0]
    assert h.T.post(f"/api/t/spotcheck/{pick['id']}", data={"present": "0"}).json()["unbound"] is True
    r = _ci10(h, bs, "dev5d-new", "999000002", name="王測甲", pin="1234", pin2="1234")
    assert r.status_code == 400 and "名冊不符" in r.json()["error"], r.text
    assert all(e["student_pk"] is None for e in _reject_pks(h, bs, "not_match"))


def test_5_pin_only_mode_empty_name_unchanged(h):
    sid = _mismatch_pair(h)
    r = _ci10(h, sid, "dev5e-new", "999000002", name="", pin="1234", pin2="")
    assert r.status_code == 400 and "名冊不符" in r.json()["error"], r.text  # 空姓名在首次分支照舊回名冊不符


def test_6_bound_device_one_tap_ignores_name(h):
    _add_rows(h, [("999000002", "李測乙")])
    s1 = h.open("CD")
    assert h.checkin(s1, "dev5f-乙", "999000002", name="李測乙", pin="2222")["ok"]
    h.T.post(f"/api/t/session/{s1}/close")
    s2 = h.open("CD")
    r = _ci10(h, s2, "dev5f-乙", "999000002", name="亂打的名字", pin="", pin2="")
    assert r.status_code == 200, r.text


# 綁定場次的 409 與已簽到橫幅

def _bind_session_setup(h, tag, kind):
    _add_rows(h, [("999000001", "王測甲")])
    s0 = h.open("CD")
    assert h.checkin(s0, f"{tag}-A", "999000001", name="王測甲", pin="1234")["ok"]
    h.T.post(f"/api/t/session/{s0}/close")
    s2 = h.open("CD", kind=kind)
    assert _ci10(h, s2, f"{tag}-B", "999000001", pin="1234").json()["rebound"] is True
    return s2


def _land_html(h, sid, dev):
    code = h.T.get(f"/api/t/session/{sid}/qr").json()["code"]
    return h.dev(dev).post("/c", data={"code": code}).text


def test_1_bind_session_409_and_banner(h):
    s2 = _bind_session_setup(h, "bindsess-a", "bind")
    r = _ci10(h, s2, "bindsess-a-C", "999000001", pin="1234")
    assert r.status_code == 409, r.text
    err = r.json()["error"]
    assert "只做綁定、不算出缺勤" in err and "（出席）" not in err, err
    assert err == ("這個學號這一場已經綁定過了（另一個瀏覽器或老師記的）。這一場只做綁定、不算出缺勤；今天還沒簽到成功的，請找老師補登。"
                   "不是你本人、或紀錄不對，現在就舉手找老師。")
    html = _land_html(h, s2, "bindsess-a-B")
    assert "這一場已經綁定了" in html and "請找老師補登" in html and "出席（" not in html and "這一場已經簽到了" not in html, html[-2500:]


def test_2_normal_session_texts_unchanged(h):
    s2 = _bind_session_setup(h, "bindsess-b", "normal")
    r = _ci10(h, s2, "bindsess-b-C", "999000001", pin="1234")
    assert r.status_code == 409 and "已經有紀錄（出席）" in r.json()["error"], r.text
    html = _land_html(h, s2, "bindsess-b-B")
    assert "這一場已經簽到了" in html and "這一場已經綁定了" not in html


# 解除綁定後靠 PIN 綁上要留旗標

def test_unbound_then_pin_bind_leaves_rebind_flag(h):
    _add_rows(h, [("999000001", "王測甲")])
    s1 = h.open("CD")
    assert h.checkin(s1, "dev6-A", "999000001", name="王測甲", pin="1234")["ok"]
    assert "rebind" not in json.loads(h.db_rows(s1)["999000001"]["flags"]), "首次綁定（沒設過 PIN）不加 rebind"
    h.T.post(f"/api/t/session/{s1}/close")
    assert h.T.post(f"/api/t/student/{h.pk('CD', '999000001')}/unbind", data={"reset_pin": ""}).status_code == 200
    s2 = h.open("CD")
    r = _ci10(h, s2, "dev6-B", "999000001", pin="1234")
    assert r.status_code == 200 and r.json().get("rebound") is True, r.text
    assert "rebind" in json.loads(h.db_rows(s2)["999000001"]["flags"])
    with h.A.db() as con:
        ev = con.execute("SELECT reason FROM events WHERE session_id=? AND kind='rebind'", (s2,)).fetchall()
    assert [e["reason"] for e in ev] == [""], [dict(e) for e in ev]
    assert _ci10(h, s2, "dev6-C", "999000001", pin="1234").status_code == 409
    entry = _stuck_list(h, s2, "999000001")
    assert len(entry) == 1 and entry[0]["conflict"] is False and entry[0]["info"] is True, entry


# 只輸 PIN 之後同姓同字數的打錯仍歸到同學
# 前端切成只輸 PIN 後 name 是空字串，另帶 name_hint（切換前表單上打的姓名）。

def _pin_only_post(h, sid, dev, stu, pin, name="", name_hint=None, pin2="", grant=None):
    cl = h.dev(dev)
    data = {"grant": grant or h.grant(sid, cl), "student_id": stu, "name": name, "pin": pin, "pin2": pin2, "hints": "h"}
    if name_hint is not None:
        data["name_hint"] = name_hint
    return cl.post("/api/checkin", data=data, headers={"user-agent": "iPhone", "cf-connecting-ip": "140.131.1.1"})


def test_1_pin_only_with_variant_hint_not_attributed(h):
    s2 = _variant_pair(h, "pinonly-a")
    r1 = _ci10(h, s2, "pinonly-a-new", "999000002", name="王測甲", pin="9999", pin2="9999")
    assert r1.status_code == 401 and "已經設過 PIN" in r1.json()["error"], r1.text
    r2 = _pin_only_post(h, s2, "pinonly-a-new", "999000002", "9998", name="", name_hint="王測甲")  # 換一組錯 PIN：同一組 60 秒內重送不重複計
    assert r2.status_code == 401 and r2.json()["error"].startswith("PIN 錯誤，剩 1 次"), r2.text
    ev = _reject_pks(h, s2)
    assert len(ev) == 2 and all(e["student_pk"] is None for e in ev), ev
    assert _stuck_list(h, s2, "999000002") == [], _stuck_list(h, s2, "999000002")
    rows = [x for x in _pin_fail_rows(h, "999000002") if x["device_hash"] != ""]
    assert len(rows) == 1 and rows[0]["n"] == 2, [dict(x) for x in _pin_fail_rows(h, "999000002")]


def test_2_pin_only_variant_hint_matching_pin_becomes_pending(h):
    s2 = _variant_pair(h, "pinonly-b", sign_b=False)
    r1 = _ci10(h, s2, "pinonly-b-new", "999000002", name="王測甲", pin="9999", pin2="9999")
    assert r1.status_code == 401, r1.text
    r2 = _pin_only_post(h, s2, "pinonly-b-new", "999000002", "2222", name="", name_hint="王測甲")
    assert r2.status_code == 200, r2.text
    j = r2.json()
    assert j["status"] == "待確認" and j["pending_reason"] == "name_variant", j
    flags = json.loads(h.db_rows(s2)["999000002"]["flags"])
    assert "rebind" in flags and "name_variant" in flags, flags


def test_3_pin_only_with_exact_hint_is_present(h):
    s2 = _variant_pair(h, "pinonly-c", sign_b=False)
    r = _pin_only_post(h, s2, "pinonly-c-new", "999000002", "2222", name="", name_hint="王測乙")
    assert r.status_code == 200 and r.json()["status"] == "出席", r.text
    assert "name_variant" not in json.loads(h.db_rows(s2)["999000002"]["flags"])


def test_4_LIMITATION_pin_only_without_hint_old_frontend_still_attributed(h):
    """記錄限制，不是期望的好行為：舊前端（沒帶 name_hint）只輸 PIN 被擋，事件仍歸到乙。"""
    s2 = _variant_pair(h, "pinonly-d")
    r = _pin_only_post(h, s2, "pinonly-d-new", "999000002", "9999", name="")
    assert r.status_code == 401, r.text
    ev = _reject_pks(h, s2, "pin_wrong")
    assert len(ev) == 1 and ev[0]["student_pk"] == h.pk("CD", "999000002"), ev


def test_5_pin_only_hint_different_surname_not_attributed(h):
    s2 = _variant_pair(h, "pinonly-e")
    r = _pin_only_post(h, s2, "pinonly-e-new", "999000002", "9999", name="", name_hint="李四方")
    assert r.status_code == 401, r.text
    ev = _reject_pks(h, s2, "pin_wrong")
    assert len(ev) == 1 and ev[0]["student_pk"] is None, ev


def test_6_rejected_before_roster_lookup_uses_hint_for_attribution(h):
    s2 = _variant_pair(h, "pinonly-f")
    cl = h.dev("pinonly-f-new")
    g = h.grant(s2, cl)
    h.T.post(f"/api/t/session/{s2}/close")
    r1 = _pin_only_post(h, s2, "pinonly-f-new", "999000002", "9999", name="", name_hint="王測甲", grant=g)
    assert r1.status_code == 410, r1.text
    r2 = _pin_only_post(h, s2, "pinonly-f-new", "999000002", "9999", name="", name_hint="王測乙", grant=g)
    assert r2.status_code == 410, r2.text
    ev = _reject_pks(h, s2, "closed")
    assert [e["student_pk"] for e in ev] == [None, h.pk("CD", "999000002")], ev


def test_7_first_time_branch_ignores_hint_unchanged(h):
    s2 = _variant_pair(h, "pinonly-g", sign_b=False)
    with h.A.db() as con:
        con.execute("UPDATE students SET pin_hash=NULL WHERE id=?", (h.pk("CD", "999000002"),))
    r = _pin_only_post(h, s2, "pinonly-g-new", "999000002", "1234", name="", name_hint="王測乙")
    assert r.status_code == 400 and "名冊不符" in r.json()["error"], r.text


def test_8_pin_only_with_hint_still_locks_after_three_wrong(h):
    s2 = _variant_pair(h, "pinonly-h", sign_b=False)
    for p in ("9991", "9992", "9993"):
        assert _pin_only_post(h, s2, "pinonly-h-new", "999000002", p, name="", name_hint="王測甲").status_code == 401
    r = _pin_only_post(h, s2, "pinonly-h-new", "999000002", "2222", name="", name_hint="王測甲")
    assert r.status_code == 423, r.text
