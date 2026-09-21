"""9/10 實地摩擦的回歸測試（BUGLOG B012/B015/B016/B018/B019/B021/B022/B037）。
每條指回 docs/FIELD_2026-09-10.md 或 BUGLOG.tsv 對應的一句話根因。"""
import json
import re
import threading
from pathlib import Path

import time_machine

TEMPLATES = Path(__file__).parent.parent / "templates"


# ───────────────────────── B012：favicon ─────────────────────────

def test_favicon_no_longer_404s(client):
    r = client.get("/favicon.ico")
    assert r.status_code != 404
    assert r.status_code == 204
    assert "max-age" in r.headers.get("cache-control", "")


# ───────────────────────── B015：qr_grace 可以大於 qr_interval ─────────────────────────

def test_current_slots_grace_exceeds_interval_boundary(A):
    """interval=10、grace=30：片結束後 29.9 秒仍有效、30.1 秒失效（BUGLOG B015 的具體邊界）。"""
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
    assert st["qr_grace"] == 120, "B015：上限是 120，不是被 qr_interval（5）夾住"


def test_save_settings_grace_not_clamped_to_interval(h):
    cid = h.cid["CD"]
    h.T.post(f"/t/course/{cid}/settings", data={
        "open_minutes": 3, "late_minutes": 15, "qr_interval": 10, "qr_grace": 60,
        "spot_n": 3, "ip_check": "", "discord_channel": ""})
    with h.A.db() as con:
        st = h.A.course_settings(h.A.get_course(con, cid))
    assert st["qr_grace"] == 60, "grace（60）大於 interval（10）應該原樣保留，不是被夾到 10"


# ───────────────────────── A1（SPEC 2026-09-21）：QR 時效預設 10／30 ─────────────────────────

def test_default_qr_grace_is_30_seconds(A):
    """B015/B007/B040 綜合裁定：qr_grace 預設 5→30，qr_interval 維持 10。"""
    assert A.DEFAULT_SETTINGS["qr_grace"] == 30
    assert A.DEFAULT_SETTINGS["qr_interval"] == 10


def test_no_context_pages_dont_hardcode_ten_seconds():
    """A1：code.html/help.html/home.html 沒有課程脈絡（qr_grace 可能被個別課程改掉），
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
    """help.html:10 防轉傳那句保留原意：QR 會換、表單只能在開它的那支手機送出、老師會抽點。"""
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


# ───────────────────────── B016：换瀏覽器被誤導成首次綁定 ─────────────────────────

def test_wrong_pin_with_pin2_always_explains_existing_pin(h):
    """非首次、帶了 pin2、PIN 錯 → 一律「已經設過 PIN」，不看是否已綁裝置（舊行為只在 device_hash 為空時給）。
    A2：已綁裝置回簽免 PIN，這裡改用另一支裝置模擬換瀏覽器（不同 cookie 罐）才吃得到 PIN 驗證。"""
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
    """對照組：沒有帶 pin2（換手機情境）PIN 錯，訊息應是正常的「剩 N 次」，不是 B016 那句。
    A2：已綁裝置回簽免 PIN，這裡改用另一支裝置才吃得到 PIN 驗證。"""
    sid = h.open("CD")
    h.checkin(sid, "devB", "9994A002", name="學生2", pin="2222")
    r = h.dev("devB2").post("/api/checkin", data={
        "grant": h.grant(sid, h.dev("devB2")), "student_id": "9994A002", "pin": "0000"})
    assert r.status_code == 401
    assert "剩" in r.json()["error"] and "已經設過 PIN" not in r.json()["error"]


def test_C_S9_pin_wrong_message_has_exit(h):
    """C-S9（SPEC 2026-09-21）：pin_wrong 補出路「忘記了請找老師重設」；鎖定訊息不變。"""
    sid = h.open("CD")
    h.checkin(sid, "cs9-owner", "9994A002", name="學生2", pin="2222")
    r = h.dev("cs9-wrong").post("/api/checkin", data={
        "grant": h.grant(sid, h.dev("cs9-wrong")), "student_id": "9994A002", "pin": "0000"})
    assert r.status_code == 401
    assert r.json()["error"] == "PIN 錯誤，剩 2 次。忘記了請找老師重設"


# ───────────────────────── B018：姓名 NFKC 正規化 ─────────────────────────

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


# ───────────────────────── B019：學號全形英數轉半形 ─────────────────────────

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


# ───────────────────────── B021：錯誤頁也下發裝置 cookie；沒 cookie 的限流放寬到 IP 同級 ─────────────────────────

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
    """同 IP 同 UA 的 30 個沒 cookie 的 client 依序打 /c 錯碼，不得有人 429（BUGLOG B021 實測：
    第二批 30 人同款手機同出口 IP 全 429）。要有進行中的點名，錯碼才會真的落到節流計數（否則
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


