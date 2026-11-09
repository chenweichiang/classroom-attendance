"""簽到與靜態檔的幾條規則：學號正規化、PIN 錯誤計次事件、帶版本號的靜態檔長期快取、
作廢的綁定場次不擋重綁。已結束場次的畫面與回應在 test_closed_session.py；名冊匯入的檢查在 test_manage.py。
素材一律自建：課程代碼 XA，學號 9994A 開頭，姓名只用 FAKE_NAMES.txt 的名字或「測試N」。"""
import os
import subprocess
import sys
from pathlib import Path

import pytest
import time_machine

from conftest import Helpers

TOOL = Path(__file__).resolve().parent.parent / "tools" / "session_report.py"
T0 = 1_790_000_000.0  # 固定時間（2026-09 中旬），讓老師頁輸出可逐字比對


@pytest.fixture()
def h17(A, teacher):
    """自建一門課與四位學生；回傳 Helpers（課程鍵是 'X'）。"""
    with A.db() as con:
        con.execute("INSERT INTO courses(code,name) VALUES('XA','規則測試')")
        cid = con.execute("SELECT id FROM courses WHERE code='XA'").fetchone()["id"]
        for sid, name in (("9994A001", "測試1"), ("9994A002", "測試2"), ("9994A003", "王小明"), ("9994A004", "測試4")):
            con.execute("INSERT INTO students(course_id,student_id,name,cls) VALUES(?,?,?,?)", (cid, sid, name, "測試班"))
    return Helpers(A, teacher, {"X": cid})


def events(A, kind):
    with A.db() as con:
        return [dict(r) for r in con.execute("SELECT * FROM events WHERE kind=? ORDER BY id", (kind,))]


# 簽到查學號的正規化

def test_checkin_finds_student_despite_zero_width_fullwidth_and_lowercase(h17):
    h = h17
    sid = h.open("X")
    variants = {
        "9994A001": "\u200b9994A001\ufeff",       # 前後夾零寬
        "9994A002": "9994A\u200b\u200d002",        # 中間夾零寬
        "9994A003": "９９９４Ａ００３",              # 全形英數
        "9994A004": " 9994a004 ",                  # 小寫加空白
    }
    names = {"9994A001": "測試1", "9994A002": "測試2", "9994A003": "王小明", "9994A004": "測試4"}
    for i, (canon, raw) in enumerate(variants.items()):
        j = h.checkin(sid, f"dev2-{i}", raw, name=names[canon], pin="1234")
        assert j.get("ok") is True, (canon, repr(raw), j)
        assert j["student_id"] == canon, (canon, j)
    assert sorted(h.db_rows(sid)) == sorted(variants), h.db_rows(sid).keys()


# PIN 錯誤事件帶累計次數

def _bound(h, sid, dev, stu, name):
    j = h.checkin(sid, dev, stu, name=name, pin="1234")
    assert j["ok"], j


def _pin_counts(h):
    return [(e["reason"], e["student_pk"]) for e in events(h.A, "pin_count")]


def test_same_wrong_pin_resent_is_dup_and_new_wrong_pin_counts_up(h17):
    h = h17
    sid = h.open("X")
    _bound(h, sid, "owner", "9994A001", "測試1")
    pk = h.pk("X", "9994A001")
    # 別支手機（只輸 PIN）連錯：同一個錯 PIN 重送、換一組、再換一組
    for pin in ("9999", "9999", "8888"):
        j = h.checkin(sid, "other", "9994A001", pin=pin, pin2="")
        assert not j["ok"]
    assert _pin_counts(h) == [("1/3", pk), ("1/3 dup", pk), ("2/3", pk)]
    rej = [e["reason"] for e in events(h.A, "checkin_reject")]
    assert rej == ["pin_wrong"] * 3, "checkin_reject 照舊每次記一筆"


def test_pin_exists_path_also_carries_count(h17):
    h = h17
    sid = h.open("X")
    _bound(h, sid, "owner", "9994A002", "測試2")
    pk = h.pk("X", "9994A002")
    j = h.checkin(sid, "other", "9994A002", name="測試2", pin="7777")  # 首綁表單（帶 pin2），但學號已設過 PIN
    assert not j["ok"]
    assert [e["reason"] for e in events(h.A, "checkin_reject")] == ["pin_exists"]
    assert _pin_counts(h) == [("1/3", pk)]


def test_variant_name_counts_for_that_student_but_reject_stays_unattributed(h17):
    h = h17
    sid = h.open("X")
    _bound(h, sid, "owner", "9994A003", "王小明")
    pk = h.pk("X", "9994A003")
    j = h.checkin(sid, "other", "9994A003", name="王小華", pin="9999", pin2="")  # 同姓同字數
    assert not j["ok"]
    assert _pin_counts(h) == [("1/3 variant", pk)], _pin_counts(h)
    rej = events(h.A, "checkin_reject")
    assert [(e["reason"], e["student_pk"]) for e in rej] == [("pin_wrong", None)], "checkin_reject 維持不歸戶"


