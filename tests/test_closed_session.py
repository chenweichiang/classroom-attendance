"""已結束場次的畫面與回應：舊網址的說明、老師手動記出席的人結案後看得到收據，
以及換了瀏覽器環境時出席時間怎麼算的現況量測。素材自建：課程 XB，學號 9994A 開頭，姓名「測試N」。"""
import pytest
import time_machine

from conftest import Helpers

T0 = 1_790_000_000.0
BAD_URL = "/s/{sid}/999999/deadbeef00"  # 簽章與時段都不對＝簽章已過期的舊網址
EXPIRED_TEXT = "這個 QR 已經過期，請重新掃描投影幕上的 QR"
LATER_TEXT = "這是之前那一場的 QR，老師已經開了新的一場，請重新掃描投影幕上的 QR"


@pytest.fixture()
def hc(A, teacher):
    with A.db() as con:
        con.execute("INSERT INTO courses(code,name) VALUES('XB','結束場次測試')")
        cid = con.execute("SELECT id FROM courses WHERE code='XB'").fetchone()["id"]
        for i in range(1, 5):
            con.execute("INSERT INTO students(course_id,student_id,name,cls) VALUES(?,?,?,?)", (cid, f"9994A00{i}", f"測試{i}", "測試班"))
    return Helpers(A, teacher, {"X": cid})


def land_events(h, sid):
    with h.A.db() as con:
        return [r["kind"] for r in con.execute("SELECT kind FROM events WHERE session_id=? AND kind LIKE 'land_%' ORDER BY id", (sid,))]


# 已結束場次的舊網址

def test_latest_closed_session_old_url_shows_closed_screen_not_qr_expired(hc):
    h = hc
    sid = h.open("X")
    h.T.post(f"/api/t/session/{sid}/close")
    r = h.student().get(BAD_URL.format(sid=sid), headers={"user-agent": "iPhone"})
    assert "點名已結束" in r.text and "有到課但沒簽到成功的，直接找老師" in r.text, r.text
    assert EXPIRED_TEXT not in r.text and "QR 已過期" not in r.text
    assert land_events(h, sid) == ["land_closed"]


def test_newer_session_live_normal_old_url_says_teacher_opened_a_new_one(hc):
    h = hc
    s1 = h.open("X")
    h.T.post(f"/api/t/session/{s1}/close")
    h.open("X")
    r = h.student().get(BAD_URL.format(sid=s1), headers={"user-agent": "iPhone"})
    assert r.status_code == 410
    assert LATER_TEXT in r.text, r.text
    assert land_events(h, s1) == ["land_closed"]


def test_newer_session_live_bind_old_url_uses_bind_only_message(hc):
    h = hc
    s1 = h.open("X")
    h.T.post(f"/api/t/session/{s1}/close")
    h.open("X", kind="bind")
    r = h.student().get(BAD_URL.format(sid=s1), headers={"user-agent": "iPhone"})
    assert r.status_code == 410
    assert "老師現在開的是只做綁定的場次" in r.text, r.text
    assert land_events(h, s1) == ["land_closed"]


def test_newer_session_already_closed_old_url_says_qr_expired(hc):
    h = hc
    s1 = h.open("X")
    h.T.post(f"/api/t/session/{s1}/close")
    s2 = h.open("X")
    h.T.post(f"/api/t/session/{s2}/close")
    r = h.student().get(BAD_URL.format(sid=s1), headers={"user-agent": "iPhone"})
    assert r.status_code == 410
    assert EXPIRED_TEXT in r.text, r.text
    assert land_events(h, s1) == ["land_closed"]


def test_session_still_live_with_expired_signature_keeps_qr_expired_page(hc):
    h = hc
    sid = h.open("X")
    r = h.student().get(BAD_URL.format(sid=sid), headers={"user-agent": "iPhone"})
    assert r.status_code == 410
    assert EXPIRED_TEXT in r.text and "QR 已過期" in r.text
    assert land_events(h, sid) == ["land_expired"]


# 老師手動記出席的人，結案後看得到自己的紀錄