# ───────────────────────── B022：首次綁定被裝置衝突擋下時不寫 PIN、不綁裝置 ─────────────────────────

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
    assert b["pin_hash"] is None, "B022：手機是別人的，不該側寫 PIN"
    assert b["device_hash"] is None, "B022：手機是別人的，不該綁裝置"


# ───────────────────────── A3（SPEC 2026-09-21）：遲到改看「掃到」時間，B029 ─────────────────────────

def test_A3_scanned_in_open_window_submitted_after_it_closes_is_present_no_was_late(h):
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


def test_A3_scanned_after_open_window_is_late(h):
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


def test_A3_past_late_until_still_closed_regardless_of_scan_time(h):
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
    assert "點名已結束" in r.json()["error"]


def test_A3_old_three_part_grant_is_invalid(h):
    """三段式舊憑證（沒有發證時間那一段）不做相容，視為無效，回「填表時間已到，請重新掃描 QR」。"""
    sid = h.open("CD")
    A = h.A
    old_grant = A.make_token("g", str(sid), A.secrets.token_hex(6), exp=A.now() + 180)
    phone = h.dev("a3-old-grant")
    r = phone.post("/api/checkin", data={"grant": old_grant, "student_id": "9994A001", "name": "學生1",
                                         "pin": "1234", "pin2": "1234", "seat": "", "hints": "h"})
    assert r.status_code == 410
    assert "填表時間已到，請重新掃描 QR" in r.json()["error"]


# ───────────────────────── A4（SPEC 2026-09-21）：姓名寬鬆比對，B018 ─────────────────────────

def _add_student(h, course, stu, name):
    with h.A.db() as con:
        con.execute("INSERT INTO students(course_id, student_id, name, cls) VALUES(?,?,?,?)",
                    (h.cid[course], stu, name, ""))


def test_A4_exact_match_passes_no_flag(h):
    sid = h.open("CD")
    j = h.checkin(sid, "a4-exact", "9994A001", name="學生1", pin="1234")
    assert j["ok"] is True, j
    row = h.row(sid, "9994A001")
    assert "name_variant" not in row["flags"], row["flags"]


def test_A4_variant_character_not_at_first_position_passes_with_flag(h):
    """溫/温 這類異體字打法（差異不在第一個字）：字數相同、第一個字相同 → 通過並帶 name_variant 旗標。"""
    _add_student(h, "CD", "A4S01", "林溫蒂")
    sid = h.open("CD")
    j = h.checkin(sid, "a4-variant", "A4S01", name="林温蒂", pin="1234")
    assert j["ok"] is True, j
    row = h.row(sid, "A4S01")
    assert "name_variant" in row["flags"], row["flags"]


def test_A4_same_surname_same_length_different_name_passes_with_flag_known_tradeoff(h):
    """同姓同字數的不同名字也會通過並帶旗標——這是刻意放寬的已知代價，不是漏洞。"""
    _add_student(h, "CD", "A4S02", "王小明")
    sid = h.open("CD")
    j = h.checkin(sid, "a4-samesurname", "A4S02", name="王小華", pin="1234")
    assert j["ok"] is True, j
    row = h.row(sid, "A4S02")
    assert "name_variant" in row["flags"], row["flags"]


def test_A4_different_length_is_rejected(h):
    _add_student(h, "CD", "A4S03", "王小明")
    sid = h.open("CD")
    j = h.checkin(sid, "a4-lendiff", "A4S03", name="王小明華", pin="1234")
    assert j["ok"] is False and "學號或姓名" in j["error"], j
    assert not h.row(sid, "A4S03")["status"]


def test_A4_different_first_char_is_rejected(h):
    _add_student(h, "CD", "A4S04", "王小明")
    sid = h.open("CD")
    j = h.checkin(sid, "a4-firstdiff", "A4S04", name="李小明", pin="1234")
    assert j["ok"] is False and "學號或姓名" in j["error"], j
    assert not h.row(sid, "A4S04")["status"]


