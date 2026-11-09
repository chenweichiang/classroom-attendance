"""tools/log_funnel.py 的回歸測試。

「落地成功卻從未送出簽到」不能把送出但全被擋的裝置也算進去；
「用回應大小反推錯誤原因」除了 HTTPException(...)，也要認得簽到路徑的 Reject(狀態碼, 訊息, 原因碼)，
否則日誌裡的 4xx 全部「對照表沒命中」。
"""
import importlib.util
import io
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location("log_funnel", ROOT / "tools" / "log_funnel.py")
LF = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(LF)


def _entry(ts, ip, method, uri, status, size=10, ua="Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) Version/18.0 Mobile Safari/604.1"):
    return json.dumps({"ts": ts, "status": status, "size": size,
                       "request": {"method": method, "uri": uri, "client_ip": ip,
                                   "headers": {"User-Agent": [ua], "Cf-Connecting-Ip": [ip]}}})


def _run(monkeypatch, capsys, lines):
    # 時間戳落在下面 --date／--from／--to 指定的那一段裡
    monkeypatch.setattr(sys, "argv", ["log_funnel.py", "--date", "2026-10-15", "--from", "15:30", "--to", "15:45",
                                      "--app", str(ROOT / "app.py")])
    monkeypatch.setattr(sys, "stdin", io.StringIO("\n".join(lines) + "\n"))
    assert LF.main() == 0
    return capsys.readouterr().out


def test_rejected_devices_are_not_counted_as_never_submitted(monkeypatch, capsys):
    t0 = 1792049760.0  # 2026-10-15 15:36:00 +08:00
    land = "/s/9/179204975/0123456789"
    lines = [
        # A：落地後送出兩次都被擋
        _entry(t0, "1.1.1.1", "GET", land, 200), _entry(t0 + 5, "1.1.1.1", "POST", "/api/checkin", 409, 284),
        _entry(t0 + 9, "1.1.1.1", "POST", "/api/checkin", 401, 100),
        # B：落地後一次都沒送出
        _entry(t0 + 1, "2.2.2.2", "GET", land, 200),
        # C：落地後成功
        _entry(t0 + 2, "3.3.3.3", "GET", land, 200), _entry(t0 + 8, "3.3.3.3", "POST", "/api/checkin", 200, 120),
    ]
    out = _run(monkeypatch, capsys, lines)
    a, b = LF.sha1_4("1.1.1.1"), LF.sha1_4("2.2.2.2")
    never = next(line for line in out.splitlines() if line.startswith("落地成功卻從未送出簽到的裝置"))
    assert b in never and a not in never and "1 支" in never
    rejected = next(line for line in out.splitlines() if line.startswith("送出簽到但從未成功的裝置"))
    assert a in rejected and b not in rejected and "1 支" in rejected
    assert "409×1" in rejected and "401×1" in rejected


def test_size_table_parses_reject_calls_including_multiline_and_from_none():
    src = '''
def f():
    raise Reject(410, "填表時間已到，請重新掃描 QR", "grant_expired") from None
    raise Reject(409, "第一段"
                 "第二段", "bound_other_device")
    raise Reject(400, NOT_MATCH, "not_match")
    raise Reject(401, f"剩 {left} 次" if left > 0 else "鎖定了", "pin_wrong")
    raise HTTPException(404, "找不到這次點名")
'''
    ex = LF.extract_http_exceptions(src)
    lit = {(e["status"], e["text"]) for e in ex if e["kind"] == "literal"}
    assert (410, "填表時間已到，請重新掃描 QR") in lit
    assert (409, "第一段第二段") in lit
    assert (401, "鎖定了") in lit
    assert (404, "找不到這次點名") in lit
    kinds = {(e["status"], e["kind"]) for e in ex}
    assert (400, "variable") in kinds and (401, "fstring") in kinds
    # 原因碼不能被當成訊息
    assert not any(e.get("text") in ("grant_expired", "bound_other_device", "not_match", "pin_wrong") for e in ex)


def test_size_table_matches_a_real_reject_response(h):
    """量對東西：拿 app 真的回出去的 410 位元組數，對照表要查得到同一句話。"""
    r = h.dev("p1").post("/api/checkin", data={"grant": "g:1:x:0:y:bad", "student_id": "9994A001"})
    assert r.status_code == 410
    table, _ = LF.build_size_table(LF.extract_http_exceptions((ROOT / "app.py").read_text(encoding="utf-8")))
    assert r.json()["error"] in table.get((410, len(r.content)), [])


# 對照表展開 f-string、裝置欄說明

def _size_table():
    return LF.build_size_table(LF.extract_http_exceptions((ROOT / "app.py").read_text(encoding="utf-8")))[0]


def test_size_table_covers_wrong_pin_remaining_attempts_message(h):
    """「PIN 錯誤，剩 2 次。…」是 f-string 組出來的，以前整條略過，日誌裡 401 一律「對照表沒命中」。"""
    s1 = h.open("CD")
    assert h.checkin(s1, "dev1-A", "9994A001", name="學生1", pin="1234")["ok"]
    h.T.post(f"/api/t/session/{s1}/close")
    s2 = h.open("CD")
    cl = h.dev("dev1-B")
    r = cl.post("/api/checkin", data={"grant": h.grant(s2, cl), "student_id": "9994A001", "pin": "9999", "pin2": "", "hints": "h"})
    assert r.status_code == 401 and "剩 2 次" in r.json()["error"], r.text
    assert r.json()["error"] in _size_table().get((401, len(r.content)), [])