def test_pin_count_is_invisible_to_teacher_outputs(h17):
    h = h17
    with time_machine.travel(T0, tick=False):
        sid = h.open("X")
        _bound(h, sid, "owner", "9994A001", "測試1")
        _bound(h, sid, "owner3", "9994A003", "王小明")
        for pin in ("9999", "9999", "8888"):
            h.checkin(sid, "other", "9994A001", pin=pin, pin2="")
        h.checkin(sid, "other2", "9994A003", name="王小華", pin="9999", pin2="")
        assert len(events(h.A, "pin_count")) == 4

        def snap():
            st = h.T.get(f"/api/t/session/{sid}/state")
            csvr = h.T.get(f"/t/course/{h.cid['X']}/export/csv")
            page = h.T.get(f"/t/session/{sid}")
            return st.text, csvr.text, page.status_code, page.text

        before = snap()
        with h.A.db() as con:
            con.execute("DELETE FROM events WHERE kind='pin_count'")
        after = snap()
    assert before == after, "老師頁 API、CSV、場次頁都不能因為多了 pin_count 事件而改變"


def test_session_report_lists_pin_count_sequence_anonymously(h17):
    h = h17
    sid = h.open("X")
    _bound(h, sid, "owner", "9994A001", "測試1")
    for pin in ("9999", "9999", "8888", "7777"):
        h.checkin(sid, "other", "9994A001", pin=pin, pin2="")
    env = {**os.environ, "ATTEND_DB": str(h.A.DB_PATH)}
    r = subprocess.run([sys.executable, str(TOOL), str(sid)], capture_output=True, text=True, env=env, timeout=30)
    assert r.returncode == 0, r.stderr
    line = [ln for ln in r.stdout.splitlines() if "PIN 計次：" in ln]
    assert len(line) == 1, r.stdout
    assert line[0].split("PIN 計次：")[1] == "1/3、1/3 dup、2/3、3/3", line[0]
    assert line[0].split()[0].startswith("S"), line[0]
    for secret in ("9994A001", "測試1"):
        assert secret not in r.stdout


# 帶版本號的字型長期快取

def _a_static_path():
    root = Path(__file__).resolve().parent.parent / "static"
    f = next(p for p in sorted(root.rglob("*")) if p.is_file())
    return "/static/" + f.relative_to(root).as_posix()


def test_static_with_matching_font_version_is_immutable_one_year(A, client):
    p = _a_static_path()
    r = client.get(p, params={"v": A.FONT_V})
    assert r.status_code == 200
    assert r.headers["cache-control"] == "public, max-age=31536000, immutable"


def test_static_with_wrong_or_missing_version_keeps_seven_days(A, client):
    p = _a_static_path()
    assert client.get(p, params={"v": "deadbeef00"}).headers["cache-control"] == "public, max-age=604800"
    assert client.get(p).headers["cache-control"] == "public, max-age=604800"


# 作廢的綁定場次不擋重綁

def _revoke_in_bind_session(h, stu, name):
    sid = h.open("X", kind="bind")
    assert h.checkin(sid, "dev1-old", stu, name=name, pin="1234")["ok"] is True
    pick = h.T.post(f"/api/t/session/{sid}/spotcheck").json()["picks"][0]
    assert h.T.post(f"/api/t/spotcheck/{pick['id']}", data={"present": "0"}).json()["unbound"] is True
    return sid


def test_voided_bind_session_no_longer_blocks_rebind_same_day(h17):
    h = h17
    sid = _revoke_in_bind_session(h, "9994A001", "測試1")
    pk = h.pk("X", "9994A001")
    h.T.post(f"/api/t/session/{sid}/close")
    assert h.T.post(f"/api/t/session/{sid}/void", data={"void": "1"}).json() == {"ok": True}
    assert h.T.post(f"/api/t/student/{pk}/unbind", data={"reset_pin": ""}).json()["revoked_today"] is False
    sid2 = h.open("X")
    j = h.checkin(sid2, "dev1-new", "9994A001", name="測試1", pin="5678")
    assert j["ok"] is True, j


def test_not_voided_bind_session_still_blocks_rebind(h17):
    h = h17
    sid = _revoke_in_bind_session(h, "9994A002", "測試2")
    pk = h.pk("X", "9994A002")
    h.T.post(f"/api/t/session/{sid}/close")
    assert h.T.post(f"/api/t/student/{pk}/unbind", data={"reset_pin": ""}).json()["revoked_today"] is True
    sid2 = h.open("X")
    j = h.checkin(sid2, "dev1-new", "9994A002", name="測試2", pin="5678")
    assert j["ok"] is False and "今天不能再綁定" in j["error"], j