def test_A4_existing_row_from_resubmit_not_retroactively_flagged(h):
    """已存在的列（already 早退路徑）不補這個旗標：正常首簽（exact）之後再次送出，即使這次帶了
    一個「字數相同、第一個字相同」的不同名字，既有列也不會因此被補上 name_variant（因為 already
    早退不會重新跑姓名比對）。"""
    sid = h.open("CD")
    j1 = h.checkin(sid, "a4-again", "9994A001", name="學生1", pin="1234")
    assert j1["ok"] is True and "name_variant" not in h.row(sid, "9994A001")["flags"]
    j2 = h.checkin(sid, "a4-again", "9994A001", name="學生九", pin="1234")  # 已綁定同手機，回原收據
    assert j2["ok"] is True and j2.get("again") is True
    assert "name_variant" not in h.row(sid, "9994A001")["flags"]


# ───────────────────────── A5（SPEC 2026-09-21）：過期頁與錯誤頁加「找老師」，B006 ─────────────────────────

FIND_TEACHER_TEXT = "手機掃不過，不要借同學的手機，直接舉手找老師。"


def test_A5_find_teacher_text_present_in_code_and_student_templates():
    code_text = (TEMPLATES / "code.html").read_text(encoding="utf-8")
    student_text = (TEMPLATES / "student.html").read_text(encoding="utf-8")
    assert FIND_TEACHER_TEXT in code_text, "code.html 說明段缺「找老師」文案"
    assert student_text.count(FIND_TEACHER_TEXT) >= 2, (
        "student.html 應該在過期頁與表單下方小字都出現「找老師」文案")


def test_A5_expired_page_shows_find_teacher_text(h):
    sid = h.open("CD")
    bad_sig = "0" * 10
    r = h.dev("a5-expired").get(f"/s/{sid}/0/{bad_sig}")
    assert r.status_code == 410
    assert FIND_TEACHER_TEXT in r.text


# ───────────────────────── A2（SPEC 2026-09-21）：已綁裝置回簽免 PIN，題三 A ─────────────────────────

def test_A2_bound_device_checkin_without_pin_succeeds(h):
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


def test_A2_bound_device_wrong_pin_still_succeeds_and_no_fail_count(h):
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


def test_A2_other_device_without_pin_still_rejected(h):
    sid0 = h.open("CD")
    h.checkin(sid0, "a2-owner", "9994A003", name="學生3", pin="1234")
    h.T.post(f"/api/t/session/{sid0}/close")
    sid = h.open("CD")
    dev = h.dev("a2-stranger")
    r = dev.post("/api/checkin", data={
        "grant": h.grant(sid, dev), "student_id": "9994A003", "name": "學生3", "pin": "", "seat": "", "hints": "h"})
    assert r.status_code == 400
    assert "PIN 要 4 到 6 位數字" in r.json()["error"]


def test_A2_unbound_after_teacher_unbind_requires_old_pin(h):
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