def _manual_present_after_bound(h, stu="9994A001", name="測試1", status="present"):
    """stu 在上一場把手機（'own'）綁好；這一場老師手動記 status（簽到列 device_hash 是空的），學生這支手機先拿好表單憑證。回傳 (sid, grant)。"""
    s0 = h.open("X")
    assert h.checkin(s0, "own", stu, name=name, pin="1234")["ok"]
    h.T.post(f"/api/t/session/{s0}/close")
    sid = h.open("X")
    assert h.T.post(f"/api/t/session/{sid}/status", data={"pk": h.pk("X", stu), "status": status}).json()["ok"]
    grant = h.grant(sid, h.dev("own"))
    h.borrower_grant = h.grant(sid, h.dev("borrower"))  # 別支手機在結案前拿好的憑證
    h.T.post(f"/api/t/session/{sid}/close")
    return sid, grant


def _valid_signature_url(h, sid):
    A = h.A
    with A.db() as con:
        s = A.get_session(con, sid)
        st = A.course_settings(A.get_course(con, s["course_id"]))
        slot = max(A.current_slots(s, st))
        return f"/s/{sid}/{slot}/{A.slot_sig(s, slot)}"


def test_manual_present_student_with_bound_phone_sees_receipt_on_old_url(hc):
    h = hc
    sid, _ = _manual_present_after_bound(h)
    r = h.dev("own").get(BAD_URL.format(sid=sid), headers={"user-agent": "iPhone"})
    assert r.status_code == 200 and "這一場已經簽到了" in r.text and "測試1" in r.text, (r.status_code, r.text)


def test_manual_present_student_with_bound_phone_sees_receipt_on_valid_url(hc):
    h = hc
    sid, _ = _manual_present_after_bound(h)
    r = h.dev("own").get(_valid_signature_url(h, sid), headers={"user-agent": "iPhone"})
    assert r.status_code == 200 and "這一場已經簽到了" in r.text and "測試1" in r.text, (r.status_code, r.text)


def test_manual_present_student_posting_after_close_gets_receipt_and_nothing_is_written(hc):
    h = hc
    sid, grant = _manual_present_after_bound(h)
    before = h.db_rows(sid)
    j = h.checkin(sid, "own", "9994A001", pin="", pin2="", grant=grant)
    assert j["ok"] is True and j["again"] is True and j["name"] == "測試1" and j["status"] == "出席", j
    assert "grant" not in j
    assert h.db_rows(sid) == before, "不寫任何列、不改任何欄位"


def test_same_receipt_when_grant_signature_valid_but_expired(hc):
    h = hc
    with time_machine.travel(T0, tick=False) as t:
        sid, grant = _manual_present_after_bound(h)
        t.shift(3600)
        j = h.checkin(sid, "own", "9994A001", pin="", pin2="", grant=grant)
    assert j["ok"] is True and j["again"] is True and j["status"] == "出席", j


def test_phone_bound_to_someone_else_still_sees_closed_message(hc):
    h = hc
    sid, _ = _manual_present_after_bound(h)
    r = h.dev("borrower").get(BAD_URL.format(sid=sid), headers={"user-agent": "iPhone"})
    assert "這一場已經簽到了" not in r.text
    j = h.checkin(sid, "borrower", "9994A001", pin="", pin2="", grant=h.borrower_grant)
    assert j["ok"] is False and "點名已經結束" in j["error"], j


def test_student_marked_absent_by_teacher_does_not_get_receipt(hc):
    h = hc
    sid, grant = _manual_present_after_bound(h, status="absent")
    r = h.dev("own").get(BAD_URL.format(sid=sid), headers={"user-agent": "iPhone"})
    assert "這一場已經簽到了" not in r.text
    j = h.checkin(sid, "own", "9994A001", pin="", pin2="", grant=grant)
    assert j["ok"] is False and "點名已經結束" in j["error"], j


# 只量不修（記錄現況，不改程式）
# 開放 3 分鐘、遲到 15 分鐘（課程預設值，明寫進設定讓測試不靠預設）。時間用 time_machine 凍結後 shift。

def _set_windows(h):
    import json
    with h.A.db() as con:
        con.execute("UPDATE courses SET settings=? WHERE id=?",
                    (json.dumps({"open_minutes": 3, "late_minutes": 15}), h.cid["X"]))