def test_size_table_covers_409_already_recorded_with_each_status_label(h):
    s1 = h.open("CD")
    assert h.checkin(s1, "dev1-C", "9994A001", name="學生1", pin="1234")["ok"]
    h.T.post(f"/api/t/session/{s1}/close")
    s2 = h.open("CD")
    assert h.checkin(s2, "dev1-C", "9994A001", pin="1234")["ok"]
    cl = h.dev("dev1-D")
    r = cl.post("/api/checkin", data={"grant": h.grant(s2, cl), "student_id": "9994A001", "pin": "1234", "pin2": "", "hints": "h"})
    assert r.status_code == 409 and "這個學號這一場已經有紀錄（出席）" in r.json()["error"], r.text
    assert r.json()["error"] in _size_table().get((409, len(r.content)), [])


def test_expansion_only_for_left_and_status_label_other_fstrings_still_skipped():
    src = '''
STATUS_LABEL = {"present": "出席", "late": "遲到"}
def f():
    raise Reject(401, f"剩 {left} 次", "x")
    raise Reject(409, f"記為「{STATUS_LABEL[prior['status']]}」，{left} 次", "y")
    raise Reject(400, f"不明 {something}", "z")
'''
    table, notes = LF.build_size_table(LF.extract_http_exceptions(src))
    texts = {t for v in table.values() for t in v}
    assert {f"剩 {n} 次" for n in range(0, 11)} <= texts
    assert "記為「出席」，3 次" in texts and "記為「遲到」，10 次" in texts
    assert len([t for t in texts if t.startswith("記為")]) == 2 * 11
    assert not any("不明" in t for t in texts)
    assert any("不明" in n for n in notes), notes


def test_first_line_warns_that_device_column_is_ip_hash(monkeypatch, capsys):
    t0 = 1792049760.0
    lines = [_entry(t0, "1.1.1.1", "GET", "/s/9/179204975/0123456789", 200)]
    out = _run(monkeypatch, capsys, lines)
    first = out.splitlines()[0]
    # 這支工具不再做「疑似替人簽」的判斷，第一行提示跟著改
    assert first == ("注意：「裝置」欄是出口 IP 的雜湊。同一個校園網路（NAT）後面的多支手機會併成同一個，"
                     "被擋的人可能被併掉；誰被擋、誰替誰簽以 tools/session_report.py 為準。"), first


# NAT 假訊號

UA_PHONES = [f"Mozilla/5.0 (iPhone; CPU iPhone OS 18_{i} like Mac OS X) Version/18.0 Mobile Safari/604.1" for i in range(6)]


def test_six_phones_behind_one_egress_each_signing_their_own_is_not_proxy_signing(monkeypatch, capsys):
    """校園 NAT／行動網路：6 支手機同一個出口 IP，各簽各的一次 200。以前同 IP 第二筆起算「疑似替人簽」。"""
    t0 = 1792049760.0
    land = "/s/9/179204975/0123456789"
    lines = []
    for i, ua in enumerate(UA_PHONES):
        lines += [_entry(t0 + i, "9.9.9.9", "GET", land, 200, ua=ua), _entry(t0 + 10 + i, "9.9.9.9", "POST", "/api/checkin", 200, 120, ua=ua)]
    out = _run(monkeypatch, capsys, lines)
    assert "替人簽" not in out.replace("誰替誰簽以 tools/session_report.py 為準", ""), out
    assert "疑似替人簽" not in out, out
    assert "/api/checkin 各狀態碼數：200:6" in out, out


def test_same_ip_second_success_is_not_reported_either(monkeypatch, capsys):
    t0 = 1792049760.0
    land = "/s/9/179204975/0123456789"
    lines = [_entry(t0, "1.1.1.1", "GET", land, 200), _entry(t0 + 5, "1.1.1.1", "POST", "/api/checkin", 200, 120),
             _entry(t0 + 9, "1.1.1.1", "POST", "/api/checkin", 200, 120)]
    out = _run(monkeypatch, capsys, lines)
    assert not any(x.startswith("同一裝置成功後又送出其他簽到") for x in out.splitlines()), out


def test_teacher_machine_is_ua_plus_teacher_path_not_the_whole_egress_ip(monkeypatch, capsys):
    """老師的筆電（UA T）打了 /t 與老師端 API，學生手機（UA S）在同一個出口 IP 後面落地簽到：只剔除老師那一組，學生要留著。"""
    t0 = 1792049760.0
    land = "/s/9/179204975/0123456789"
    teacher_ua = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 Version/18.0 Safari/605.1.15"
    lines = [
        _entry(t0 - 3, "7.7.7.7", "GET", "/t", 200, ua=teacher_ua),
        _entry(t0 - 2, "7.7.7.7", "POST", "/api/t/session/9/teacher-ip", 200, ua=teacher_ua),
        _entry(t0, "7.7.7.7", "GET", "/c", 200, ua=teacher_ua),  # 老師自己開 /c 看畫面，不算學生
        _entry(t0 + 1, "7.7.7.7", "GET", land, 200, ua=UA_PHONES[0]),
        _entry(t0 + 8, "7.7.7.7", "POST", "/api/checkin", 200, 120, ua=UA_PHONES[0]),
    ]
    out = _run(monkeypatch, capsys, lines)
    assert "不同學生裝置數：1" in out, out
    assert "第一次落地→第一次簽到 200 秒數分佈（1 支）" in out, out


def test_header_documents_nat_limits_teacher_rule_and_410_scope():
    doc = LF.__doc__
    assert "UA" in doc and "老師端路徑" in doc, "檔頭要說明老師機器改用 UA＋老師端路徑判定"
    assert "410" in doc and "已簽到" in doc and "不含" in doc, "檔頭要寫明 410 不含已簽到回訪"
    assert "疑似替人簽" in doc and "NAT" in doc, "檔頭要說明為什麼拿掉疑似替人簽"
    assert "同裝置多次簽到疑似替人簽" not in doc, "漏斗彙總清單不該再列這一項"