def test_A2_bound_device_not_locked_out_by_others_wrong_pins(h):
    """B044：平日免 PIN 之後，PIN 鎖定不該擋到本人已綁的那支手機。
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


# ───────────────────────── C-S1（SPEC 2026-09-21）：姓名寫法不同的首綁一律轉待確認 ─────────────────────────


def test_C_S1_variant_first_bind_is_pending_with_both_flags(h):
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


def test_C_S1_exact_first_bind_still_present(h):
    """exact 首綁行為不變：沒有 name_variant，狀態照舊是出席（開放窗內）。"""
    sid = h.open("CD")
    j = h.checkin(sid, "cs1-exact", "9994A001", name="學生1", pin="1234")
    assert j["ok"] is True, j
    row = h.row(sid, "9994A001")
    assert row["status"] == "present", row
    assert "name_variant" not in row["flags"], row["flags"]


def test_C_S1_variant_spotcheck_present_restores_was_late(h):
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


# ───────────────────────── C-S4（SPEC 2026-09-21）：簽到的讀後寫放進同一個寫入交易 ─────────────────────────

def test_C_S4_race_same_device_two_students_first_bind_only_one_wins(h):
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


# ───────────────────────── C-S5（SPEC 2026-09-21）：簽到憑證在下發時就綁裝置 ─────────────────────────

def test_C_S5_relayed_grant_rejected_on_first_use_by_other_device(h):
    """教室內同學把 GRANT 字串轉給場外的人，場外那支手機第一次使用就該被拒，且不能佔走 grants 表
    （被拒之後，真正掃到的那支手機自己送出仍應成功）。"""
    sid = h.open("CD")
    devA = h.dev("s5-a")
    devB = h.dev("s5-b")
    grant = h.grant(sid, devA)
    r = devB.post("/api/checkin", data={"grant": grant, "student_id": "9994A001", "name": "學生1",
                                         "pin": "1234", "pin2": "1234", "hints": "h"})
    assert r.status_code == 410, r.text
    assert r.json()["error"] == "這份簽到表單是別支手機掃到的，請自己重新掃描 QR"
    with h.A.db() as con:
        gsig = grant.rsplit(":", 1)[-1]
        row = con.execute("SELECT device_hash FROM grants WHERE sig=?", (gsig,)).fetchone()
    assert row is None, "被拒的別支裝置不該佔走 grants 表"
    r2 = devA.post("/api/checkin", data={"grant": grant, "student_id": "9994A001", "name": "學生1",
                                          "pin": "1234", "pin2": "1234", "hints": "h"})
    assert r2.status_code == 200, r2.text


def test_C_S5_first_load_without_cookie_cookie_and_grant_match(h):
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


def test_C_S5_four_part_legacy_grant_is_invalid(h):
    sid = h.open("CD")
    dev = h.dev("s5-legacy")
    old_grant = h.A.make_token("g", str(sid), "abcdef123456", str(h.A.now()), exp=h.A.now() + 180)
    r = dev.post("/api/checkin", data={"grant": old_grant, "student_id": "9994A001", "name": "學生1",
                                        "pin": "1234", "pin2": "1234", "hints": "h"})
    assert r.status_code == 410, r.text


# ───────────────────────── C-S3（SPEC 2026-09-21）：出口 IP 限流桶不擋已綁裝置 ─────────────────────────

def test_C_S3_bound_device_exempt_from_ip_throttle(h):
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


# ───────────────────────── C-S7（SPEC 2026-09-21）：裝置 cookie 改用 __Host- 前綴 ─────────────────────────

class _FakeReq:
    """C-S7 規格建議的函式層測試：只需要 .cookies，不必真的發 HTTP 請求。"""
    def __init__(self, cookies):
        self.cookies = cookies


def test_C_S7_http_mode_keeps_legacy_cookie_name(A):
    """本機與測試走 http，瀏覽器不收沒有 Secure 的 __Host- cookie，http 下維持舊名。"""
    assert A.DK_COOKIE == "attend_dk"
    assert A.BASE_URL.startswith("http://")


def test_C_S7_https_mode_new_name_preferred_over_legacy(A, monkeypatch):
    monkeypatch.setattr(A, "DK_COOKIE", "__Host-attend_dk")
    req = _FakeReq({"__Host-attend_dk": "newname000000000000000000", "attend_dk": "oldname000000000000000000"})
    assert A.effective_device_key(req) == "newname000000000000000000"


def test_C_S7_https_mode_legacy_adopted_only_if_bound(A, monkeypatch):
    monkeypatch.setattr(A, "DK_COOKIE", "__Host-attend_dk")
    legacy = "boundoldname0000000000000"
    with A.db() as con:
        con.execute("INSERT INTO courses(code,name) VALUES('CD','脈絡設計')")
        cid = con.execute("SELECT id FROM courses").fetchone()["id"]
        con.execute("INSERT INTO students(course_id,student_id,name,device_hash) VALUES(?,?,?,?)",
                    (cid, "9994A001", "學生1", A.hash_device(legacy)))
    A_req = _FakeReq({"attend_dk": legacy})
    assert A.effective_device_key(A_req) == legacy, "9/10 已綁的舊名裝置不必重綁"


def test_C_S7_https_mode_legacy_adopted_with_explicit_con(A, monkeypatch):
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


def test_C_S7_https_mode_malformed_legacy_cookie_ignored(A, monkeypatch):
    """格式不合法（太短）的舊名 cookie 一律當沒有 cookie，不查資料庫。"""
    monkeypatch.setattr(A, "DK_COOKIE", "__Host-attend_dk")
    A_req = _FakeReq({"attend_dk": "too-short"})
    assert A.effective_device_key(A_req) == ""


def test_C_S7_https_mode_legacy_ignored_if_unbound(A, monkeypatch):
    monkeypatch.setattr(A, "DK_COOKIE", "__Host-attend_dk")
    req = _FakeReq({"attend_dk": "unboundoldname0000000000000"})
    assert A.effective_device_key(req) == "", "未綁的舊名一律忽略、當作沒有 cookie"


def test_C_S7_student_entry_calls_effective_device_key_once(h, monkeypatch):
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