def test_measure_pin_locked_new_env_teacher_reset_then_same_env_sets_new_pin(hc):
    h = hc
    _set_windows(h)
    with time_machine.travel(T0, tick=False) as t:
        s0 = h.open("X")
        assert h.checkin(s0, "owner", "9994A001", name="測試1", pin="1234")["ok"]
        h.T.post(f"/api/t/session/{s0}/close")
        t.shift(1000)
        sid = h.open("X")  # O＝開場時刻
        t.shift(60)  # 1:00：新環境（沒有 cookie）落地
        newenv = h.dev("newenv")
        g = h.grant(sid, newenv)
        locked = []
        for i, pin in enumerate(("1111", "2222", "3333", "4444")):
            t.shift(2)
            locked.append(h.checkin(sid, "newenv", "9994A001", pin=pin, pin2="", grant=g))
        assert not any(j["ok"] for j in locked), locked
        assert "鎖" in locked[-1]["error"] or "找老師" in locked[-1]["error"], locked[-1]
        t.shift(270 - 68)  # 4:30：老師在點名頁重設
        pk = h.pk("X", "9994A001")
        assert h.T.post(f"/api/t/student/{pk}/unbind", data={"reset_pin": "1"}).json()["reset"] is True
        t.shift(10)  # 4:40：同一個環境重新整理（重新取得表單憑證）後設新 PIN
        g2 = h.grant(sid, newenv)
        j = h.checkin(sid, "newenv", "9994A001", name="測試1", pin="5678", pin2="5678", grant=g2)
        row = h.db_rows(sid).get("9994A001")
    # 量到的現況：只記錄，不代表這是對的行為，見檔尾 MEASURED_*
    assert j["ok"] is True, j
    assert row is not None and row["status"] == MEASURED_PIN_LOCK, (j, row and row["status"])


def test_measure_new_env_lands_then_credential_expires_other_env_completes(hc):
    h = hc
    _set_windows(h)
    grant_seconds = None
    with time_machine.travel(T0, tick=False) as t:
        sid = h.open("X")
        with h.A.db() as con:
            grant_seconds = h.A.course_settings(h.A.get_course(con, h.cid["X"]))["grant_seconds"]
        t.shift(60)  # 1:00：新環境落地、沒送出
        h.grant(sid, h.dev("env1"))
        t.shift(175)  # 規格的「175 秒」＝學生頁倒數的 GRANT_SECONDS-5；伺服器端憑證時限 grant_seconds 預設 180，這時還沒過
        assert grant_seconds == 180
        t.shift(5)  # 另一個新環境重掃、完成首次綁定
        j = h.checkin(sid, "env2", "9994A002", name="測試2", pin="1234")
        row = h.db_rows(sid).get("9994A002")
    assert j["ok"] is True, j
    assert row is not None and row["status"] == MEASURED_EXPIRED_RESCAN, (j, row and row["status"])


MEASURED_PIN_LOCK = "late"  # 實測：4:40 完成＝遲到（出席窗 3:00；判定看重新整理後那張憑證的發證時間）
MEASURED_EXPIRED_RESCAN = "late"  # 實測：4:00 由另一個新環境重掃完成＝遲到


def test_phone_bound_to_student_b_gets_no_receipt_for_student_a_after_close(hc):
    """乙的手機綁在乙名下、乙本場有出席列；結案後用這支手機送甲的學號，不能拿到甲的收據（防借用）。"""
    h = hc
    s0 = h.open("X")
    assert h.checkin(s0, "own2", "9994A002", name="測試2", pin="1234")["ok"]
    h.T.post(f"/api/t/session/{s0}/close")
    sid = h.open("X")
    for stu in ("9994A001", "9994A002"):
        assert h.T.post(f"/api/t/session/{sid}/status", data={"pk": h.pk("X", stu), "status": "present"}).json()["ok"]
    grant = h.grant(sid, h.dev("own2"))
    h.T.post(f"/api/t/session/{sid}/close")
    before = h.db_rows(sid)
    j = h.checkin(sid, "own2", "9994A001", pin="", pin2="", grant=grant)
    assert j["ok"] is False and "點名已經結束" in j["error"], j
    assert h.db_rows(sid) == before
