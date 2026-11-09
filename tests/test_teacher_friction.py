"""老師端與投影頁摩擦的回歸測試。
每條測試對應一句話根因，寫在各自的 docstring。"""
import json
import re
import sqlite3
import threading
import time

import pytest
import hashlib
import time_machine


# events 表與 log_event

def test_events_table_exists_and_columns(A):
    with A.db() as con:
        cols = {r["name"] for r in con.execute("PRAGMA table_info(events)")}
    assert cols == {"id", "ts", "session_id", "kind", "reason", "device8", "ua_class", "student_pk"}


def test_log_event_writes_row(A):
    A.log_event(1, "land_ok", device8="abcd1234", ua_class="iOS-Safari")
    with A.db() as con:
        row = con.execute("SELECT * FROM events WHERE session_id=1").fetchone()
    assert row["kind"] == "land_ok"
    assert row["device8"] == "abcd1234"
    assert row["ua_class"] == "iOS-Safari"
    assert row["student_pk"] is None


def test_log_event_failure_does_not_raise(A, monkeypatch, capsys):
    """寫入失敗不得影響主流程：把 connect() 換成永遠炸的版本，log_event 仍要正常回傳，只印 stderr。"""
    def boom():
        raise sqlite3.OperationalError("裝壞的")
    monkeypatch.setattr(A, "connect", boom)
    A.log_event(1, "land_ok")  # 不應該拋例外
    captured = capsys.readouterr()
    assert "log_event 失敗" in captured.err


def test_log_event_on_shares_caller_connection(A):
    """log_event_on 用呼叫端傳入的連線，不自己開新連線、不自己 commit。"""
    with A.db() as con:
        A.log_event_on(con, 7, "checkin_ok", reason="present", device8="ffee0011", ua_class="Mac", student_pk=3)
        # 這裡還沒 commit（con() context manager結束時才會），用同一條連線立刻查得到
        row = con.execute("SELECT * FROM events WHERE session_id=7").fetchone()
        assert row["kind"] == "checkin_ok" and row["student_pk"] == 3


def test_classify_ua_categories(A):
    cases = {
        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1": "iOS-Safari",
        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) CriOS/120.0 Mobile/15E148 Safari/604.1": "iOS-Chrome",
        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148": "iOS-INAPP",
        "Mozilla/5.0 (Linux; Android 13; SM-G991B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Mobile Safari/537.36": "Android-Chrome",
        "Mozilla/5.0 (Linux; Android 13; SM-G991B) AppleWebKit/537.36 (KHTML, like Gecko) SamsungBrowser/23.0 Chrome/115.0 Mobile Safari/537.36": "Android-Samsung",
        "Mozilla/5.0 (Linux; Android 13; SM-G991B; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/120.0 Mobile Safari/537.36": "Android-WebView",
        "Mozilla/5.0 (Linux; Android 13; wv) AppleWebKit/537.36": "Android-WebView",
        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148 Line/13.0": "LINE",
        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 [FBAN/FBIOS]": "FB-IG",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 Safari/605.1.15": "Mac",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0 Safari/537.36": "Win",
        "curl/8.0": "other",
        "": "other",
    }
    for ua, want in cases.items():
        assert A.classify_ua(ua) == want, (ua, want, A.classify_ua(ua))


def test_classify_ua_isolated_subconditions(A):
    """每個 or 條件的各個子字串，在一般樣本裡多半是「跟別的子字串一起出現」，
    單獨只滿足其中一個子條件的樣本很少，or↔and 互換、大小寫變體、換成別的字串常量都測不出來。
    這裡每筆都刻意只讓一個子條件成立。"""
    cases = {
        # "Line;"（不含 "Line/"）單獨成立
        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148 Line;11.5.0": "LINE",
        # "FBAV"（不含 FBAN、Instagram）單獨成立；同時殺掉 FBAV/Instagram 之間 or→and 的突變
        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 [FBAV/450.0.0.0.83]": "FB-IG",
        # "Instagram"（不含 FBAN、FBAV）單獨成立
        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 Instagram 300.0.0.32.111": "FB-IG",
        # "iPad"（不含 iPhone、iPod；真實 iPad UA 是 "CPU OS"，不像 iPod 會夾帶 "CPU iPhone OS"）單獨成立；
        # 同時殺掉 iPad/iPod 之間 or→and 的突變
        "Mozilla/5.0 (iPad; CPU OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148": "iOS-INAPP",
        # 有 "Version/" 沒有 "Safari"：真實世界常見（WKWebView 型 in-app 瀏覽器常省略 Safari/ 版號），
        # and 被錯改成 or 時這裡會誤判成 iOS-Safari
        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148": "iOS-INAPP",
        # 有 "Safari" 沒有 "Version/"：同一個 and 條件的另一半
        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148 Safari/604.1": "iOS-INAPP",
    }
    for ua, want in cases.items():
        assert A.classify_ua(ua) == want, (ua, want, A.classify_ua(ua))
    # 等價突變（不寫測試，理由）：
    # - `u = ua or ""` → `u = ua or "XXXX"`：只有 ua 為假值（""／None）時這條路才會走到，兩種
    #   替代值都不含任何分類關鍵字，結果同為 "other"，沒有輸入能區分兩者。
    # - "iPod" 分支的字串替換（XXiPodXX／ipod／IPOD）：真實 iPod 裝置 UA 一律含 "CPU iPhone OS"
    #   字樣，"iPhone" in u 恆真、Python `or` 短路，"iPod" 那個子條件在真實格式下永遠不會被求值；
    #   要用不符真實格式的合成字串才能命中，故不補。


def test_grant_sid_hint_best_effort(A):
    tok = A.make_token("g", "42", "nonce", exp=A.now() + 60)
    assert A._grant_sid_hint(tok) == 42
    assert A._grant_sid_hint("garbage") is None
    assert A._grant_sid_hint("g:notanumber:x:1:sig") is None


# 簽到事件記錄不得跟主流程互鎖（database is locked 回歸）

def test_checkin_ok_events_do_not_deadlock_main_transaction(h):
    """api_checkin 早退（already/IntegrityError 分支）還握著 con 的寫入交易時，
    log_event 另開連線去寫 events 會跟它互鎖到 busy timeout（10 秒），e2e 卡住等不到 #receipt。
    連續多次含成功與重複送出（both 走 already 分支）、以及錯誤 PIN（走 reject 分支）都不得變慢或出現
    database is locked；events 記錄的筆數要精確。
    已綁裝置回簽免 PIN，錯 PIN 那次改用另一支未綁定的裝置才吃得到 PIN 驗證。
    不斷言「10 輪×3 次請求的總時間」：PIN 雜湊（pbkdf2 600,000 輪）在較慢機器上單次就要約 0.45 秒，
    20 次雜湊（每輪 2 次）就吃掉 9 秒以上，會把單純「機器比較慢」誤判成「疑似 database is locked」
    （見 test_single_request_ceiling_survives_slow_pin_hash 的證明）。
    真正的 bug 特徵是單一請求卡到 busy timeout（10 秒）以上，所以量每一次請求的耗時，
    斷言單次最慢 < 5 秒（遠低於 10 秒 busy timeout、又遠高於一次 PIN 雜湊）。"""
    with h.A.db() as con:
        for i in range(10):
            con.execute("INSERT OR IGNORE INTO students(course_id, student_id, name) VALUES(?,?,?)",
                        (h.cid["CD"], f"BURST{i:03d}", f"衝刺{i}"))
    sid = h.open("CD")
    worst = {"elapsed": -1.0, "round": None, "kind": None}

    def timed(round_i, kind, fn):
        t0 = time.time()
        result = fn()
        elapsed = time.time() - t0
        if elapsed > worst["elapsed"]:
            worst.update(elapsed=elapsed, round=round_i, kind=kind)
        return result

    for i in range(10):
        stu = f"BURST{i:03d}"
        r1 = timed(i, "首次簽到", lambda: h.checkin(sid, f"burstdev{i}", stu, name=f"衝刺{i}", pin="1234"))
        assert r1["ok"] is True and r1.get("again") is not True, r1
        r2 = timed(i, "重複送出", lambda: h.checkin(sid, f"burstdev{i}", stu, name=f"衝刺{i}", pin="1234"))  # 走 already 分支
        assert r2["ok"] is True and r2["again"] is True, r2
        bad_dev = h.dev(f"burstdev{i}-b")
        bad = timed(i, "錯誤 PIN", lambda: bad_dev.post("/api/checkin", data={
            "grant": h.grant(sid, bad_dev), "student_id": stu, "pin": "0000"}))
        assert bad.status_code == 401, bad.text
    assert worst["elapsed"] < 5, (
        f"單次請求耗時 {worst['elapsed']:.2f}s（第 {worst['round']} 輪、{worst['kind']}），"
        "疑似 database is locked 卡住（bug 重現時單次會卡到 busy timeout 10 秒以上）"
    )
    with h.A.db() as con:
        kinds = {}
        for r in con.execute("SELECT kind, COUNT(*) n FROM events WHERE session_id=? GROUP BY kind", (sid,)):
            kinds[r["kind"]] = r["n"]
    assert kinds.get("checkin_ok") == 20, kinds  # 10 人各 1 首簽 + 1 重複送出
    assert kinds.get("checkin_reject") == 10, kinds  # 10 人各 1 次錯 PIN


def test_single_request_ceiling_survives_slow_pin_hash(h, monkeypatch):
    """迴歸測試：證明若改用「總時間 < 6 秒」當判準，會被較慢的 PIN 雜湊拖垮而誤報，
    上面那條測試用的「單次請求 < 5 秒」判準則不受影響。用固定 sleep 取代真實運算時間的差異來源
    （不是在猜計時，是直接控制耗時本身，這裡就是在測計時判準的行為，符合例外）：
    每次 hash_pin 呼叫多花 0.37 秒，模擬較慢機器單次雜湊約 0.45 秒；
    10 輪×每輪最多 2 次雜湊呼叫，20 次 × 0.37 秒 = 7.4 秒，必定推過舊門檻的 6 秒，
    但單次請求的雜湊次數固定是 1 次，遠低於新門檻的 5 秒——不會是巧合，是刻意設計的。
    實測：把這裡的斷言換成 `assert elapsed < 6`，在同樣的 monkeypatch 下量到 9.38 秒、確實失敗，
    證實總時間判準會誤報。"""
    real_hash_pin = h.A.hash_pin

    def slow_hash_pin(pin, salt):
        time.sleep(0.37)
        return real_hash_pin(pin, salt)
    monkeypatch.setattr(h.A, "hash_pin", slow_hash_pin)

    with h.A.db() as con:
        for i in range(10):
            con.execute("INSERT OR IGNORE INTO students(course_id, student_id, name) VALUES(?,?,?)",
                        (h.cid["CD"], f"SLOW{i:03d}", f"慢速{i}"))
    sid = h.open("CD")
    total_t0 = time.time()
    worst = 0.0
    for i in range(10):
        stu = f"SLOW{i:03d}"
        t0 = time.time()
        r1 = h.checkin(sid, f"slowdev{i}", stu, name=f"慢速{i}", pin="1234")
        worst = max(worst, time.time() - t0)
        assert r1["ok"] is True and r1.get("again") is not True, r1
        t0 = time.time()
        r2 = h.checkin(sid, f"slowdev{i}", stu, name=f"慢速{i}", pin="1234")
        worst = max(worst, time.time() - t0)
        assert r2["ok"] is True and r2["again"] is True, r2
        bad_dev = h.dev(f"slowdev{i}-b")
        t0 = time.time()
        bad = bad_dev.post("/api/checkin", data={
            "grant": h.grant(sid, bad_dev), "student_id": stu, "pin": "0000"})
        worst = max(worst, time.time() - t0)
        assert bad.status_code == 401, bad.text
    total_elapsed = time.time() - total_t0

    assert worst < 5, f"單次最慢 {worst:.2f}s，才是真正卡住的訊號（busy timeout 是 10 秒）"
    # 證明本身：同一段正常流程（沒有任何死鎖），舊的「總時間 < 6 秒」判準在這裡會被純粹的
    # 雜湊耗時拖垮而誤報——這一行預期會通過（total_elapsed 確實 >= 6），刻意保留來記錄事實。
    assert total_elapsed >= 6, (
        f"總耗時只有 {total_elapsed:.2f}s，沒有超過舊門檻 6 秒，不足以證明舊判準真的會誤報，"
        "請確認 monkeypatch 的 sleep 秒數設定是否還有效"
    )


def test_do_checkin_integrity_error_branch_recovers_existing_row(h, monkeypatch):
    """do_checkin 裡 `except sqlite3.IntegrityError:` 那個分支（第 2314 行附近）是防禦性寫法：
    程式自己的註解說明本函式全程持有 BEGIN IMMEDIATE 寫入鎖，正常情況下第二個請求走到這裡之前，
    前一個請求已經 commit，「already」查詢一定看得到剛寫入的列，INSERT 不可能撞唯一鍵——這是
    刻意設計成打不到的防禦分支，不是能用真的兩個並發請求重現的 race（那樣只會卡在鎖，不會撞
    IntegrityError）。用 monkeypatch 讓「already」那次查詢（第一次執行到那句 SQL）假裝查無此列，
    製造出「表裡明明已經有一筆、程式卻以為沒有」的失真狀態，逼 INSERT 真的撞唯一鍵、真的丟出
    IntegrityError、真的走進 except 去補救——這是為了測到這支防禦碼本身而刻意採用的重現手法，
    不是規格指定的技巧（規格沒有指定；偏離與理由見回報「偏離規格」一節）。"""
    sid = h.open("CD")
    first = h.checkin(sid, "integdev1", "9994A001", name="學生1", pin="1234")
    assert first["ok"] is True and first.get("again") is not True, first

    real_connect = h.A.connect
    state = {"faked": False}

    class _NoRow:
        def fetchone(self):
            return None

    class _SpyConnection:
        def __init__(self, real):
            self._real = real

        def execute(self, sql, *args, **kwargs):
            if not state["faked"] and sql == "SELECT * FROM checkins WHERE session_id=? AND student_pk=?":
                state["faked"] = True
                return _NoRow()
            return self._real.execute(sql, *args, **kwargs)

        def __getattr__(self, name):
            return getattr(self._real, name)

    monkeypatch.setattr(h.A, "connect", lambda *a, **k: _SpyConnection(real_connect(*a, **k)))
    second = h.checkin(sid, "integdev1", "9994A001", name="學生1", pin="1234")
    assert state["faked"] is True, "monkeypatch 沒有攔到「already」那次查詢，這條測試沒有真的測到 IntegrityError 分支"
    assert second["ok"] is True and second["again"] is True, second
    assert second["ts"] == first["ts"] and second["status"] == first["status"], (
        "撞到 IntegrityError 後應該從表裡撈回原本那一筆的真實內容，不是憑空回應"
    )


# /api/t/session/{sid}/state 的 funnel 與 stuck

def test_funnel_counts_land_and_checkin(h):
    sid = h.open("CD")
    dev = h.dev("fdev1")
    # 落地成功兩次（QR 掃到、六位數對到都算 land_ok／code_ok 不影響 land 計數；這裡直接打 /s 落地路由）
    with h.A.db() as con:
        s = h.A.get_session(con, sid)
    slot = int(h.A.now() // 10)
    sig = h.A.slot_sig(s, slot)
    r = dev.get(f"/s/{sid}/{slot}/{sig}")
    assert r.status_code == 200
    r2 = dev.get(f"/s/{sid}/999999/deadbeef00")  # 假簽章 → land_expired
    assert r2.status_code == 410
    h.checkin(sid, "fdev1", "9994A001", name="學生1", pin="1234")  # checkin_ok
    # fdev1 已綁定，回簽免 PIN；錯 PIN 這筆改用另一支未綁定的裝置才吃得到 PIN 驗證
    dev2 = h.dev("fdev1-b")
    bad = dev2.post("/api/checkin", data={"grant": h.grant(sid, dev2), "student_id": "9994A001", "pin": "0000"})
    assert bad.status_code == 401  # checkin_reject: pin_wrong
    state = h.state(sid)
    fn = state["funnel"]["total"]
    assert fn["land_ok"] >= 1
    assert fn["land_expired"] >= 1
    assert fn["checkin_ok"] >= 1
    assert fn["reject"] >= 1
    assert fn["reject_by_reason"].get("pin_wrong", 0) >= 1


def test_funnel_reject_reason_breakdown(h):
    sid = h.open("CD")
    dev = h.dev("rdev1")
    # not_match：學號不存在
    r1 = dev.post("/api/checkin", data={"grant": h.grant(sid, dev), "student_id": "NOTEXIST9", "name": "誰", "pin": "1234", "pin2": "1234"})
    assert r1.status_code == 400
    # pin_format
    r2 = dev.post("/api/checkin", data={"grant": h.grant(sid, dev), "student_id": "9994A002", "name": "學生2", "pin": "12"})
    assert r2.status_code == 400
    state = h.state(sid)
    breakdown = state["funnel"]["total"]["reject_by_reason"]
    assert breakdown.get("not_match", 0) >= 1
    assert breakdown.get("pin_format", 0) >= 1


def test_funnel_recent60_excludes_old_events(h):
    sid = h.open("CD")
    with time_machine.travel(h.A.now(), tick=False) as trav:
        h.dev("old1")
        h.checkin(sid, "old1", "9994A001", name="學生1", pin="1234")
        trav.shift(120)  # 兩分鐘後才發生的事件不該進 recent60
        h.dev("old2")
        h.checkin(sid, "old2", "9994A002", name="學生2", pin="1234")
        state = h.state(sid)
    assert state["funnel"]["recent60"]["checkin_ok"] == 1, state["funnel"]["recent60"]
    assert state["funnel"]["total"]["checkin_ok"] == 2, state["funnel"]["total"]


def test_funnel_silent_device_counted_after_90s(h):
    """落地成功但 90 秒內沒有任何簽到嘗試才算沉默；不到 90 秒不算。"""
    sid = h.open("CD")
    with h.A.db() as con:
        s = h.A.get_session(con, sid)
    with time_machine.travel(h.A.now(), tick=False) as trav:
        slot = int(h.A.now() // 10)
        sig = h.A.slot_sig(s, slot)
        dev = h.dev("silentdev")
        r = dev.get(f"/s/{sid}/{slot}/{sig}")
        assert r.status_code == 200
        state_early = h.state(sid)
        assert state_early["funnel"]["total"]["silent"] == 0, "還沒滿 90 秒不該算沉默"
        trav.shift(95)
        state_late = h.state(sid)
    assert state_late["funnel"]["total"]["silent"] == 1, state_late["funnel"]["total"]


# 簽到遇到非預期例外要記事件，事件寫入不能卡住學生

def test_checkin_unexpected_exception_records_event_and_reraises(h, monkeypatch):
    """do_checkin 遇到 Reject 以外的例外（例如這裡直接製造一個確定會發生的 RuntimeError，不用
    真的鎖資料庫）要記一筆 checkin_error 事件、原樣往外拋，不能悶聲被 500 蓋掉——漏斗才看得出
    這支裝置『有送出過』而不是失蹤。"""
    sid = h.open("CD")
    dev = h.dev("dev17err")
    grant = h.grant(sid, dev)

    def boom(*a, **k):
        raise RuntimeError("模擬非預期例外")
    monkeypatch.setattr(h.A, "fingerprint", boom)
    with pytest.raises(RuntimeError):
        dev.post("/api/checkin", data={"grant": grant, "student_id": "9994A001", "name": "學生1",
                                        "pin": "1234", "pin2": "1234", "hints": "h"})
    with h.A.db() as con:
        ev = con.execute("SELECT reason FROM events WHERE session_id=? AND kind='checkin_error'", (sid,)).fetchone()
    assert ev is not None and ev["reason"] == "RuntimeError", ev


def test_funnel_server_error_excludes_device_from_silent(h):
    """漏斗要多一個 server_error 欄位；有 checkin_error 事件的裝置代表『有送出過只是伺服器炸了』，
    不能被算進「落地未送出」（silent）。"""
    sid = h.open("CD")
    with h.A.db() as con:
        s = h.A.get_session(con, sid)
    with time_machine.travel(h.A.now(), tick=False) as trav:
        slot = int(h.A.now() // 10)
        sig = h.A.slot_sig(s, slot)
        dev = h.dev("dev17funnel")
        r = dev.get(f"/s/{sid}/{slot}/{sig}")
        assert r.status_code == 200
        with h.A.db() as con:
            land = con.execute(
                "SELECT device8 FROM events WHERE session_id=? AND kind='land_ok' ORDER BY id DESC LIMIT 1", (sid,)).fetchone()
        trav.shift(1)  # 「有沒有送出過」查的是 ts>落地時間，跟落地同一瞬間不算，要先讓時間往前走一點
        h.A.log_event(sid, "checkin_error", reason="RuntimeError", device8=land["device8"])
        trav.shift(94)
        state = h.state(sid)
    assert state["funnel"]["total"]["server_error"] == 1, state["funnel"]["total"]
    assert state["funnel"]["total"]["silent"] == 0, "有 checkin_error 的裝置不該被算進落地未送出"


def test_log_event_short_timeout_keeps_landing_page_fast(h):
    """log_event 的連線逾時要改成 0.5 秒（不是跟主流程共用的 10 秒）——資料庫真的被另一條連線
    鎖住時，落地頁（/s/...）只呼叫 log_event 記事件，不能陪著等到 10 秒的 busy timeout。
    這裡真的用另一條連線 BEGIN IMMEDIATE 鎖住寫入、鎖住 4 秒：log_event 若共用 10 秒逾時，落地頁
    會等滿 4 秒才等到鎖放開；用 0.5 秒逾時則最多等 0.5 秒就放棄（事件可能沒寫到，但落地頁還是要快速回應）。
    計時只包落地這一個請求（裝置先建好），門檻 3 秒：正常路徑本機約 0.6 秒，慢的 CI 機器量過接近 2 秒，
    門檻要和鎖住秒數拉開，才分得出「放棄等鎖」和「等到鎖放開」。固定的鎖住秒數是為了製造一個可控的
    競態視窗，不是在猜某個操作要花多久，符合鐵則 6 的例外（測的就是逾時這個計時行為本身）。"""
    sid = h.open("CD")
    with h.A.db() as con:
        s = h.A.get_session(con, sid)
    slot = int(h.A.now() // 10)
    sig = h.A.slot_sig(s, slot)
    phone = h.dev("dev17land")  # 先建好裝置（會先開一次 /c），計時只包落地請求

    locker = sqlite3.connect(h.A.DB_PATH, timeout=30, check_same_thread=False)
    locker.execute("PRAGMA journal_mode=WAL")
    locker.execute("BEGIN IMMEDIATE")
    locker.execute("UPDATE sessions SET note=note WHERE id=?", (sid,))  # 佔住寫入鎖，不改資料

    def release():
        time.sleep(4)
        locker.commit(); locker.close()
    t = threading.Thread(target=release); t.start()
    try:
        t0 = time.time()
        r = phone.get(f"/s/{sid}/{slot}/{sig}")
        elapsed = time.time() - t0
    finally:
        t.join(timeout=10)
    assert r.status_code == 200
    assert elapsed < 3.0, (
        f"落地頁在資料庫被鎖住 4 秒的期間等了 {elapsed:.2f}s，log_event 逾時應該遠短於 4 秒"
    )


def test_locked_db_checkin_error_flushed_via_backlog(h):
    """補充 `test_checkin_unexpected_exception_records_event_and_reraises`：
    那條用 monkeypatch 丟例外，沒有真的鎖資料庫，抓不到真正的根因——資料庫真的被另一條
    連線 `BEGIN IMMEDIATE` 鎖住時，`do_checkin` 自己的寫入（10 秒逾時）跟 `log_event` 補記
    checkin_error 的那次嘗試（0.5 秒逾時）用的是同一個檔案，鎖沒放開兩次都拿不到鎖，事件照樣
    憑空消失。這裡真的用另一條連線鎖住；鎖包在 try/finally 裡，斷言失敗也一定
    會 ROLLBACK＋close，不會讓整個 pytest 卡住。"""
    from fastapi.testclient import TestClient
    sid = h.open("CD")
    with h.A.db() as con:
        s = h.A.get_session(con, sid)
    slot = int(h.A.now() // 10)
    sig = h.A.slot_sig(s, slot)
    # 這支要能看到真正的 500（不是讓 TestClient 把例外原樣往外丟到測試程序）：do_checkin 的
    # `except Exception as exc: log_event(...); raise` 會把例外原樣往外拋，交給 FastAPI 轉成
    # 500 回應；預設 TestClient(raise_server_exceptions=True) 反而會把它整個丟到 pytest 這裡，
    # 蓋掉「真的回了 500」這件事本身，跟 round4b5 那支原始重現腳本一樣要關掉這個行為。
    dev = TestClient(h.A.app, base_url="http://test", raise_server_exceptions=False)
    dev.get("/c")
    page = dev.get(f"/s/{sid}/{slot}/{sig}")
    assert page.status_code == 200
    grant = re.search(r'GRANT="([^"]+)"', page.text).group(1)

    lock = sqlite3.connect(h.A.DB_PATH, isolation_level=None, timeout=30)
    lock.execute("PRAGMA journal_mode=WAL")
    try:
        lock.execute("BEGIN IMMEDIATE")
        r = dev.post("/api/checkin", data={"grant": grant, "student_id": "9994A001", "name": "學生1",
                                            "pin": "1234", "pin2": "1234", "hints": "h"})
    finally:
        lock.execute("ROLLBACK")
        lock.close()
    assert r.status_code == 500, r.text

    # 鎖放開之後，隨便哪一次 log_event 都要把 backlog 積壓的事件一起補寫進去——這裡讓另一支
    # 裝置完整簽到成功（不只是落地）觸發，這樣這支裝置本身也不會被漏斗誤判成沉默（它真的簽到
    # 成功了），才乾淨地只驗「被鎖住那支裝置」有沒有被誤判。
    dev_b = TestClient(h.A.app, base_url="http://test", raise_server_exceptions=False)
    dev_b.get("/c")
    slot2 = int(h.A.now() // 10)
    sig2 = h.A.slot_sig(s, slot2)
    page2 = dev_b.get(f"/s/{sid}/{slot2}/{sig2}")
    assert page2.status_code == 200
    grant2 = re.search(r'GRANT="([^"]+)"', page2.text).group(1)
    r2 = dev_b.post("/api/checkin", data={"grant": grant2, "student_id": "9994A002", "name": "學生2",
                                           "pin": "1234", "pin2": "1234", "hints": "h"})
    assert r2.status_code == 200, r2.text

    with h.A.db() as con:
        ev = con.execute(
            "SELECT id, kind, reason, ts, device8 FROM events WHERE session_id=? ORDER BY id", (sid,)).fetchall()
    kinds = [(e["kind"], e["reason"]) for e in ev]
    assert ("checkin_error", "OperationalError") in kinds, kinds
    err_row = next(e for e in ev if e["kind"] == "checkin_error")
    land_rows = [e for e in ev if e["kind"] == "land_ok"]
    assert len(land_rows) == 2, land_rows
    second_land = land_rows[1]
    assert err_row["ts"] < second_land["ts"], (
        f"checkin_error 的 ts（{err_row['ts']}）應該早於放鎖後那一筆 land_ok（{second_land['ts']}）——"
        "backlog 補寫時要保留失敗當下的時間，不是之後補寫的時間"
    )

    with time_machine.travel(h.A.now(), tick=False) as trav:
        trav.shift(95)
        state = h.state(sid)
    f = state["funnel"]["total"]
    assert f["server_error"] == 1, f
    assert f["silent"] == 0, "被鎖住那支裝置有 checkin_error，不該算進落地未送出"


def test_event_backlog_flush_does_not_lose_or_duplicate_events(h, monkeypatch):
    """backlog 補寫要「不可遺失、不可重複」：連續兩次 log_event 都失敗（用 monkeypatch 讓
    connect() 對這個特定呼叫短暫丟例外，不用真的鎖表——這裡驗的是 backlog 資料結構本身的
    正確性，不是鎖的行為，跟上面那條真鎖測試分開關注），第三次成功時，三筆都要在、不重複。"""
    sid = h.open("CD")
    calls = {"n": 0}
    real_connect = h.A.connect

    def flaky_connect(timeout=10):
        if timeout == 0.5 and calls["n"] < 2:
            calls["n"] += 1
            raise sqlite3.OperationalError("模擬暫時拿不到鎖")
        return real_connect(timeout=timeout)
    monkeypatch.setattr(h.A, "connect", flaky_connect)
    h.A.log_event(sid, "checkin_error", reason="第一筆")
    h.A.log_event(sid, "checkin_error", reason="第二筆")
    h.A.log_event(sid, "checkin_error", reason="第三筆")
    with h.A.db() as con:
        rows = [r["reason"] for r in con.execute(
            "SELECT reason FROM events WHERE session_id=? AND kind='checkin_error' ORDER BY id", (sid,))]
    assert rows == ["第一筆", "第二筆", "第三筆"], rows


def test_event_backlog_capped_at_500(h, monkeypatch):
    """backlog 塞爆也不能無界成長——超過 500 筆之後長度要停在 500（deque maxlen）。"""
    h.A._event_backlog.clear()

    def always_fail(timeout=10):
        raise sqlite3.OperationalError("永遠失敗，只為了把 backlog 塞滿")
    monkeypatch.setattr(h.A, "connect", always_fail)
    for i in range(520):
        h.A.log_event(None, "checkin_error", reason=str(i))
    assert len(h.A._event_backlog) == 500, len(h.A._event_backlog)
    h.A._event_backlog.clear()


# 一筆壞資料不能卡死 backlog

def test_forged_huge_sid_grant_does_not_poison_backlog(h):
    """`_grant_sid_hint` 對攻擊者可控的 grant 字串沒有範圍檢查：偽造一個場次 id 30 個 9 的
    grant，`int(parts[1])` 算出遠超 SQLite INTEGER 範圍的天文數字。若 backlog 沒有隔離壞資料，
    這筆帶壞 session_id 的 checkin_reject 事件會卡進 backlog——之後每一次 log_event 都要
    跟它一起 executemany、一起因 OverflowError 失敗、一起被放回去，三筆正常的 land_ok 一筆都
    寫不進去。沒有隔離的實作會讓這條測試變紅。"""
    h.A._event_backlog.clear()
    anon = h.student()
    r = anon.post("/api/checkin", data={"grant": f"g:{'9' * 30}:x", "student_id": "9994A001",
                                         "name": "學生1", "pin": "1234", "pin2": "1234", "hints": "h"})
    assert r.status_code in (400, 410), r.text
    sid = h.open("CD")
    for i in range(3):
        h.A.log_event(sid, "land_ok", device8=f"dev24d{i}")
    with h.A.db() as con:
        n = con.execute("SELECT COUNT(*) FROM events WHERE session_id=? AND kind='land_ok'", (sid,)).fetchone()[0]
    assert n == 3, n
    assert len(h.A._event_backlog) == 0, list(h.A._event_backlog)


def test_beacon_huge_sid_becomes_null_session_id(h):
    """`/api/beacon` 不用登入，`sid` 表單欄位最長 32 位數字，一樣能餵進超出範圍的天文數字；
    要回 204（原本就是「故意不回錯誤內容」的端點），事件的 session_id 洗成 NULL，之後的正常
    事件要照樣寫得進去。"""
    h.A._event_backlog.clear()
    anon = h.student()
    r = anon.post("/api/beacon", data={"kind": "jserror", "msg": "boom", "sid": "9" * 32})
    assert r.status_code == 204, r.text
    sid = h.open("CD")
    h.A.log_event(sid, "land_ok", device8="dev24beacon")
    with h.A.db() as con:
        client_ev = con.execute(
            "SELECT session_id FROM events WHERE kind='client' ORDER BY id DESC LIMIT 1").fetchone()
        n = con.execute("SELECT COUNT(*) FROM events WHERE session_id=? AND kind='land_ok'", (sid,)).fetchone()[0]
    assert client_ev is not None and client_ev["session_id"] is None, dict(client_ev) if client_ev else None
    assert n == 1, n


def test_locked_db_with_forged_grant_and_normal_event_both_flush_cleanly(h):
    """鎖住期間偽造 grant 一次（會因鎖住而 OperationalError，放回 backlog）、正常事件一次
    （同樣被鎖住、放回 backlog）；放鎖後再一次正常事件要把兩筆一起補寫，backlog 清空。鎖包在
    try/finally 裡，斷言失敗也一定會 ROLLBACK＋close。偽造 grant 一路走的是簽章失敗分支，
    不會碰到 do_checkin 主流程那個 10 秒逾時的連線，只會卡在 log_event 自己的 0.5 秒逾時，
    所以這條不用像上一節真鎖測試那樣等 10 秒以上。"""
    h.A._event_backlog.clear()
    sid = h.open("CD")
    lock = sqlite3.connect(h.A.DB_PATH, isolation_level=None, timeout=30)
    lock.execute("PRAGMA journal_mode=WAL")
    try:
        lock.execute("BEGIN IMMEDIATE")
        anon = h.student()
        anon.post("/api/checkin", data={"grant": f"g:{'9' * 30}:x", "student_id": "9994A001",
                                         "name": "學生1", "pin": "1234", "pin2": "1234", "hints": "h"})
        h.A.log_event(sid, "land_ok", device8="dev24lockA")
    finally:
        lock.execute("ROLLBACK")
        lock.close()
    h.A.log_event(sid, "land_ok", device8="dev24lockB")
    with h.A.db() as con:
        n = con.execute("SELECT COUNT(*) FROM events WHERE session_id=? AND kind='land_ok'", (sid,)).fetchone()[0]
    assert n == 2, n
    assert len(h.A._event_backlog) == 0, list(h.A._event_backlog)


def test_log_event_on_huge_session_id_becomes_null(h):
    """`log_event_on` 跟主流程共用連線，壞資料在這裡丟例外會讓呼叫端整個請求 500——這裡直接
    驗證超出範圍的 session_id 不會讓它拋例外，而是寫成 NULL。"""
    with h.A.db() as con:
        h.A.log_event_on(con, 10 ** 30, "checkin_ok", reason="dev24test")
    with h.A.db() as con:
        row = con.execute(
            "SELECT session_id FROM events WHERE kind='checkin_ok' AND reason='dev24test' "
            "ORDER BY id DESC LIMIT 1").fetchone()
    assert row is not None and row["session_id"] is None, dict(row) if row else None


def test_session_html_renders_server_error_in_funnel(h):
    """老師頁的漏斗要接 server_error，才顯示得出「伺服器錯誤 N」與紅字。"""
    from pathlib import Path
    html = (Path(h.A.__file__).parent / "templates" / "session.html").read_text()
    assert "t.server_error" in html, "老師頁的 renderFunnel 沒有接 server_error"
    assert "伺服器錯誤" in html, "老師頁沒有『伺服器錯誤』這個字樣"


def test_stuck_lists_student_after_two_rejects(h):
    """已綁裝置回簽免 PIN，錯 PIN 這段改用另一支未綁定的裝置（stuck 判定只看 student_pk，不看裝置）。"""
    sid0 = h.open("CD")
    h.checkin(sid0, "stuckdev", "9994A003", name="學生3", pin="5555")  # 先在上一場綁定，才有 PIN 可以打錯
    h.T.post(f"/api/t/session/{sid0}/close")
    sid = h.open("CD")  # 新場次：這位還沒在「這一場」簽到成功，卡住的判定才對
    dev2 = h.dev("stuckdev-b")
    for _ in range(2):
        bad = dev2.post("/api/checkin", data={"grant": h.grant(sid, dev2), "student_id": "9994A003", "pin": "0000"})
        assert bad.status_code == 401
    state = h.state(sid)
    stuck_ids = {x["student_id"] for x in state["stuck"]}
    assert "9994A003" in stuck_ids, state["stuck"]
    entry = next(x for x in state["stuck"] if x["student_id"] == "9994A003")
    assert entry["reason"] == "pin_wrong"
    assert entry["reason_label"] == "PIN 錯誤"


def test_stuck_excludes_student_who_later_succeeds(h):
    """已綁裝置回簽免 PIN，錯 PIN 這段改用另一支未綁定的裝置；之後用本人已綁定的裝置成功簽到。"""
    sid0 = h.open("CD")
    h.checkin(sid0, "stuckdev2", "9994A004", name="學生4", pin="9999")  # 先在上一場綁定
    h.T.post(f"/api/t/session/{sid0}/close")
    sid = h.open("CD")
    dev2 = h.dev("stuckdev2-b")
    for _ in range(2):
        bad = dev2.post("/api/checkin", data={"grant": h.grant(sid, dev2), "student_id": "9994A004", "pin": "0000"})
        assert bad.status_code == 401
    ok = h.checkin(sid, "stuckdev2", "9994A004", name="學生4", pin="9999")
    assert ok["ok"] is True
    state = h.state(sid)
    stuck_ids = {x["student_id"] for x in state["stuck"]}
    assert "9994A004" not in stuck_ids, state["stuck"]


# /api/beacon

def test_beacon_records_client_error(A, client):
    r = client.post("/api/beacon", data={"kind": "jserror", "msg": "TypeError: x is undefined", "sid": "1"})
    assert r.status_code == 204
    with A.db() as con:
        row = con.execute("SELECT * FROM events WHERE kind='client'").fetchone()
    assert row is not None
    assert row["reason"].startswith("jserror:")
    assert "x is undefined" in row["reason"]
    assert row["session_id"] == 1


def test_beacon_rejects_bad_kind_silently(A, client):
    r = client.post("/api/beacon", data={"kind": "notallowed", "msg": "x"})
    assert r.status_code == 204
    with A.db() as con:
        n = con.execute("SELECT COUNT(*) FROM events WHERE kind='client'").fetchone()[0]
    assert n == 0


def test_beacon_truncates_long_message(A, client):
    long_msg = "x" * 500
    client.post("/api/beacon", data={"kind": "jserror", "msg": long_msg})
    with A.db() as con:
        row = con.execute("SELECT reason FROM events WHERE kind='client'").fetchone()
    assert len(row["reason"]) <= 200


def test_beacon_throttled_per_device(A, seeded):
    from fastapi.testclient import TestClient
    cl = TestClient(A.app, base_url="http://test")
    cl.get("/c")  # 拿裝置 cookie
    codes = [cl.post("/api/beacon", data={"kind": "jserror", "msg": f"e{i}"}).status_code for i in range(10)]
    assert all(c == 204 for c in codes)  # 超過限流也回 204（靜默丟掉），不噴錯
    with A.db() as con:
        n = con.execute("SELECT COUNT(*) FROM events WHERE kind='client'").fetchone()[0]
    assert n == 6, n  # 每裝置每分鐘最多 6 筆


# QR 靜區 ≥4 模組

def test_qr_svg_quiet_zone_at_least_4_modules(A):
    """app.qr_svg 產出的靜區要有 4 模組寬（ISO/IEC 18004 最低要求）。用同一條 url 分別跟參照的
    border=1／border=4 版本比對 viewBox 尺寸，不假設 SvgPathImage 內部的像素／mm 換算規則。"""
    import re as _re
    import qrcode
    import qrcode.image.svg

    def viewbox_size(svg: str) -> int:
        m = _re.search(r'viewBox="0 0 (\d+) (\d+)"', svg)
        assert m, svg[:200]
        return int(m.group(1))

    url = "http://test/s/1/0/abcdefabcd"
    vb_app = viewbox_size(A.qr_svg(url))
    vb_ref4 = viewbox_size(qrcode.make(url, image_factory=qrcode.image.svg.SvgPathImage, box_size=20, border=4).to_string(encoding="unicode"))
    vb_ref1 = viewbox_size(qrcode.make(url, image_factory=qrcode.image.svg.SvgPathImage, box_size=20, border=1).to_string(encoding="unicode"))
    assert vb_app == vb_ref4, (vb_app, vb_ref4)  # app 用的就是 border=4
    assert vb_app > vb_ref1, (vb_app, vb_ref1)  # 且確實比 border=1 的靜區大


def test_qr_svg_border_is_4_in_app(A):
    import inspect
    src = inspect.getsource(A.qr_svg)
    assert "border=4" in src


# 自動結案成功率過低不自動發 Discord

def test_discord_auto_held_when_below_min_rate(h, monkeypatch):
    sent = []
    monkeypatch.setattr(h.A, "discord_post", lambda ch, c: (sent.append((ch, c)) or (True, "HTTP 200")))
    h.A.DISCORD_TOKEN = "x"
    with h.A.db() as con:
        con.execute("UPDATE courses SET settings=? WHERE id=?",
                    (json.dumps({"discord_channel": "123456789012345"}), h.cid["CD"]))
    sid = h.open("CD")
    h.checkin(sid, "onlyone", "9994A001", name="學生1", pin="1234")  # 1/4 出席，低於 0.5 門檻
    with h.A.db() as con:
        s = h.A.get_session(con, sid)
        with time_machine.travel(s["late_until"] + 1, tick=False):
            h.A.finalize_expired(con)
        con.commit()
        row = con.execute("SELECT note FROM sessions WHERE id=?", (sid,)).fetchone()
    time.sleep(0.2)  # 給背景執行緒一點時間，確認「真的沒有發」而不是還沒發完
    assert row["note"] == "discord_held", row["note"]
    assert sent == [], f"成功率過低不該自動發：{sent}"
    state = h.state(sid)
    assert state["discord_held"] is True


def test_discord_held_is_surfaced_and_cleared_after_manual_send(h, monkeypatch):
    """暫緩之後老師要看得到、手動發完要消掉。後端回了 discord_held 但頁面沒接＝老師不知道名單沒發。"""
    from pathlib import Path
    sent = []
    monkeypatch.setattr(h.A, "discord_post", lambda ch, c: (sent.append((ch, c)) or (True, "HTTP 200")))
    h.A.DISCORD_TOKEN = "x"
    with h.A.db() as con:
        con.execute("UPDATE courses SET settings=? WHERE id=?",
                    (json.dumps({"discord_channel": "123456789012345"}), h.cid["CD"]))
    sid = h.open("CD")
    h.checkin(sid, "onlyone", "9994A001", name="學生1", pin="1234")
    with h.A.db() as con:
        s = h.A.get_session(con, sid)
        with time_machine.travel(s["late_until"] + 1, tick=False):
            h.A.finalize_expired(con)
        con.commit()
    assert h.state(sid)["discord_held"] is True and sent == []
    html = (Path(h.A.__file__).parent / "templates" / "session.html").read_text()
    assert "state.discord_held" in html and 'id="heldMsg"' in html, "老師頁沒有接 discord_held"
    r = h.T.post(f"/api/t/session/{sid}/notify")
    assert r.status_code == 200 and len(sent) >= 1
    assert h.state(sid)["discord_held"] is False, "手動發完後暫緩提示要消掉"


def test_discord_auto_sent_when_above_min_rate(h, monkeypatch):
    sent = []
    monkeypatch.setattr(h.A, "discord_post", lambda ch, c: (sent.append((ch, c)) or (True, "HTTP 200")))
    h.A.DISCORD_TOKEN = "x"
    with h.A.db() as con:
        con.execute("UPDATE courses SET settings=? WHERE id=?",
                    (json.dumps({"discord_channel": "123456789012345"}), h.cid["CD"]))
    sid = h.open("CD")
    for i, dev in enumerate(["m1", "m2", "m3"]):
        h.checkin(sid, dev, f"9994A00{i+1}", name=f"學生{i+1}", pin="1234")  # 3/4 出席，高於門檻
    with h.A.db() as con:
        s = h.A.get_session(con, sid)
        with time_machine.travel(s["late_until"] + 1, tick=False):
            h.A.finalize_expired(con)
        con.commit()
        row = con.execute("SELECT note FROM sessions WHERE id=?", (sid,)).fetchone()
    for _ in range(50):
        if sent:
            break
        time.sleep(0.05)
    assert row["note"] != "discord_held"
    assert len(sent) == 1


def test_attendance_rate_excludes_excused_from_denominator(h):
    sid = h.open("CD")
    pk = h.pk("CD", "9994A004")
    with h.A.db() as con:
        con.execute("INSERT INTO checkins(session_id, student_pk, ts, status, flags, source) VALUES(?,?,?,?,?,?)",
                    (sid, pk, 0, "excused", "[]", "manual"))
    h.checkin(sid, "ratedev", "9994A001", name="學生1", pin="1234")
    with h.A.db() as con:
        s = h.A.get_session(con, sid)
        rate = h.A.attendance_rate(con, s)
    # 應到 4 人，扣掉 1 位請假 → 分母 3；出席 1 人 → 1/3
    assert abs(rate - 1 / 3) < 1e-9, rate


def test_attendance_rate_all_status_categories_exact_value(h):
    """出席／遲到／待確認／缺席各至少一人時的精確出席率（只測沒有 late 與 pending 的場次、
    或沒斷言 excused 預設值為 0 的精確結果，就抓不到 ok 元組裡的 late/pending 被換掉、
    或 excused 的 counts.get 預設值被改成 1）。"""
    sid = h.open("CD")  # CD 名冊 4 人：9994A001~004
    pk1, pk2, pk3 = h.pk("CD", "9994A001"), h.pk("CD", "9994A002"), h.pk("CD", "9994A003")
    # 9994A004 完全沒有 checkins 列＝缺席，不進 counts
    with h.A.db() as con:
        con.executemany(
            "INSERT INTO checkins(session_id, student_pk, ts, status, flags, source) VALUES(?,?,?,?,?,?)",
            [(sid, pk1, 0, "present", "[]", "manual"),
             (sid, pk2, 0, "late", "[]", "manual"),
             (sid, pk3, 0, "pending", "[]", "manual")])
    with h.A.db() as con:
        s = h.A.get_session(con, sid)
        rate = h.A.attendance_rate(con, s)
    # 應到 4 人，無人請假 → 分母 4；出席+遲到+待確認共 3 → 3/4
    assert rate == 0.75, rate


def test_attendance_rate_zero_denominator_returns_none(h):
    """全班都請假時分母為 0，必須回 None、不得 ZeroDivisionError（denom>0 判斷被改成恆真或 >=0
    時，這裡會直接在除法那行炸例外，測試會失敗，等同殺掉該突變）。"""
    sid = h.open("OOP")  # OOP 名冊 2 人
    pk1, pk2 = h.pk("OOP", "999000025"), h.pk("OOP", "999000026")
    with h.A.db() as con:
        con.executemany(
            "INSERT INTO checkins(session_id, student_pk, ts, status, flags, source) VALUES(?,?,?,?,?,?)",
            [(sid, pk1, 0, "excused", "[]", "manual"),
             (sid, pk2, 0, "excused", "[]", "manual")])
    with h.A.db() as con:
        s = h.A.get_session(con, sid)
        rate = h.A.attendance_rate(con, s)
    assert rate is None, rate


def test_attendance_rate_denominator_exactly_one(h):
    """分母剛好是 1（DT 名冊 2 人，1 位請假）時仍要回傳數值，不是 None（denom>0 被錯改成
    denom>1 時，這種剛好等於 1 的邊界會被誤判成 None）。"""
    sid = h.open("DT")  # DT 名冊 2 人：9994A001、1153A009
    pk1, pk2 = h.pk("DT", "9994A001"), h.pk("DT", "1153A009")
    with h.A.db() as con:
        con.execute("INSERT INTO checkins(session_id, student_pk, ts, status, flags, source) VALUES(?,?,?,?,?,?)",
                    (sid, pk1, 0, "excused", "[]", "manual"))
        con.execute("INSERT INTO checkins(session_id, student_pk, ts, status, flags, source) VALUES(?,?,?,?,?,?)",
                    (sid, pk2, 0, "present", "[]", "manual"))
    with h.A.db() as con:
        s = h.A.get_session(con, sid)
        rate = h.A.attendance_rate(con, s)
    assert rate == 1.0, rate


def test_finalize_expired_returns_finalized_session_ids(h):
    """finalize_expired 的回傳值就是被結案的場次 id 清單，不是 None 佔位（app.py:done.append）。"""
    sid = h.open("CD")
    with h.A.db() as con:
        s = h.A.get_session(con, sid)
        with time_machine.travel(s["late_until"] + 1, tick=False):
            done = h.A.finalize_expired(con)
        con.commit()
    assert done == [sid], done


def test_manual_close_holds_when_rate_too_low(h, monkeypatch):
    """手動按「結束點名」也套用「出席不到一半就暫緩發送」，
    不是不看成功率一律照發（一律照發是要擋掉的行為）。"""
    sent = []
    monkeypatch.setattr(h.A, "discord_post", lambda ch, c: (sent.append((ch, c)) or (True, "HTTP 200")))
    h.A.DISCORD_TOKEN = "x"
    with h.A.db() as con:
        con.execute("UPDATE courses SET settings=? WHERE id=?",
                    (json.dumps({"discord_channel": "123456789012345"}), h.cid["CD"]))
    sid = h.open("CD")  # 0 人出席，遠低於門檻
    r = h.T.post(f"/api/t/session/{sid}/close")
    assert r.json() == {"ok": True, "discord": False, "held": True}
    assert sent == []


# 首綁 ≥5 人之後 same_fp／bind_burst 都不再標

def test_bind_burst_and_same_fp_suppressed_after_five_binds(h, seeded):
    with h.A.db() as con:
        for i in range(10):
            con.execute("INSERT OR IGNORE INTO students(course_id, student_id, name) VALUES(?,?,?)",
                        (h.cid["CD"], f"MASS{i:03d}", f"量產{i}"))
    sid = h.open("CD")
    ip = "10.10.10.10"
    for i in range(7):
        stu = f"MASS{i:03d}"
        dev = h.dev(f"massdev{i}")
        r = dev.post("/api/checkin", data={
            "grant": h.grant(sid, dev), "student_id": stu, "name": f"量產{i}", "pin": "1234", "pin2": "1234"},
            headers={"user-agent": "iPhone-mass", "cf-connecting-ip": ip})
        j = r.json()
        assert j["ok"] is True, j
        row = h.row(sid, stu)
        if i < 2:
            # 前兩筆還沒滿 5 人門檻（同來源多筆首綁在第 3 筆才會觸發，這裡先確認流程正常）
            continue
        if i >= 5:  # 第 6、7 人時，本場首綁已經 >=5，不該再標 bind_burst／same_fp
            assert "bind_burst" not in row["flags"], row["flags"]
            assert "same_fp" not in row["flags"], row["flags"]


def test_bind_burst_still_flags_before_five_binds(h):
    """對照組：人少的正常時段（<5 人首綁）同來源多筆首綁仍要標，不能被我們的修正一起消掉。"""
    sid = h.open("CD")
    ip = "10.10.10.20"
    for i, sidv in enumerate(("9994A001", "9994A002", "9994A003")):
        dev = h.dev(f"smalldev{i}")
        r = dev.post("/api/checkin", data={
            "grant": h.grant(sid, dev), "student_id": sidv, "name": f"學生{sidv[-1]}", "pin": "1234", "pin2": "1234"},
            headers={"user-agent": "iPhone-small", "cf-connecting-ip": ip})
        assert r.json()["ok"] is True
    row = h.row(sid, "9994A003")
    assert "bind_burst" in row["flags"], row["flags"]  # 第三筆兩分鐘內同來源首綁 → 標記


# 老師頁手動簽到改用 /status 端點，接受學號後 3 碼與姓名片段（頁面邏輯在 e2e-teacher 測）

def test_status_endpoint_still_used_for_manual_checkin(h):
    """前端改成候選清單直接呼叫 /status；後端端點本身不變，這裡守住它沒被改壞。"""
    sid = h.open("CD")
    pk = h.pk("CD", "9994A002")
    r = h.T.post(f"/api/t/session/{sid}/status", data={"pk": pk, "status": "present"})
    assert r.json()["ok"] is True
    row = h.row(sid, "9994A002")
    assert row["status"] == "present"


# 綁定場次（共用定義＋新場次種類）

def test_bind_session_no_late_window_and_kind_recorded(h):
    sid = h.open("CD", kind="bind")
    with h.A.db() as con:
        row = con.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
    assert row["kind"] == "bind"
    assert row["open_until"] == row["late_until"], "綁定場次沒有遲到區，open_until 應等於 late_until"
    assert row["late_until"] - row["opened_at"] == h.A.DEFAULT_SETTINGS["bind_minutes"] * 60


def test_normal_session_kind_and_late_window_unchanged(h):
    """對照組：一般場次行為完全不變（kind='normal'、仍有獨立的遲到窗）。"""
    sid = h.open("CD")
    with h.A.db() as con:
        row = con.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
    assert row["kind"] == "normal"
    assert row["open_until"] < row["late_until"]


def test_variant_name_first_bind_flagged(h):
    """姓名寫法不同的首綁要記到 name_variant 旗標（這條原只在 test_friction.py
    測過，這裡補一份給覆蓋率算進去的三個檔案用）。"""
    with h.A.db() as con:
        con.execute("INSERT INTO students(course_id,student_id,name,cls) VALUES(?,?,?,?)",
                    (h.cid["CD"], "TF-VAR1", "林溫蒂", ""))
    sid = h.open("CD")
    j = h.checkin(sid, "tf-variant", "TF-VAR1", name="林温蒂", pin="1234")
    assert j["ok"] is True, j
    assert "name_variant" in h.row(sid, "TF-VAR1")["flags"]


def test_open_session_rejects_unknown_kind(h):
    """kind 只收 normal／bind，其餘一律 400。"""
    cid = h.cid["CD"]
    r = h.T.post(f"/t/course/{cid}/open", data={"kind": "bogus"})
    assert r.status_code == 400 and "種類不合法" in r.text


def test_bind_session_close_writes_no_auto_absent_and_no_discord(h, monkeypatch):
    sent = []
    monkeypatch.setattr(h.A, "discord_post", lambda ch, c: (sent.append((ch, c)) or (True, "HTTP 200")))
    h.A.DISCORD_TOKEN = "x"
    with h.A.db() as con:
        con.execute("UPDATE courses SET settings=? WHERE id=?",
                    (json.dumps({"discord_channel": "123456789012345"}), h.cid["CD"]))
    sid = h.open("CD", kind="bind")
    resp = h.checkin(sid, "binddev1", "9994A001", name="學生1", pin="1111")
    assert resp["ok"] is True and resp["kind"] == "bind", resp
    r = h.T.post(f"/api/t/session/{sid}/close")
    assert r.status_code == 200
    assert r.json()["discord"] is False, r.json()
    assert not sent, "綁定場次結案不該發 Discord"
    with h.A.db() as con:
        n_auto = con.execute("SELECT COUNT(*) FROM checkins WHERE session_id=? AND source='auto'", (sid,)).fetchone()[0]
    assert n_auto == 0, "綁定場次結案不該寫自動缺席列"


def test_bind_session_auto_close_no_discord_no_absent(h, monkeypatch):
    """老師忘了按結束、遲到窗過了讓 finalize_expired 自動結案，綁定場次同樣不寫自動缺席、不發 Discord。"""
    sent = []
    monkeypatch.setattr(h.A, "discord_post", lambda ch, c: (sent.append((ch, c)) or (True, "HTTP 200")))
    h.A.DISCORD_TOKEN = "x"
    with h.A.db() as con:
        con.execute("UPDATE courses SET settings=? WHERE id=?",
                    (json.dumps({"discord_channel": "123456789012345"}), h.cid["CD"]))
    sid = h.open("CD", kind="bind")
    with h.A.db() as con:
        con.execute("UPDATE sessions SET opened_at=opened_at-10000, open_until=open_until-10000, "
                    "late_until=late_until-10000 WHERE id=?", (sid,))
        done = h.A.finalize_expired(con)
    assert sid in done
    assert not sent, "綁定場次自動結案不該發 Discord"
    with h.A.db() as con:
        n_auto = con.execute("SELECT COUNT(*) FROM checkins WHERE session_id=? AND source='auto'", (sid,)).fetchone()[0]
    assert n_auto == 0


def test_bind_session_excluded_from_report_csv_and_me(h):
    sid = h.open("CD", kind="bind")
    resp = h.checkin(sid, "binddev2", "9994A001", name="學生1", pin="2222")
    assert resp["ok"] is True
    h.T.post(f"/api/t/session/{sid}/close")
    with h.A.db() as con:
        sessions, _rows = h.A.build_report(con, h.cid["CD"])
    assert sid not in [s["id"] for s in sessions], "綁定場次不該出現在報表"
    csv_resp = h.T.get(f"/t/course/{h.cid['CD']}/export/csv")
    assert csv_resp.status_code == 200
    header = csv_resp.text.lstrip("﻿").split("\n")[0]
    assert header.count(",") == 8, f"綁定場次不該多出一欄場次日期：{header!r}"
    dev = h.dev("binddev2")
    me = dev.post("/api/me").json()
    course = next(c for c in me["courses"] if c["student_id"] == "9994A001")
    assert course["items"] == [], "綁定場次不該出現在 /api/me"


def test_attendance_rate_none_for_bind_session(h):
    sid = h.open("CD", kind="bind")
    with h.A.db() as con:
        s = con.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
        assert h.A.attendance_rate(con, s) is None


def test_bind_session_allows_first_bind_even_when_allow_bind_off(h):
    h.T.post(f"/api/t/course/{h.cid['CD']}/bind", data={"allow": "0"})
    sid = h.open("CD", kind="bind")
    resp = h.checkin(sid, "binddev3", "9994A001", name="學生1", pin="3333")
    assert resp["ok"] is True, resp
    h.T.post(f"/api/t/session/{sid}/close")
    # 對照：同課一般場次仍照 allow_bind 關閉擋下（先結束綁定場次，同課同時只會有一場 live）
    sid2 = h.open("CD")
    resp2 = h.dev("binddev4").post("/api/checkin", data={
        "grant": h.grant(sid2, h.dev("binddev4")), "student_id": "9994A002", "name": "學生2",
        "pin": "4444", "pin2": "4444"})
    assert resp2.status_code == 403, resp2.text


def test_bind_session_spotcheck_now_enabled(h):
    """綁定場次的抽點是開放的；候選人規則與一般場次相同。
    api_spotcheck 在綁定場次不回 409，期望能抽到人。"""
    sid = h.open("CD", kind="bind")
    h.checkin(sid, "binddev5", "9994A001", name="學生1", pin="5555")
    r = h.T.post(f"/api/t/session/{sid}/spotcheck")
    assert r.status_code == 200, r.text
    picks = r.json()["picks"]
    assert len(picks) == 1 and picks[0]["student_id"] == "9994A001", picks


def test_bind_session_spotcheck_absent_revokes_binding(h):
    """抽點結果不在場時，綁定場次要撤回那個學號的綁定（device_hash/bound_at/pin_hash/pin_salt
    全清空，因為那筆綁定很可能不是本人做的），回應多帶 unbound: true。"""
    sid = h.open("CD", kind="bind")
    h.checkin(sid, "binddev6", "9994A001", name="學生1", pin="6666")
    with h.A.db() as con:
        before = con.execute("SELECT device_hash, pin_hash, bound_at FROM students WHERE student_id='9994A001'").fetchone()
    assert before["device_hash"] is not None and before["pin_hash"] is not None
    pick = h.T.post(f"/api/t/session/{sid}/spotcheck").json()["picks"][0]
    r = h.T.post(f"/api/t/spotcheck/{pick['id']}", data={"present": "0"})
    assert r.status_code == 200, r.text
    assert r.json()["unbound"] is True, r.json()
    with h.A.db() as con:
        after = con.execute("SELECT device_hash, pin_hash, pin_salt, bound_at FROM students WHERE student_id='9994A001'").fetchone()
    assert after["device_hash"] is None and after["pin_hash"] is None, after
    assert after["pin_salt"] is None and after["bound_at"] is None, after


def _revoke_in_bind_session(h, dev="proxydev"):
    """代綁者在綁定場次綁走 9994A001，老師叫名人不在、判不在場 → 綁定被撤回。回傳場次 id。"""
    sid = h.open("CD", kind="bind")
    assert h.checkin(sid, dev, "9994A001", name="學生1", pin="9999")["ok"] is True
    pick = h.T.post(f"/api/t/session/{sid}/spotcheck").json()["picks"][0]
    assert h.T.post(f"/api/t/spotcheck/{pick['id']}", data={"present": "0"}).json()["unbound"] is True
    return sid


# ids 用 ASCII：中文 id 會被 pytest 跳脫成 \uXXXX，mutmut 拿那串去選測試時 pytest 找不到（結束碼 4），整輪突變白跑
@pytest.mark.parametrize("rebind_dev", ["proxydev", "freshcookiedev"], ids=["same-cookie", "new-cookie"])
def test_no_rebind_in_same_bind_session_after_spot_absent(h, rebind_dev):
    """本場不能再綁。綁定場次抽點判不在、綁定被撤回之後，代綁者用同一支
    手機馬上重掃（或換一個 cookie）不得把那個學號又綁回去——否則撤回兩秒就被還原，而那個學號本場
    已經抽過、不會再被抽到。看的是學號不是裝置，換 cookie 繞不過。"""
    sid = _revoke_in_bind_session(h)
    r = h.dev(rebind_dev).post("/api/checkin", data={
        "grant": h.grant(sid, h.dev(rebind_dev)), "student_id": "9994A001", "name": "學生1",
        "pin": "9999", "pin2": "9999", "hints": "h"}, headers={"user-agent": "iPhone", "cf-connecting-ip": "140.131.1.1"})
    assert r.status_code == 403, r.text
    assert "找老師" in r.json()["error"], r.json()
    with h.A.db() as con:
        stu = con.execute("SELECT device_hash, pin_hash FROM students WHERE student_id='9994A001' AND course_id=?",
                          (h.cid["CD"],)).fetchone()
        reason = con.execute("SELECT reason FROM events WHERE session_id=? AND kind='checkin_reject' ORDER BY id DESC LIMIT 1",
                             (sid,)).fetchone()["reason"]
    assert stu["device_hash"] is None and stu["pin_hash"] is None, "撤回之後本場不得再被綁上"
    assert reason == "bind_revoked", reason
    assert h.A.REJECT_REASON_LABEL.get("bind_revoked"), "老師頁漏斗要有這個拒絕原因的中文標籤"
    assert h.row(sid, "9994A001")["bound"] is False


def test_supersedes_same_day_new_normal_session_blocks_rebind(h):
    """同一天接著開的一般點名若開放首次綁定，代綁者換裝置就能把撤回的學號
    馬上綁回去，所以同一天新開的一般點名也要擋，期望 403（規則同上一條）。
    隔天才能綁的版本另見 test_next_day_can_bind。"""
    sid = _revoke_in_bind_session(h, dev="dev14dev1")
    h.T.post(f"/api/t/session/{sid}/close")
    sid2 = h.open("CD")  # 同一天新開一般點名，allow_bind 預設開著
    j = h.dev("dev14dev2").post("/api/checkin", data={
        "grant": h.grant(sid2, h.dev("dev14dev2")), "student_id": "9994A001", "name": "學生1",
        "pin": "1234", "pin2": "1234", "hints": "h"}, headers={"user-agent": "iPhone", "cf-connecting-ip": "140.131.1.1"})
    assert j.status_code == 403, j.text
    assert j.json()["error"].startswith("老師今天叫名時"), j.json()
    assert h.row(sid2, "9994A001")["bound"] is False


def test_same_day_new_bind_session_also_blocks(h):
    """同一天再開一場新的綁定場次，一樣要擋（不論現在這場是綁定還是一般）。"""
    sid = _revoke_in_bind_session(h, dev="dev14dev3")
    h.T.post(f"/api/t/session/{sid}/close")
    sid2 = h.open("CD", kind="bind")
    j = h.dev("dev14dev4").post("/api/checkin", data={
        "grant": h.grant(sid2, h.dev("dev14dev4")), "student_id": "9994A001", "name": "學生1",
        "pin": "1234", "pin2": "1234", "hints": "h"}, headers={"user-agent": "iPhone", "cf-connecting-ip": "140.131.1.1"})
    assert j.status_code == 403, j.text


def test_next_day_can_bind(h):
    """隔天（跨過台灣時間 00:00）之後照常可以綁——不是永久封鎖。"""
    sid = _revoke_in_bind_session(h, dev="dev14dev5")
    h.T.post(f"/api/t/session/{sid}/close")
    with time_machine.travel(h.A.now() + 86400, tick=False):
        sid2 = h.open("CD")
        j = h.checkin(sid2, "dev14dev6", "9994A001", name="學生1", pin="1234")
    assert j["ok"] is True and j["first_time"] is True, j
    assert h.row(sid2, "9994A001")["bound"] is True


def test_unaffected_student_same_day_still_can_bind(h):
    """沒被判不在的學生，同一天照常可以綁（只擋被判過不在的那個學號）。"""
    sid = _revoke_in_bind_session(h, dev="dev14dev7")
    h.T.post(f"/api/t/session/{sid}/close")
    sid2 = h.open("CD")
    j = h.checkin(sid2, "dev14dev8", "9994A002", name="學生2", pin="2222")
    assert j["ok"] is True and j["first_time"] is True, j


def test_other_students_unaffected_in_same_bind_session(h):
    """只擋被判不在場的那個學號，同一場其他人的首綁不受影響。"""
    sid = _revoke_in_bind_session(h)
    j = h.checkin(sid, "otherdev", "9994A002", name="學生2", pin="2222")
    assert j["ok"] is True and j["first_time"] is True, j


def test_normal_session_spotcheck_absent_does_not_unbind(h):
    """一般場次的抽點行為完全不變：抽點結果不在場不觸發解綁，也不回 unbound。"""
    sid = h.open("CD")
    h.checkin(sid, "binddev7", "9994A001", name="學生1", pin="7777")
    pick = h.T.post(f"/api/t/session/{sid}/spotcheck").json()["picks"][0]
    r = h.T.post(f"/api/t/spotcheck/{pick['id']}", data={"present": "0"})
    assert r.status_code == 200, r.text
    assert "unbound" not in r.json(), r.json()
    with h.A.db() as con:
        after = con.execute("SELECT device_hash, pin_hash FROM students WHERE student_id='9994A001'").fetchone()
    assert after["device_hash"] is not None and after["pin_hash"] is not None, after


def test_bind_session_extend_extends_both_open_and_late_until(h):
    sid = h.open("CD", kind="bind")
    with h.A.db() as con:
        before = con.execute("SELECT open_until, late_until FROM sessions WHERE id=?", (sid,)).fetchone()
    r = h.T.post(f"/api/t/session/{sid}/extend", data={"minutes": 2})
    assert r.status_code == 200 and r.json()["phase"] == "open"
    with h.A.db() as con:
        after = con.execute("SELECT open_until, late_until FROM sessions WHERE id=?", (sid,)).fetchone()
    assert after["open_until"] == before["open_until"] + 120
    assert after["late_until"] == before["late_until"] + 120
    assert after["open_until"] == after["late_until"], "延長後仍不該出現遲到區"


# 本場作廢但保留＋自動缺席整批更正

def test_void_requires_closed_session(h):
    sid = h.open("CD")  # 還沒結束
    r = h.T.post(f"/api/t/session/{sid}/void", data={"void": "1"})
    assert r.status_code == 409, r.text


def test_void_excludes_from_report_csv_me_and_can_be_undone(h):
    sid = h.open("CD")
    resp = h.checkin(sid, "voiddev1", "9994A001", name="學生1", pin="1212")
    assert resp["ok"] is True
    h.T.post(f"/api/t/session/{sid}/close")
    with h.A.db() as con:
        sessions_before, _ = h.A.build_report(con, h.cid["CD"])
    assert sid in [s["id"] for s in sessions_before]

    r = h.T.post(f"/api/t/session/{sid}/void", data={"void": "1"})
    assert r.status_code == 200 and r.json()["ok"] is True
    with h.A.db() as con:
        row = con.execute("SELECT voided_at FROM sessions WHERE id=?", (sid,)).fetchone()
        assert row["voided_at"] is not None
        sessions_voided, _ = h.A.build_report(con, h.cid["CD"])
    assert sid not in [s["id"] for s in sessions_voided], "作廢後不該出現在報表"
    dev = h.dev("voiddev1")
    me = dev.post("/api/me").json()
    course = next(c for c in me["courses"] if c["student_id"] == "9994A001")
    assert course["items"] == [], "作廢後不該出現在 /api/me"
    # 紀錄本身保留、可查
    with h.A.db() as con:
        k = con.execute("SELECT * FROM checkins WHERE session_id=? AND student_pk=(SELECT id FROM students WHERE student_id='9994A001' AND course_id=?)",
                        (sid, h.cid["CD"])).fetchone()
    assert k is not None and k["status"] == "present", "作廢不該動到既有簽到紀錄"

    # 取消作廢：重新計入
    r2 = h.T.post(f"/api/t/session/{sid}/void", data={"void": "0"})
    assert r2.status_code == 200
    with h.A.db() as con:
        row2 = con.execute("SELECT voided_at FROM sessions WHERE id=?", (sid,)).fetchone()
        assert row2["voided_at"] is None
        sessions_after, _ = h.A.build_report(con, h.cid["CD"])
    assert sid in [s["id"] for s in sessions_after], "取消作廢後應重新出現在報表"


def test_bulk_status_only_touches_auto_absent_rows(h):
    sid = h.open("CD")
    # 9994A001：真的簽到出席；9994A002：老師手動記請假；9994A003／9994A004：不簽，結案後變自動缺席
    resp = h.checkin(sid, "bulkdev1", "9994A001", name="學生1", pin="1111")
    assert resp["ok"] is True
    pk2 = h.pk("CD", "9994A002")
    r = h.T.post(f"/api/t/session/{sid}/status", data={"pk": pk2, "status": "excused"})
    assert r.json()["ok"] is True
    before_close = h.T.post(f"/api/t/session/{sid}/bulk-status", data={"to": "present"})
    assert before_close.status_code == 409, "未結束的場次不能整批更正"

    h.T.post(f"/api/t/session/{sid}/close")
    with h.A.db() as con:
        n_auto_before = con.execute(
            "SELECT COUNT(*) FROM checkins WHERE session_id=? AND source='auto' AND status='absent'", (sid,)).fetchone()[0]
    assert n_auto_before == 2, "9994A003、9994A004 沒簽，結案後應各有一筆自動缺席"

    r2 = h.T.post(f"/api/t/session/{sid}/bulk-status", data={"to": "present"})
    assert r2.status_code == 200
    j2 = r2.json()
    assert j2["ok"] is True and j2["n"] == 2, j2

    row1 = h.row(sid, "9994A001")
    assert row1["status"] == "present" and row1["source"] == "qr", "學生自己簽的列不該被整批更正動到"
    row2 = h.row(sid, "9994A002")
    assert row2["status"] == "excused" and "manual" in row2["flags"], "老師手動請假不該被整批更正動到"
    for stu in ("9994A003", "9994A004"):
        row = h.row(sid, stu)
        assert row["status"] == "present" and row["source"] == "manual" and "manual" in row["flags"], row

    # 已經整批更正過，本場自動缺席歸零，再呼叫一次不會誤動別的列
    # 整批更正拿掉「缺席」這個目標狀態（absent 現在直接 400），改用仍合法的 present 驗證冪等
    r3 = h.T.post(f"/api/t/session/{sid}/bulk-status", data={"to": "present"})
    assert r3.status_code == 200 and r3.json()["n"] == 0
    row1_again = h.row(sid, "9994A001")
    assert row1_again["status"] == "present", "第二次整批更正不該把已經處理過的人改回缺席"


def test_bulk_status_rejects_invalid_status(h):
    sid = h.open("CD")
    h.T.post(f"/api/t/session/{sid}/close")
    r = h.T.post(f"/api/t/session/{sid}/bulk-status", data={"to": "pending"})
    assert r.status_code == 400, r.text


# 通知的守門

def test_notify_rejects_bind_session(h):
    sid = h.open("CD", kind="bind")
    h.checkin(sid, "notifybind1", "9994A001", name="學生1", pin="2222")
    h.T.post(f"/api/t/session/{sid}/close")
    r = h.T.post(f"/api/t/session/{sid}/notify")
    assert r.status_code == 409, r.text
    assert "綁定" in r.json()["error"], r.text


def test_notify_rejects_voided_session(h):
    sid = h.open("CD")
    h.checkin(sid, "notifyvoid1", "9994A001", name="學生1", pin="3333")
    h.T.post(f"/api/t/session/{sid}/close")
    r0 = h.T.post(f"/api/t/session/{sid}/void", data={"void": "1"})
    assert r0.status_code == 200, r0.text
    r = h.T.post(f"/api/t/session/{sid}/notify")
    assert r.status_code == 409, r.text
    assert "作廢" in r.json()["error"], r.text


def test_void_hides_discord_held_without_clearing_note(h, monkeypatch):
    """改寫斷言——作廢不再清掉 note，而是讀 discord_held 的地方（api_state）額外要求
    voided_at IS NULL；這樣取消作廢後提示才會自己回來，不用另外找地方補寫 note。原本這支測試斷言
    「作廢後 DB 的 note 被清成空字串」，跟新規格相反，改成斷言 note 全程保持 discord_held、
    只有 api_state 算出來的 discord_held 隨作廢／取消作廢切換。"""
    monkeypatch.setattr(h.A, "discord_post", lambda ch, c: (True, "HTTP 200"))
    h.A.DISCORD_TOKEN = "x"
    with h.A.db() as con:
        con.execute("UPDATE courses SET settings=? WHERE id=?",
                    (json.dumps({"discord_channel": "123456789012345"}), h.cid["CD"]))
    sid = h.open("CD")
    h.checkin(sid, "notifyheld1", "9994A001", name="學生1", pin="4444")  # 1/4 出席，低於 0.5 門檻
    with h.A.db() as con:
        s = h.A.get_session(con, sid)
        with time_machine.travel(s["late_until"] + 1, tick=False):
            h.A.finalize_expired(con)
        con.commit()
        row = con.execute("SELECT note FROM sessions WHERE id=?", (sid,)).fetchone()
    assert row["note"] == "discord_held", row["note"]
    assert h.state(sid)["discord_held"] is True
    r = h.T.post(f"/api/t/session/{sid}/void", data={"void": "1"})
    assert r.status_code == 200, r.text
    with h.A.db() as con:
        row2 = con.execute("SELECT note FROM sessions WHERE id=?", (sid,)).fetchone()
    assert row2["note"] == "discord_held", "作廢不再清 note，DB 裡的痕跡要留著"
    assert h.state(sid)["discord_held"] is False, "作廢時提示要跟著隱藏"
    r2 = h.T.post(f"/api/t/session/{sid}/void", data={"void": "0"})
    assert r2.status_code == 200, r2.text
    assert h.state(sid)["discord_held"] is True, "取消作廢後提示要自己回來，不用另外補寫 note"


def test_notify_runs_finalize_expired_first(h, monkeypatch):
    """遲到窗剛過、還沒人開過老師頁時，缺席列還沒寫入；notify 要自己先跑 finalize_expired 補上再發。"""
    sent = []
    monkeypatch.setattr(h.A, "discord_post", lambda ch, c: (sent.append((ch, c)) or (True, "HTTP 200")))
    h.A.DISCORD_TOKEN = "x"
    with h.A.db() as con:
        con.execute("UPDATE courses SET settings=? WHERE id=?",
                    (json.dumps({"discord_channel": "123456789012345"}), h.cid["CD"]))
    sid = h.open("CD")
    h.checkin(sid, "notifydev1", "9994A001", name="學生1", pin="1111")
    with h.A.db() as con:
        s = h.A.get_session(con, sid)
    with time_machine.travel(s["late_until"] + 1, tick=False):
        with h.A.db() as con:
            assert con.execute("SELECT closed_at FROM sessions WHERE id=?", (sid,)).fetchone()["closed_at"] is None
            n_auto_before = con.execute("SELECT COUNT(*) FROM checkins WHERE session_id=? AND source='auto'", (sid,)).fetchone()[0]
        assert n_auto_before == 0, "還沒 finalize，不該已經有自動缺席列"
        r = h.T.post(f"/api/t/session/{sid}/notify")
    assert r.status_code == 200, r.text
    with h.A.db() as con:
        row = con.execute("SELECT closed_at FROM sessions WHERE id=?", (sid,)).fetchone()
        n_auto_after = con.execute(
            "SELECT COUNT(*) FROM checkins WHERE session_id=? AND source='auto' AND status='absent'", (sid,)).fetchone()[0]
    assert row["closed_at"] is not None, "notify 應該先跑 finalize_expired 把場次結案"
    assert n_auto_after == 3, "9994A002/003/004 沒簽，應各有一筆自動缺席"
    assert sent, "應該要發 Discord"
    for stu in ("9994A002", "9994A003", "9994A004"):
        assert stu in sent[-1][1], f"{stu} 沒簽到卻沒出現在缺席名單：{sent[-1][1]}"


# 綁定場次不接受手動改狀態

def test_manual_status_rejected_for_existing_row_in_bind_session(h):
    sid = h.open("CD", kind="bind")
    h.checkin(sid, "t2dev1", "9994A001", name="學生1", pin="1234")
    r = h.T.post(f"/api/t/session/{sid}/status", data={"pk": h.pk("CD", "9994A001"), "status": "absent"})
    assert r.status_code == 409, r.text
    assert "另開一般點名" in r.json()["error"], r.text


def test_manual_checkin_rejected_for_bind_session(h):
    """手動簽到走的是同一支端點，一個從沒簽過的學號也該被擋。"""
    sid = h.open("CD", kind="bind")
    r = h.T.post(f"/api/t/session/{sid}/status", data={"pk": h.pk("CD", "9994A002"), "status": "present"})
    assert r.status_code == 409, r.text


def test_status_unaffected_for_normal_session(h):
    """對照組：一般場次手動改狀態完全不變。"""
    sid = h.open("CD")
    r = h.T.post(f"/api/t/session/{sid}/status", data={"pk": h.pk("CD", "9994A001"), "status": "present"})
    assert r.status_code == 200, r.text


def test_state_reports_bound_n_for_bind_session(h):
    sid = h.open("CD", kind="bind")
    h.checkin(sid, "t2dev2", "9994A001", name="學生1", pin="1111")
    h.checkin(sid, "t2dev3", "9994A002", name="學生2", pin="2222")
    st = h.state(sid)
    assert st["bound_n"] == 2, st


# 同課同時只能一場未結案

def test_concurrent_open_only_one_live_session(A):
    from fastapi.testclient import TestClient
    teacher = TestClient(A.app, base_url="http://test")
    assert teacher.post("/t/login", data={"password": "pw", "next": "/t"}, follow_redirects=False).status_code == 303
    with A.db() as con:
        con.execute("INSERT INTO courses(code,name) VALUES('CD','脈絡設計')")
        cid = con.execute("SELECT id FROM courses").fetchone()["id"]
        con.execute("INSERT INTO students(course_id,student_id,name,created_at) VALUES(?,'A001','學生A001',0)", (cid,))
        # 上一堂忘了按結束、遲到窗已過但還沒被 finalize（老師沒開過頁面就會是這狀態）
        t = A.now()
        con.execute("INSERT INTO sessions(course_id,secret,opened_at,open_until,late_until) VALUES(?,'x',?,?,?)",
                    (cid, t - 2000, t - 1900, t - 10))

    real_hex = A.secrets.token_hex
    paused = threading.Event(); resume = threading.Event()

    class _Secrets:
        def __getattr__(self, k):
            return getattr(A.secrets, k)

        def token_hex(self, n):
            if threading.current_thread().name == "A" and not paused.is_set():
                paused.set(); resume.wait(8)  # A 已通過 live 檢查，還沒 INSERT
            return real_hex(n)

    A.secrets = _Secrets()
    out = {}

    def run(name, kind):
        def f():
            try:
                with A.db() as con:
                    out[name] = A.do_open_session(con, cid, kind)
            except Exception as e:  # noqa: BLE001
                out[name + "_err"] = f"{type(e).__name__}: {e}"
        return threading.Thread(target=f, name=name)

    ta = run("A", "bind"); ta.start(); paused.wait(8)
    tb = run("B", "normal"); tb.start(); tb.join(6)
    resume.set(); ta.join(8)
    with A.db() as con:
        live = [(r["id"], r["kind"]) for r in con.execute(
            "SELECT id,kind FROM sessions WHERE course_id=? AND closed_at IS NULL AND late_until>?", (cid, A.now()))]
    assert len(live) == 1, f"同課同時只能一場未結案，實得 {live}（out={out}）"


def test_open_survives_losing_finalize_race(A):
    """finalize_expired 的 SELECT 讀到過期場次之後、UPDATE 之前，另一個輪詢（投影頁／老師頁）
    先把它結案——這邊的 UPDATE rowcount=0 仍會讓 Python sqlite3 開出隱式交易，finalize_expired 以前
    只在「有結到」才 commit，交易就這樣留著，do_open_session 緊接的 BEGIN IMMEDIATE 直接炸
    「cannot start a transaction within a transaction」，老師按「開始點名」看到 500。
    用代理連線把「另一個輪詢先贏」固定在 SELECT 與 UPDATE 之間，不靠運氣撞時序。"""
    with A.db() as con:
        con.execute("INSERT INTO courses(code,name) VALUES('CD','脈絡設計')")
        cid = con.execute("SELECT id FROM courses").fetchone()["id"]
        con.execute("INSERT INTO students(course_id,student_id,name,created_at) VALUES(?,'A001','學生A001',0)", (cid,))
        t = A.now()
        con.execute("INSERT INTO sessions(course_id,secret,opened_at,open_until,late_until) VALUES(?,'x',?,?,?)",
                    (cid, t - 2000, t - 1900, t - 10))

    class _Rows:
        def __init__(self, rows):
            self._rows = rows

        def fetchall(self):
            return self._rows

    class _RaceCon:
        def __init__(self, real):
            self.real, self.fired = real, False

        def execute(self, sql, *a):
            cur = self.real.execute(sql, *a)
            if not self.fired and sql.startswith("SELECT * FROM sessions WHERE closed_at IS NULL AND late_until<"):
                rows = cur.fetchall()
                self.fired = True
                with A.db() as other:  # 另一個輪詢在這個空檔先結案
                    assert A.finalize_expired(other), "前提不成立：另一條連線應該結得到那一場"
                return _Rows(rows)
            return cur

        def __getattr__(self, k):
            return getattr(self.real, k)

    real = A.connect()
    try:
        proxy = _RaceCon(real)
        new_sid = A.do_open_session(proxy, cid, "normal")
        real.commit()
        assert proxy.fired, "前提不成立：沒有攔到 finalize_expired 的 SELECT（SQL 字串改了就要同步這條測試）"
    finally:
        real.close()
    with A.db() as con:
        live = [r["id"] for r in con.execute(
            "SELECT id FROM sessions WHERE course_id=? AND closed_at IS NULL AND late_until>?", (cid, A.now()))]
    assert live == [new_sid], f"搶輸 finalize 之後仍要開得出一場新的點名，實得 {live}"


# 已有未結案場次而種類不同

def test_open_different_kind_while_live_shows_error(h):
    h.open("CD", kind="bind")
    r = h.T.post(f"/t/course/{h.cid['CD']}/open", data={"kind": "normal"}, follow_redirects=False)
    assert r.status_code == 409, r.text
    # 訊息改成依方向給不同建議動作，不再用內部旗標名「綁定場次」或含糊的「先結束」；
    # 綁定場次還在進行、想開一般點名時，改叫老師去那一場按「結束點名」。
    assert "只做綁定" in r.text and "結束點名" in r.text, r.text
    with h.A.db() as con:
        rows = [(row["id"], row["kind"]) for row in con.execute(
            "SELECT id, kind FROM sessions WHERE course_id=? AND closed_at IS NULL", (h.cid["CD"],))]
    assert len(rows) == 1, f"不該新建也不該沿用，資料庫應仍只有那一場綁定場次：{rows}"


def test_open_same_kind_while_live_reuses_session(h):
    """對照組：種類相同照舊沿用（連點防護）。"""
    sid1 = h.open("CD", kind="bind")
    r = h.T.post(f"/t/course/{h.cid['CD']}/open", data={"kind": "bind"}, follow_redirects=False)
    assert r.status_code == 303, r.text
    sid2 = int(r.headers["location"].rsplit("/", 1)[1])
    assert sid1 == sid2


# 整批更正拿掉「缺席」

def test_bulk_status_rejects_absent(h):
    sid = h.open("CD")
    h.T.post(f"/api/t/session/{sid}/close")
    r = h.T.post(f"/api/t/session/{sid}/bulk-status", data={"to": "absent"})
    assert r.status_code == 400, r.text


def test_bulk_status_still_accepts_present_late_excused(h):
    sid = h.open("CD")
    h.T.post(f"/api/t/session/{sid}/close")
    for to in ("present", "late", "excused"):
        r = h.T.post(f"/api/t/session/{sid}/bulk-status", data={"to": to})
        assert r.status_code == 200, (to, r.text)


# 綁定場次的首綁開關顯示

def test_state_forces_allow_bind_true_for_bind_session(h):
    h.T.post(f"/api/t/course/{h.cid['CD']}/bind", data={"allow": "0"})
    sid = h.open("CD", kind="bind")
    st = h.state(sid)
    assert st["allow_bind"] is True, st
    assert st["bind_forced"] is True, st


def test_state_bind_forced_false_for_normal_session(h):
    sid = h.open("CD")
    st = h.state(sid)
    assert st["bind_forced"] is False, st


# bind_minutes 進設定頁

def test_save_settings_accepts_bind_minutes(h):
    r = h.T.post(f"/t/course/{h.cid['CD']}/settings", data={
        "open_minutes": 3, "late_minutes": 15, "qr_interval": 10, "qr_grace": 30,
        "spot_n": 3, "bind_minutes": 12}, follow_redirects=False)
    assert r.status_code == 303, r.text
    with h.A.db() as con:
        c = h.A.get_course(con, h.cid["CD"])
    assert h.A.course_settings(c)["bind_minutes"] == 12


def test_save_settings_clamps_bind_minutes_to_3_30(h):
    r = h.T.post(f"/t/course/{h.cid['CD']}/settings", data={
        "open_minutes": 3, "late_minutes": 15, "qr_interval": 10, "qr_grace": 30,
        "spot_n": 3, "bind_minutes": 999}, follow_redirects=False)
    assert r.status_code == 303, r.text
    with h.A.db() as con:
        c = h.A.get_course(con, h.cid["CD"])
    assert h.A.course_settings(c)["bind_minutes"] == 30


def test_students_page_has_bind_minutes_field(h):
    resp = h.T.get(f"/t/course/{h.cid['CD']}/students")
    assert 'name="bind_minutes"' in resp.text, resp.text
    assert 'min="3"' in resp.text and 'max="30"' in resp.text, resp.text


# 說明文字

def test_home_page_mentions_bind_first_step_and_exceptions(h):
    resp = h.T.get("/t")
    assert "只做綁定" in resp.text
    assert "第一週留五分鐘" not in resp.text, "舊句子應被改掉"
    assert "抽點" in resp.text and "結束點名" in resp.text and "Discord" in resp.text


def test_report_page_mentions_excluded_sessions(h):
    resp = h.T.get(f"/t/course/{h.cid['CD']}/report")
    assert "不含綁定場次與已作廢場次" in resp.text, resp.text


# 多數人不走教室網路時，靜音 ip_differs


def test_majority_off_campus_mutes_ip_differs(h):
    """3 人全走各自行動網路（都跟教室 IP 不同）→ state 沒有 ip_differs、ip_check_muted 為真，
    但資料庫裡原始旗標仍在。"""
    sid = h.open("CD")
    h.T.post(f"/api/t/session/{sid}/teacher-ip", headers={"cf-connecting-ip": "140.131.1.1"})
    for i, stu in enumerate(("9994A001", "9994A002", "9994A003")):
        h.checkin(sid, f"cs6-off-{i}", stu, name=f"學生{i+1}", pin="1234", ip=f"10.0.0.{i+1}")
    st = h.state(sid)
    assert st["ip_check_muted"] is True, st
    for row in st["rows"]:
        assert "ip_differs" not in row["flags"], row
    with h.A.db() as con:
        db_rows = con.execute(
            "SELECT st.student_id, k.flags FROM checkins k JOIN students st ON st.id=k.student_pk WHERE k.session_id=?",
            (sid,)).fetchall()
    assert all("ip_differs" in json.loads(r["flags"]) for r in db_rows), [dict(r) for r in db_rows]


def test_minority_off_campus_still_shows_flag(h):
    """5 人裡 4 人同教室 IP、1 人不同 → 那 1 人照常顯示 ip_differs，其餘不受影響。"""
    with h.A.db() as con:
        con.execute("INSERT INTO students(course_id,student_id,name,cls) VALUES(?,?,?,?)",
                    (h.cid["CD"], "9994A005", "學生5", "創科一甲"))
    sid = h.open("CD")
    h.T.post(f"/api/t/session/{sid}/teacher-ip", headers={"cf-connecting-ip": "140.131.1.1"})
    for i, stu in enumerate(("9994A001", "9994A002", "9994A003", "9994A004")):
        h.checkin(sid, f"cs6-on-{i}", stu, name=f"學生{i+1}", pin="1234", ip="140.131.1.1")
    h.checkin(sid, "cs6-off", "9994A005", name="學生5", pin="1234", ip="10.0.0.9")
    st = h.state(sid)
    assert st["ip_check_muted"] is False, st
    row5 = next(r for r in st["rows"] if r["student_id"] == "9994A005")
    assert "ip_differs" in row5["flags"], row5
    for stu in ("9994A001", "9994A002", "9994A003", "9994A004"):
        row = next(r for r in st["rows"] if r["student_id"] == stu)
        assert "ip_differs" not in row["flags"], row


# 出席不到一半的提示與 Discord 脫鉤

def test_low_rate_true_without_discord(h):
    """沒設 Discord 的課，開場後沒人簽、讓遲到窗過期：low_rate 真、discord_held 假。"""
    sid = h.open("CD")
    with h.A.db() as con:
        s = h.A.get_session(con, sid)
        with time_machine.travel(s["late_until"] + 1, tick=False):
            state = h.state(sid)
    assert state["low_rate"] is True, state
    assert state["discord_held"] is False, state


def test_low_rate_true_with_discord_held(h, monkeypatch):
    """有設 Discord 的課同樣情境：low_rate 真、discord_held 真（discord_post_long 要 mock 掉）。"""
    sent = []
    monkeypatch.setattr(h.A, "discord_post", lambda ch, c: (sent.append((ch, c)) or (True, "HTTP 200")))
    h.A.DISCORD_TOKEN = "x"
    with h.A.db() as con:
        con.execute("UPDATE courses SET settings=? WHERE id=?",
                    (json.dumps({"discord_channel": "123456789012345"}), h.cid["CD"]))
    sid = h.open("CD")
    with h.A.db() as con:
        s = h.A.get_session(con, sid)
        with time_machine.travel(s["late_until"] + 1, tick=False):
            state = h.state(sid)
    assert state["low_rate"] is True, state
    assert state["discord_held"] is True, state
    assert sent == [], f"成功率過低不該自動發：{sent}"


def test_low_rate_false_at_half_and_above(h):
    """出席剛好一半與一半以上：low_rate 假。"""
    sid = h.open("CD")
    h.checkin(sid, "dev3dev1", "9994A001", name="學生1", pin="1234")
    h.checkin(sid, "dev3dev2", "9994A002", name="學生2", pin="1234")  # 2/4 剛好一半
    with h.A.db() as con:
        s = h.A.get_session(con, sid)
        with time_machine.travel(s["late_until"] + 1, tick=False):
            state = h.state(sid)
    assert state["low_rate"] is False, state


def test_low_rate_false_after_void_true_after_unvoid(h):
    """作廢之後：low_rate 假；取消作廢：回到真。"""
    sid = h.open("CD")
    with h.A.db() as con:
        s = h.A.get_session(con, sid)
        with time_machine.travel(s["late_until"] + 1, tick=False):
            h.A.finalize_expired(con)
        con.commit()
    r = h.T.post(f"/api/t/session/{sid}/void", data={"void": "1"})
    assert r.status_code == 200, r.text
    assert h.state(sid)["low_rate"] is False
    r2 = h.T.post(f"/api/t/session/{sid}/void", data={"void": "0"})
    assert r2.status_code == 200, r2.text
    assert h.state(sid)["low_rate"] is True


def test_low_rate_false_for_bind_session(h):
    """綁定場次結束後：low_rate 假（attendance_rate 本來就回 None）。"""
    sid = h.open("CD", kind="bind")
    with h.A.db() as con:
        s = h.A.get_session(con, sid)
        with time_machine.travel(s["late_until"] + 1, tick=False):
            state = h.state(sid)
    assert state["low_rate"] is False, state


def test_low_rate_true_after_manual_close(h):
    """老師手動按結束（不是自動結案）且出席不到一半：low_rate 真。"""
    sid = h.open("CD")
    r = h.T.post(f"/api/t/session/{sid}/close")
    assert r.status_code == 200, r.text
    state = h.state(sid)
    assert state["low_rate"] is True, state


def test_home_page_flags_low_attendance_closed_session(h):
    """老師首頁：低出席的已結束場次那列有「出席不到一半」，正常場次沒有。"""
    sid_low = h.open("CD")
    h.T.post(f"/api/t/session/{sid_low}/close")  # 0/4，低於一半
    sid_ok = h.open("CD")
    for i, stu in enumerate(("9994A001", "9994A002", "9994A003")):
        h.checkin(sid_ok, f"dev3home{i}", stu, name=f"學生{i+1}", pin="1234")
    h.T.post(f"/api/t/session/{sid_ok}/close")  # 3/4，高於一半
    resp = h.T.get("/t")
    # 老師首頁常見問題新增一句同樣含「出席不到一半」的說明文字（規格逐字要求），
    # 純文字子字串比對會把那句也算進去；改成比對每列那顆標記本身的 markup，才是這條測試真正要驗的東西。
    assert resp.text.count('<span class="tag flag">出席不到一半</span>') == 1, resp.text


def test_home_page_no_tag_for_live_voided_or_bind_sessions(h):
    """進行中、作廢、綁定場次都不該出現「出席不到一半」（即使出席率低）。"""
    h.open("CD")  # 進行中，一般場次，0/4 但還沒結束
    sid_void = h.open("DT")
    h.T.post(f"/api/t/session/{sid_void}/close")  # 0/2，低於一半但接著作廢
    h.T.post(f"/api/t/session/{sid_void}/void", data={"void": "1"})
    sid_bind = h.open("OOP", kind="bind")
    h.T.post(f"/api/t/session/{sid_bind}/close")  # 綁定場次，不計出缺勤
    resp = h.T.get("/t")
    # 同上，比對標記本身而不是整頁子字串（常見問題那句也含這四個字，是預期的）。
    assert '<span class="tag flag">出席不到一半</span>' not in resp.text, resp.text


def test_session_html_has_lowmsg_id_and_reads_state(h):
    resp = h.T.get(f"/t/session/{h.open('CD')}")
    assert 'id="lowMsg"' in resp.text, resp.text
    assert "state.low_rate" in resp.text, resp.text


# 老師頁看得到投影頁有沒有在取碼

def test_projector_age_none_before_first_qr_pull(h):
    sid = h.open("CD")
    state = h.state(sid)
    assert state["projector_age"] is None, state


def test_projector_age_after_qr_pull_and_after_30s(h):
    """只有 `have` 對得上目前（或前兩格）碼時才算「投影頁真的收到回應」，
    所以第一次不帶 have 的取碼（前端也是這樣：畫面上還沒有碼）不更新 projector_age。
    這裡用「先取碼拿到 code，再帶那個 code 回來問一次」模擬投影頁真實的下一輪輪詢，
    這才是「連線正常」該被判定成立的情境。"""
    sid = h.open("CD")
    with time_machine.travel(h.A.now(), tick=False) as trav:
        r0 = h.T.get(f"/api/t/session/{sid}/qr")
        assert r0.status_code == 200
        code = r0.json()["code"]
        state0 = h.state(sid)
        assert state0["projector_age"] is None, state0  # 第一次沒有 have，不算數
        r = h.T.get(f"/api/t/session/{sid}/qr", params={"have": code})
        assert r.status_code == 200
        state = h.state(sid)
        assert state["projector_age"] in (0, 1), state
        trav.shift(30)
        state2 = h.state(sid)
        assert abs(state2["projector_age"] - 30) <= 1, state2


def test_projector_age_isolated_per_session(h):
    """別場的 qr 不影響這場。"""
    sid1 = h.open("CD")
    sid2 = h.open("DT")
    h.T.get(f"/api/t/session/{sid1}/qr")
    state2 = h.state(sid2)
    assert state2["projector_age"] is None, state2


def test_unauthenticated_qr_does_not_update_projector_seen(h, A):
    """未登入打 qr（401）不會更新紀錄。"""
    from fastapi.testclient import TestClient
    sid = h.open("CD")
    anon = TestClient(A.app, base_url="http://test")
    r = anon.get(f"/api/t/session/{sid}/qr")
    assert r.status_code == 401
    state = h.state(sid)
    assert state["projector_age"] is None, state


def test_session_html_has_projmsg_id(h):
    resp = h.T.get(f"/t/session/{h.open('CD')}")
    assert 'id="projMsg"' in resp.text, resp.text


# api_qr 回傳 grace

def test_api_qr_returns_grace(h):
    sid = h.open("CD")
    r = h.T.get(f"/api/t/session/{sid}/qr")
    assert r.status_code == 200, r.text
    assert r.json()["grace"] == h.A.DEFAULT_SETTINGS["qr_grace"], r.json()


# 已綁手機的學生不能被騙去「設 PIN」

def test_pin_unchanged_flag_when_bound_device_sends_first_bind_form(h):
    sid0 = h.open("CD")
    h.checkin(sid0, "dev4dev", "9994A001", name="學生1", pin="1111")  # 首綁
    h.T.post(f"/api/t/session/{sid0}/close")
    with h.A.db() as con:
        before = dict(con.execute(
            "SELECT pin_hash, pin_salt FROM students WHERE student_id='9994A001' AND course_id=?",
            (h.cid["CD"],)).fetchone())
    sid = h.open("CD")
    dev = h.dev("dev4dev")
    grant = h.grant(sid, dev)
    r = dev.post("/api/checkin", data={"grant": grant, "student_id": "9994A001", "name": "學生1",
                                       "pin": "9999", "pin2": "9999"})
    j = r.json()
    assert j["ok"] is True, j
    assert j.get("pin_unchanged") is True, j
    with h.A.db() as con:
        after = dict(con.execute(
            "SELECT pin_hash, pin_salt FROM students WHERE student_id='9994A001' AND course_id=?",
            (h.cid["CD"],)).fetchone())
    assert after == before, (before, after)


def test_pin_unchanged_absent_for_genuine_first_bind(h):
    sid = h.open("CD")
    j = h.checkin(sid, "dev4fresh", "9994A002", name="學生2", pin="2222")
    assert j["ok"] is True, j
    assert "pin_unchanged" not in j, j


# 已經簽到的人重送，一律先回收據

def test_resend_after_grant_expired_returns_receipt(h):
    """首送成功後過 181 秒重送→200 again。"""
    sid = h.open("CD")
    r1 = h.checkin(sid, "dev5dev1", "9994A001", name="學生1", pin="1234")
    assert r1["ok"] is True and r1.get("again") is not True, r1
    dev = h.dev("dev5dev1")
    grant = h.grant(sid, dev)
    future = h.A.now() + h.A.DEFAULT_SETTINGS["grant_seconds"] + 1
    with time_machine.travel(future, tick=False):
        r = dev.post("/api/checkin", data={"grant": grant, "student_id": "9994A001", "pin": "1234"})
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["ok"] is True and j["again"] is True, j
    assert j["student_id"] == "9994A001", j


def test_resend_after_teacher_closes_session_returns_receipt(h):
    """老師結束後重送→200 again。"""
    sid = h.open("CD")
    r1 = h.checkin(sid, "dev5dev2", "9994A002", name="學生2", pin="2222")
    assert r1["ok"] is True, r1
    dev = h.dev("dev5dev2")
    grant = h.grant(sid, dev)  # 憑證此時仍未過期
    h.T.post(f"/api/t/session/{sid}/close")
    r = dev.post("/api/checkin", data={"grant": grant, "student_id": "9994A002", "pin": "2222"})
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["ok"] is True and j["again"] is True, j


def test_different_device_with_expired_grant_still_410(h):
    """別支裝置拿同一學號與過期憑證→仍 410。"""
    sid = h.open("CD")
    h.checkin(sid, "dev5dev3", "9994A003", name="學生3", pin="3333")
    dev_owner = h.dev("dev5dev3")
    grant = h.grant(sid, dev_owner)
    other_dev = h.dev("dev5dev3-intruder")
    future = h.A.now() + h.A.DEFAULT_SETTINGS["grant_seconds"] + 1
    with time_machine.travel(future, tick=False):
        r = other_dev.post("/api/checkin", data={"grant": grant, "student_id": "9994A003", "pin": "3333"})
    assert r.status_code == 410, r.text


def test_same_device_different_student_expired_grant_still_410(h):
    """同裝置換成別的學號→仍 410。"""
    sid = h.open("CD")
    h.checkin(sid, "dev5dev4", "9994A001", name="學生1", pin="4444")
    dev = h.dev("dev5dev4")
    grant = h.grant(sid, dev)
    future = h.A.now() + h.A.DEFAULT_SETTINGS["grant_seconds"] + 1
    with time_machine.travel(future, tick=False):
        r = dev.post("/api/checkin", data={"grant": grant, "student_id": "9994A002", "pin": "0000"})
    assert r.status_code == 410, r.text


def test_never_checked_in_student_expired_grant_still_410(h):
    """從沒簽過的人過期→仍 410。"""
    sid = h.open("CD")
    dev = h.dev("dev5dev5")
    grant = h.grant(sid, dev)
    future = h.A.now() + h.A.DEFAULT_SETTINGS["grant_seconds"] + 1
    with time_machine.travel(future, tick=False):
        r = dev.post("/api/checkin", data={"grant": grant, "student_id": "9994A004", "pin": "5555", "pin2": "5555"})
    assert r.status_code == 410, r.text


# 手動結束也套用暫緩發 Discord

def _set_discord_channel(h, course="CD", channel="123456789012345"):
    with h.A.db() as con:
        con.execute("UPDATE courses SET settings=? WHERE id=?", (json.dumps({"discord_channel": channel}), h.cid[course]))


def test_manual_close_zero_checkins_holds_and_does_not_send(h, monkeypatch):
    sent = []
    monkeypatch.setattr(h.A, "discord_post", lambda ch, c: (sent.append((ch, c)) or (True, "HTTP 200")))
    h.A.DISCORD_TOKEN = "x"
    _set_discord_channel(h)
    sid = h.open("CD")  # CD 4 人，0 人簽到
    r = h.T.post(f"/api/t/session/{sid}/close").json()
    assert r["held"] is True and r["discord"] is False, r
    time.sleep(0.2)  # 確認真的沒發，不是背景執行緒還沒跑完
    assert sent == [], f"出席不到一半不該發：{sent}"
    with h.A.db() as con:
        row = con.execute("SELECT note FROM sessions WHERE id=?", (sid,)).fetchone()
    assert row["note"] == "discord_held", row["note"]
    assert h.state(sid)["discord_held"] is True


def test_manual_close_over_half_checkins_sends_once(h, monkeypatch):
    sent = []
    monkeypatch.setattr(h.A, "discord_post", lambda ch, c: (sent.append((ch, c)) or (True, "HTTP 200")))
    h.A.DISCORD_TOKEN = "x"
    _set_discord_channel(h)
    sid = h.open("CD")
    for i, dev in enumerate(["dev7-m1", "dev7-m2", "dev7-m3"]):  # 3/4 出席，過半
        h.checkin(sid, dev, f"9994A00{i+1}", name=f"學生{i+1}", pin="1234")
    r = h.T.post(f"/api/t/session/{sid}/close").json()
    assert r["discord"] is True and "held" not in r, r
    for _ in range(50):
        if sent:
            break
        time.sleep(0.05)
    assert len(sent) == 1, sent
    with h.A.db() as con:
        row = con.execute("SELECT note FROM sessions WHERE id=?", (sid,)).fetchone()
    assert row["note"] != "discord_held"


def test_manual_close_no_channel_does_not_hold(h, monkeypatch):
    """沒頻道 → 不發、note 空（low_rate 仍照常顯示，跟 held 是兩回事）。"""
    sent = []
    monkeypatch.setattr(h.A, "discord_post", lambda ch, c: (sent.append((ch, c)) or (True, "HTTP 200")))
    h.A.DISCORD_TOKEN = "x"
    sid = h.open("CD")  # 沒設 discord_channel
    r = h.T.post(f"/api/t/session/{sid}/close").json()
    assert r["discord"] is False and "held" not in r, r
    assert sent == []
    with h.A.db() as con:
        row = con.execute("SELECT note FROM sessions WHERE id=?", (sid,)).fetchone()
    assert row["note"] != "discord_held"
    assert h.state(sid)["low_rate"] is True, "出席不到一半的提示不受這支影響，照樣顯示"


def test_bind_session_manual_close_does_not_hold_or_send(h, monkeypatch):
    """綁定場次手動結束：不發、不寫 held（本來就不計出缺勤，跟暫緩門檻無關）。"""
    sent = []
    monkeypatch.setattr(h.A, "discord_post", lambda ch, c: (sent.append((ch, c)) or (True, "HTTP 200")))
    h.A.DISCORD_TOKEN = "x"
    _set_discord_channel(h)
    sid = h.open("CD", kind="bind")
    r = h.T.post(f"/api/t/session/{sid}/close").json()
    assert r == {"ok": True, "discord": False}, r
    with h.A.db() as con:
        row = con.execute("SELECT note FROM sessions WHERE id=?", (sid,)).fetchone()
    assert row["note"] != "discord_held"


def test_and_finalize_expired_share_should_hold_discord(h, monkeypatch):
    """api_close 與 finalize_expired 共用 should_hold_discord，兩處判斷要一致。"""
    monkeypatch.setattr(h.A, "discord_post", lambda ch, c: (True, "HTTP 200"))
    h.A.DISCORD_TOKEN = "x"
    _set_discord_channel(h)
    sid = h.open("CD")
    with h.A.db() as con:
        s = h.A.get_session(con, sid)
        held_by_finalize = h.A.should_hold_discord(con, s, "123456789012345")
    assert held_by_finalize is True, "0 人簽到時 should_hold_discord 應該為真"


# 點名頁「這場先不要自動發 Discord」

def test_hold_progressing_marks_note_and_state(h, monkeypatch):
    """進行中按 hold=1：note 立刻變成 discord_held_manual，state 的 discord_held／discord_held_manual 皆真——
    不必等結案，因為老師是在點名進行中就決定要暫緩。"""
    sent = []
    monkeypatch.setattr(h.A, "discord_post", lambda ch, c: (sent.append((ch, c)) or (True, "HTTP 200")))
    h.A.DISCORD_TOKEN = "x"
    _set_discord_channel(h)
    sid = h.open("CD")
    for i, dev in enumerate(["dev15-m1", "dev15-m2", "dev15-m3"]):  # 3/4 出席，過半——刻意讓自動判斷會說「不用暫緩」
        h.checkin(sid, dev, f"9994A00{i+1}", name=f"學生{i+1}", pin="1234")
    r = h.T.post(f"/api/t/session/{sid}/hold", data={"hold": "1"})
    assert r.status_code == 200 and r.json() == {"ok": True, "held": True}, r.text
    with h.A.db() as con:
        row = con.execute("SELECT note FROM sessions WHERE id=?", (sid,)).fetchone()
    assert row["note"] == "discord_held_manual", row["note"]
    state = h.state(sid)
    assert state["discord_held"] is True and state["discord_held_manual"] is True, state


def test_auto_close_does_not_send_when_manually_held(h, monkeypatch):
    """進行中 hold=1（出席過半，出席率不是理由）→ 自動結案不發、note 仍是 discord_held_manual。"""
    sent = []
    monkeypatch.setattr(h.A, "discord_post", lambda ch, c: (sent.append((ch, c)) or (True, "HTTP 200")))
    h.A.DISCORD_TOKEN = "x"
    _set_discord_channel(h)
    sid = h.open("CD")
    for i, dev in enumerate(["dev15a-m1", "dev15a-m2", "dev15a-m3"]):
        h.checkin(sid, dev, f"9994A00{i+1}", name=f"學生{i+1}", pin="1234")
    assert h.T.post(f"/api/t/session/{sid}/hold", data={"hold": "1"}).json()["held"] is True
    with h.A.db() as con:
        s = h.A.get_session(con, sid)
        with time_machine.travel(s["late_until"] + 1, tick=False):
            h.A.finalize_expired(con)
        con.commit()
        row = con.execute("SELECT note FROM sessions WHERE id=?", (sid,)).fetchone()
    time.sleep(0.2)
    assert sent == [], f"手動暫緩，出席率再高也不該自動發：{sent}"
    assert row["note"] == "discord_held_manual", row["note"]
    state = h.state(sid)
    assert state["discord_held"] is True and state["discord_held_manual"] is True, state


def test_void_after_manual_hold_hides_discord_held_and_unvoid_restores(h, monkeypatch):
    """手動暫緩後作廢 → discord_held 假（作廢判斷照舊）；取消作廢 → discord_held 真。"""
    h.A.DISCORD_TOKEN = "x"
    _set_discord_channel(h)
    sid = h.open("CD")
    assert h.T.post(f"/api/t/session/{sid}/hold", data={"hold": "1"}).json()["held"] is True
    h.T.post(f"/api/t/session/{sid}/close")
    assert h.state(sid)["discord_held"] is True
    h.T.post(f"/api/t/session/{sid}/void", data={"void": "1"})
    assert h.state(sid)["discord_held"] is False, "作廢時暫緩提示要跟著隱藏"
    with h.A.db() as con:
        row = con.execute("SELECT note FROM sessions WHERE id=?", (sid,)).fetchone()
    assert row["note"] == "discord_held_manual", "作廢不清 note"
    h.T.post(f"/api/t/session/{sid}/void", data={"void": "0"})
    assert h.state(sid)["discord_held"] is True, "取消作廢後提示要自己回來"


def test_hold_then_unhold_over_half_auto_sends(h, monkeypatch):
    """hold=1 再 hold=0：取消暫緩之後，出席過半，自動結案照發（跟沒按過 hold 一樣）。"""
    sent = []
    monkeypatch.setattr(h.A, "discord_post", lambda ch, c: (sent.append((ch, c)) or (True, "HTTP 200")))
    h.A.DISCORD_TOKEN = "x"
    _set_discord_channel(h)
    sid = h.open("CD")
    for i, dev in enumerate(["dev15b-m1", "dev15b-m2", "dev15b-m3"]):
        h.checkin(sid, dev, f"9994A00{i+1}", name=f"學生{i+1}", pin="1234")
    assert h.T.post(f"/api/t/session/{sid}/hold", data={"hold": "1"}).json()["held"] is True
    assert h.T.post(f"/api/t/session/{sid}/hold", data={"hold": "0"}).json()["held"] is False
    with h.A.db() as con:
        row = con.execute("SELECT note FROM sessions WHERE id=?", (sid,)).fetchone()
    assert row["note"] == "", "hold=0 要把 discord_held_manual 清成空字串"
    with h.A.db() as con:
        s = h.A.get_session(con, sid)
        with time_machine.travel(s["late_until"] + 1, tick=False):
            h.A.finalize_expired(con)
        con.commit()
    for _ in range(50):
        if sent:
            break
        time.sleep(0.05)
    assert len(sent) == 1, sent


def test_hold_then_manual_close_returns_held_true_regardless_of_rate(h, monkeypatch):
    """hold=1 後手動結束：回 held: true、不發——不論出席率（held 語意延伸給手動暫緩）。"""
    sent = []
    monkeypatch.setattr(h.A, "discord_post", lambda ch, c: (sent.append((ch, c)) or (True, "HTTP 200")))
    h.A.DISCORD_TOKEN = "x"
    _set_discord_channel(h)
    sid = h.open("CD")
    for i, dev in enumerate(["dev15c-m1", "dev15c-m2", "dev15c-m3"]):  # 出席過半
        h.checkin(sid, dev, f"9994A00{i+1}", name=f"學生{i+1}", pin="1234")
    assert h.T.post(f"/api/t/session/{sid}/hold", data={"hold": "1"}).json()["held"] is True
    r = h.T.post(f"/api/t/session/{sid}/close").json()
    assert r == {"ok": True, "discord": False, "held": True}, r
    time.sleep(0.2)
    assert sent == [], f"手動暫緩，出席過半也不該發：{sent}"
    with h.A.db() as con:
        row = con.execute("SELECT note FROM sessions WHERE id=?", (sid,)).fetchone()
    assert row["note"] == "discord_held_manual", row["note"]


def test_hold_after_closed_is_409(h):
    sid = h.open("CD")
    h.T.post(f"/api/t/session/{sid}/close")
    r = h.T.post(f"/api/t/session/{sid}/hold", data={"hold": "1"})
    assert r.status_code == 409, r.text
    assert "發到 Discord" in r.json()["error"], r.json()


def test_hold_on_bind_session_is_409(h):
    sid = h.open("CD", kind="bind")
    r = h.T.post(f"/api/t/session/{sid}/hold", data={"hold": "1"})
    assert r.status_code == 409, r.text
    assert "綁定場次" in r.json()["error"], r.json()


def test_hold_without_login_is_401(h):
    r = h.dev("dev15-anon").post("/api/t/session/1/hold", data={"hold": "1"})
    assert r.status_code == 401, r.text


def test_hold_rejects_value_other_than_0_or_1(h):
    """`hold` 表單值只接受 "0"／"1"；其他值（例如手誤打的 "2"、"true"）要回 400，不能悄悄當假值處理。"""
    sid = h.open("CD")
    r = h.T.post(f"/api/t/session/{sid}/hold", data={"hold": "2"})
    assert r.status_code == 400, r.text
    assert "hold 參數不合法" in r.json()["error"], r.json()


def test_manual_notify_clears_both_kinds_of_held(h, monkeypatch):
    """手動「發到 Discord」成功照舊清掉暫緩，兩種暫緩都要清——這裡驗手動暫緩那種。"""
    sent = []
    monkeypatch.setattr(h.A, "discord_post", lambda ch, c: (sent.append((ch, c)) or (True, "HTTP 200")))
    h.A.DISCORD_TOKEN = "x"
    _set_discord_channel(h)
    sid = h.open("CD")
    assert h.T.post(f"/api/t/session/{sid}/hold", data={"hold": "1"}).json()["held"] is True
    h.T.post(f"/api/t/session/{sid}/close")
    assert h.state(sid)["discord_held_manual"] is True
    r = h.T.post(f"/api/t/session/{sid}/notify")
    assert r.status_code == 200 and len(sent) >= 1
    assert h.state(sid)["discord_held"] is False and h.state(sid)["discord_held_manual"] is False


# 背景發 Discord 失敗要留下痕跡、讓老師看得到

def _wait_for_note(h, sid, want, timeout=3.0):
    """輪詢真正的資料庫狀態，不是猜背景執行緒跑多久——最多等 timeout 秒。"""
    deadline = time.time() + timeout
    row = None
    while time.time() < deadline:
        with h.A.db() as con:
            row = con.execute("SELECT note FROM sessions WHERE id=?", (sid,)).fetchone()
        if row["note"] == want:
            return row
        time.sleep(0.05)
    return row


def test_manual_close_discord_failure_records_note_and_event(h, monkeypatch):
    """手動結束（api_close）背景發送失敗：note 要變成 discord_failed、留一筆 discord_fail 事件、
    api_state 的 discord_failed 要真——現況這些全部悶聲丟掉，老師以為發了其實沒發。"""
    monkeypatch.setattr(h.A, "discord_post_long", lambda ch, c: (False, "HTTP 403"))
    _set_discord_channel(h)
    sid = h.open("CD")
    for i, dev in enumerate(["dev16m1", "dev16m2", "dev16m3"]):
        h.checkin(sid, dev, f"9994A00{i+1}", name=f"學生{i+1}", pin="1234")  # 3/4，高於門檻，不會 held
    assert h.T.post(f"/api/t/session/{sid}/close").json()["ok"] is True
    row = _wait_for_note(h, sid, "discord_failed")
    assert row["note"] == "discord_failed", row["note"]
    with h.A.db() as con:
        ev = con.execute("SELECT COUNT(*) n FROM events WHERE session_id=? AND kind='discord_fail'", (sid,)).fetchone()
    assert ev["n"] == 1, "應該有一筆 discord_fail 事件"
    assert h.state(sid)["discord_failed"] is True


def test_auto_finalize_discord_failure_records_note_and_event(h, monkeypatch):
    """自動結案（finalize_expired）背景發送失敗，行為要跟手動結束一致。"""
    monkeypatch.setattr(h.A, "discord_post_long", lambda ch, c: (False, "HTTP 403"))
    _set_discord_channel(h)
    sid = h.open("CD")
    for i, dev in enumerate(["dev16a1", "dev16a2", "dev16a3"]):
        h.checkin(sid, dev, f"9994A00{i+1}", name=f"學生{i+1}", pin="1234")
    with h.A.db() as con:
        s = h.A.get_session(con, sid)
        with time_machine.travel(s["late_until"] + 1, tick=False):
            h.A.finalize_expired(con)
        con.commit()
    row = _wait_for_note(h, sid, "discord_failed")
    assert row["note"] == "discord_failed", row["note"]
    with h.A.db() as con:
        ev = con.execute("SELECT COUNT(*) n FROM events WHERE session_id=? AND kind='discord_fail'", (sid,)).fetchone()
    assert ev["n"] == 1
    assert h.state(sid)["discord_failed"] is True


def test_bg_discord_post_does_not_overwrite_held_note(h, monkeypatch):
    """note 已經是 discord_held（或 discord_held_manual）就不能被蓋成 discord_failed——
    背景發送失敗只在 note 還是空字串時才寫；直接呼叫 _bg_discord_post 驗這道防呆本身。"""
    monkeypatch.setattr(h.A, "discord_post_long", lambda ch, c: (False, "HTTP 403"))
    sid = h.open("CD")
    with h.A.db() as con:
        con.execute("UPDATE sessions SET note='discord_held' WHERE id=?", (sid,))
    h.A._bg_discord_post(sid, "123456789012345", "測試內容")
    with h.A.db() as con:
        row = con.execute("SELECT note FROM sessions WHERE id=?", (sid,)).fetchone()
    assert row["note"] == "discord_held", "原本是 discord_held 的場次不該被蓋掉"


def test_bg_discord_post_swallows_exception_from_discord_post_long(h, monkeypatch, capsys):
    """`discord_post_long` 不是回傳 (False, msg)，而是直接丟例外（例如 requests 逾時）——
    `_bg_discord_post` 的 `except Exception` 分支要接住它，行為要跟回傳 False 一致：
    stderr 留一行、記 discord_fail 事件、note 變 discord_failed，不能讓背景執行緒把例外整條吞掉
    導致連 stderr 都看不到。"""
    def boom(ch, c):
        raise RuntimeError("connection reset")
    monkeypatch.setattr(h.A, "discord_post_long", boom)
    _set_discord_channel(h)
    sid = h.open("CD")
    for i, dev in enumerate(["dev16e1", "dev16e2", "dev16e3"]):
        h.checkin(sid, dev, f"9994A00{i+1}", name=f"學生{i+1}", pin="1234")
    assert h.T.post(f"/api/t/session/{sid}/close").json()["ok"] is True
    row = _wait_for_note(h, sid, "discord_failed")
    assert row["note"] == "discord_failed", row["note"]
    with h.A.db() as con:
        ev = con.execute("SELECT reason FROM events WHERE session_id=? AND kind='discord_fail'", (sid,)).fetchone()
    assert ev is not None, "應該有一筆 discord_fail 事件"
    # 事件（老師頁紅字的來源）只放中文訊息加例外類別，不放英文原文；原文只留在 stderr
    assert ev["reason"] == "Discord 發送失敗（RuntimeError）" and "connection reset" not in ev["reason"], ev["reason"]
    assert "connection reset" in capsys.readouterr().err, "例外要印一行 stderr，不能悄悄吞掉"


def test_manual_notify_clears_discord_failed(h, monkeypatch):
    """之後手動發送成功：note 要清空、discord_failed 要變假（清暫緩的那行也要涵蓋這一種）。"""
    monkeypatch.setattr(h.A, "discord_post_long", lambda ch, c: (False, "HTTP 403"))
    _set_discord_channel(h)
    sid = h.open("CD")
    for i, dev in enumerate(["dev16n1", "dev16n2", "dev16n3"]):
        h.checkin(sid, dev, f"9994A00{i+1}", name=f"學生{i+1}", pin="1234")
    h.T.post(f"/api/t/session/{sid}/close")
    _wait_for_note(h, sid, "discord_failed")
    assert h.state(sid)["discord_failed"] is True
    monkeypatch.setattr(h.A, "discord_post_long", lambda ch, c: (True, "OK"))
    r = h.T.post(f"/api/t/session/{sid}/notify")
    assert r.status_code == 200, r.text
    with h.A.db() as con:
        row = con.execute("SELECT note FROM sessions WHERE id=?", (sid,)).fetchone()
    assert row["note"] == "", row["note"]
    assert h.state(sid)["discord_failed"] is False


def test_session_html_shows_discord_failed_red_text(h):
    """老師頁要接 discord_failed，才顯示得出來（Playwright 另外驗真的顯示紅字）。"""
    from pathlib import Path
    html = (Path(h.A.__file__).parent / "templates" / "session.html").read_text()
    assert "state.discord_failed" in html, "老師頁沒有接 discord_failed"
    assert "沒有成功" in html, "老師頁沒有發送失敗的說明文字"


# 手動發 Discord 失敗不要回 502（Cloudflare 會換成自己的錯誤頁）

def test_manual_notify_failure_is_424_not_502(h, monkeypatch):
    """手動「發到 Discord」失敗要回 424，不是 502——Cloudflare 對 origin 的 5xx 可能換成自己的
    錯誤頁，老師只看得到「伺服器忙線（HTTP 502），再按一次」，看不到真正的失敗原因。"""
    monkeypatch.setattr(h.A, "discord_post_long", lambda ch, c: (False, "HTTP 403"))
    _set_discord_channel(h)
    sid = h.open("CD")
    h.T.post(f"/api/t/session/{sid}/close")
    r = h.T.post(f"/api/t/session/{sid}/notify")
    assert r.status_code == 424, r.text
    assert "Discord 發送失敗" in r.json()["error"], r.text


# 手動「發到 Discord」不可重複發

def _closed_session_with_channel(h, monkeypatch, sent, delay=0.0):
    def slow_post(ch, content):
        if delay:
            time.sleep(delay)
        sent.append((ch, content))
        return True, "OK"
    monkeypatch.setattr(h.A, "discord_post_long", slow_post)
    _set_discord_channel(h)
    sid = h.open("CD")
    h.checkin(sid, "dev6-owner", "9994A001", name="學生1", pin="1234")
    h.T.post(f"/api/t/session/{sid}/close")
    return sid


def test_parallel_notify_only_one_actually_sends(h, monkeypatch):
    sent = []
    sid = _closed_session_with_channel(h, monkeypatch, sent, delay=1.0)
    results = {}
    barrier = threading.Barrier(2)

    def go(tag):
        barrier.wait()
        results[tag] = h.T.post(f"/api/t/session/{sid}/notify")

    t1 = threading.Thread(target=go, args=("A",))
    t2 = threading.Thread(target=go, args=("B",))
    t1.start(); t2.start(); t1.join(10); t2.join(10)
    codes = sorted(r.status_code for r in results.values())
    assert codes == [200, 409], [(k, r.status_code, r.text) for k, r in results.items()]
    assert len(sent) == 1, f"發送函式應該只被呼叫一輪：{sent}"


def test_second_call_within_120s_is_again_not_resent(h, monkeypatch):
    sent = []
    sid = _closed_session_with_channel(h, monkeypatch, sent)
    r1 = h.T.post(f"/api/t/session/{sid}/notify")
    assert r1.status_code == 200 and r1.json() == {"ok": True}
    assert len(sent) == 1
    r2 = h.T.post(f"/api/t/session/{sid}/notify")
    assert r2.status_code == 200 and r2.json() == {"ok": True, "again": True}
    assert len(sent) == 1, "120 秒內第二次不該真的再發一次"


def test_after_121s_sends_again(h, monkeypatch):
    sent = []
    sid = _closed_session_with_channel(h, monkeypatch, sent)
    r1 = h.T.post(f"/api/t/session/{sid}/notify")
    assert r1.status_code == 200
    assert len(sent) == 1
    with time_machine.travel(h.A.now(), tick=False) as trav:
        trav.shift(121)
        r2 = h.T.post(f"/api/t/session/{sid}/notify")
    assert r2.status_code == 200 and r2.json() == {"ok": True}
    assert len(sent) == 2, "過了 120 秒重按應該真的再發一次"


# 「是否正在發送」與「設成發送中」之間沒有鎖，兩個請求可能同時通過檢查
def test_notify_check_and_set_is_atomic(h, monkeypatch):
    """讓 _notify_state.get 讀完之後讓出執行緒（模擬在檢查與設定之間被切走）：兩個並行請求仍只能有一個真的發送。"""
    sent = []
    sid = _closed_session_with_channel(h, monkeypatch, sent, delay=0.5)

    class SlowGet(dict):
        def get(self, *a, **k):
            v = super().get(*a, **k)
            time.sleep(0.2)
            return v
    monkeypatch.setattr(h.A, "_notify_state", SlowGet())
    results = {}
    barrier = threading.Barrier(2)

    def go(tag):
        barrier.wait()
        results[tag] = h.T.post(f"/api/t/session/{sid}/notify")

    t1 = threading.Thread(target=go, args=("A",))
    t2 = threading.Thread(target=go, args=("B",))
    t1.start(); t2.start(); t1.join(10); t2.join(10)
    codes = sorted(r.status_code for r in results.values())
    assert codes == [200, 409], [(k, r.status_code, r.text) for k, r in results.items()]
    assert len(sent) == 1, f"發送函式應該只被呼叫一輪：{len(sent)} 輪"


# 120 秒內只擋「內容相同」的重發；老師發完發現名單有錯、改完再發要真的發
def test_notify_resends_when_content_changed_within_120s(h, monkeypatch):
    sent = []
    sid = _closed_session_with_channel(h, monkeypatch, sent)
    assert h.T.post(f"/api/t/session/{sid}/notify").json() == {"ok": True}
    pk = h.pk("CD", "9994A002")
    assert h.T.post(f"/api/t/session/{sid}/status", data={"pk": pk, "status": "excused"}).json()["ok"]
    r = h.T.post(f"/api/t/session/{sid}/notify")
    assert r.status_code == 200 and r.json() == {"ok": True}, r.text
    assert len(sent) == 2 and sent[0][1] != sent[1][1], "名單改過之後重按要真的發出新的內容"


# 兩則會誤導老師的 409 訊息

def test_extend_after_close_message_suggests_backfill_not_restart(h):
    sid = h.open("CD")
    h.T.post(f"/api/t/session/{sid}/close")
    r = h.T.post(f"/api/t/session/{sid}/extend", data={"minutes": 2})
    assert r.status_code == 409
    msg = r.json()["error"]
    assert "補登" in msg, msg
    assert "重新開始" not in msg, msg


def test_open_mismatch_kind_message_depends_on_direction(h):
    s1 = h.open("CD")  # 一般點名進行中，誤按只做綁定
    r1 = h.T.post(f"/t/course/{h.cid['CD']}/open", data={"kind": "bind"}, follow_redirects=False)
    assert r1.status_code == 409
    assert "刪除此次" in r1.text and "重新開始" not in r1.text
    h.T.post(f"/api/t/session/{s1}/delete")

    bind_sid = h.open("CD", kind="bind")  # 綁定場次進行中，想開一般點名
    r2 = h.T.post(f"/t/course/{h.cid['CD']}/open", data={"kind": "normal"}, follow_redirects=False)
    assert r2.status_code == 409
    assert "結束點名" in r2.text and "重新開始" not in r2.text
    h.T.post(f"/api/t/session/{bind_sid}/close")


# 出席窗最後幾秒按延長，只延長了遲到窗

def test_extend_open_target_recovers_open_window_when_request_arrives_just_late(h):
    sid = h.open("CD")
    with h.A.db() as con:
        s0 = h.A.get_session(con, sid)
    with time_machine.travel(s0["open_until"] - 5, tick=False) as trav:
        trav.shift(6)  # 老師按下當時還在出席窗內，請求（Cloudflare 慢）晚 6 秒才到，伺服器判定已進遲到區
        r = h.T.post(f"/api/t/session/{sid}/extend", data={"minutes": 2, "target": "open"})
        assert r.status_code == 200, r.text
        j = r.json()
        assert j["phase"] == "open" and "note" not in j, j
        with h.A.db() as con2:
            s1 = h.A.get_session(con2, sid)
        assert s1["open_until"] == s0["open_until"] + 120
        trav.shift(30)
        j2 = h.checkin(sid, "dev8-late-scanner", "9994A002", "學生2", pin="1234")
    assert j2["status"] == "出席", j2


def test_extend_open_target_over_60s_late_only_extends_late_with_note(h):
    sid = h.open("CD")
    with h.A.db() as con:
        s0 = h.A.get_session(con, sid)
    with time_machine.travel(s0["open_until"] + 61, tick=False):
        r = h.T.post(f"/api/t/session/{sid}/extend", data={"minutes": 2, "target": "open"})
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["phase"] == "late"
    assert j.get("note") == "出席窗已經關了，這次延長的是遲到窗；晚到的人會記遲到。", j
    with h.A.db() as con:
        s1 = h.A.get_session(con, sid)
    assert s1["open_until"] == s0["open_until"]
    assert s1["late_until"] == s0["late_until"] + 120


def test_extend_without_target_keeps_current_behavior(h):
    sid = h.open("CD")
    forced_open_until = h.A.now() - 1  # 已經進入遲到區
    with h.A.db() as con:
        con.execute("UPDATE sessions SET open_until=? WHERE id=?", (forced_open_until, sid))
    r = h.T.post(f"/api/t/session/{sid}/extend", data={"minutes": 2})
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["phase"] == "late" and "note" not in j, j
    with h.A.db() as con:
        s1 = h.A.get_session(con, sid)
    assert s1["open_until"] == forced_open_until, "不帶 target 時維持現行行為：出席窗不會被拉回來"


# 換場後舊投影頁與舊表單只說「點名已結束」

def test_qr_returns_next_sid_when_course_has_new_live_session(h):
    bind = h.open("CD", kind="bind")
    h.T.post(f"/api/t/session/{bind}/close")
    q0 = h.T.get(f"/api/t/session/{bind}/qr").json()
    assert q0["phase"] == "closed" and q0["next_sid"] is None
    normal = h.open("CD")
    q1 = h.T.get(f"/api/t/session/{bind}/qr").json()
    assert q1["phase"] == "closed" and q1["next_sid"] == normal
    h.T.post(f"/api/t/session/{normal}/close")


def test_stale_form_after_new_session_opened_names_the_reason(h):
    bind = h.open("CD", kind="bind")
    phone = h.dev("dev9-phone")
    grant = h.grant(bind, phone)
    h.T.post(f"/api/t/session/{bind}/close")
    normal = h.open("CD")
    r = phone.post("/api/checkin", data={"grant": grant, "student_id": "9994A004", "name": "學生4",
                                          "pin": "1357", "pin2": "1357", "hints": "h"})
    assert r.status_code == 410
    assert r.json()["error"] == "這一場已經結束，老師開了新的一場，請重新掃描投影幕上的 QR", r.json()
    h.T.post(f"/api/t/session/{normal}/close")


def test_stale_form_without_new_session_keeps_original_message(h):
    """沒有新場次時的訊息（原因碼 closed 不變）。"""
    sid = h.open("CD")
    phone = h.dev("dev9-phone2")
    grant = h.grant(sid, phone)
    h.T.post(f"/api/t/session/{sid}/close")
    r = phone.post("/api/checkin", data={"grant": grant, "student_id": "9994A001", "name": "學生1",
                                          "pin": "1234", "pin2": "1234", "hints": "h"})
    assert r.status_code == 410
    assert r.json()["error"] == "點名已經結束。有到課但沒簽到的同學，請直接找老師。", r.json()


# 剛簽到成功，/me 看不到這一場

def test_live_session_checkin_appears_in_api_me(h):
    sid = h.open("CD")
    phone = h.dev("dev10-phone")
    j = h.checkin(sid, "dev10-phone", "9994A001", name="學生1", pin="1234")
    assert j["ok"]
    me = phone.post("/api/me").json()["courses"]
    c = me[0]  # 這支手機只綁了 CD 這門課的名冊，只會有一筆
    assert c["rate"] is None, c
    live_items = [i for i in c["items"] if i.get("live")]
    assert len(live_items) == 1, c["items"]
    assert live_items[0]["status"] == "present", live_items


# 抽點代簽連坐看按的順序

def _samedev_pair(h, sid, dev, a, b):
    """同一支手機連續簽兩個人：既有代簽偵測邏輯會讓兩人都變 pending。名字要對得上名冊
    （conftest 的 seeded 名冊固定叫「學生」+ 學號末碼），亂填會被 not_match 擋掉。"""
    h.checkin(sid, dev, a, name="學生" + a[-1])
    h.checkin(sid, dev, b, name="學生" + b[-1])


def test_spot_in_after_proxy_cascade_restores_status_and_drops_flag(h):
    """甲被抽點判「不在」，同手機的乙因此連坐（proxy_for+absent）；
    之後對乙自己抽點按「在」，要拿掉 proxy_for、加 spot_ok，狀態還原成他原本簽到當下的狀態。"""
    sid = h.open("CD")
    _samedev_pair(h, sid, "dev11-dev1", "9994A001", "9994A002")
    picks = {}
    for _ in range(6):
        for p in h.T.post(f"/api/t/session/{sid}/spotcheck").json()["picks"]:
            picks[p["student_id"]] = p["id"]
    assert {"9994A001", "9994A002"} <= set(picks), picks
    h.T.post(f"/api/t/spotcheck/{picks['9994A001']}", data={"present": "0"})  # 甲不在 → 乙同手機連坐
    d = h.db_rows(sid)
    assert d["9994A002"]["status"] == "absent" and "proxy_for" in d["9994A002"]["flags"], d["9994A002"]
    h.T.post(f"/api/t/spotcheck/{picks['9994A002']}", data={"present": "1"})  # 乙自己這次抽點按「在」
    d2 = h.db_rows(sid)
    assert d2["9994A002"]["status"] in ("present", "late"), d2["9994A002"]
    assert "proxy_for" not in d2["9994A002"]["flags"], d2["9994A002"]
    assert "spot_ok" in d2["9994A002"]["flags"], d2["9994A002"]


def test_dropdown_restore_after_spot_absent_reports_chained_partner(h):
    """老師用下拉把「抽點不在」的甲改回缺席以外的狀態時，回應要帶 chained，
    列出同一支手機因連坐被記缺席的乙（不自動改他，只是讓老師看得到）。"""
    sid = h.open("CD")
    _samedev_pair(h, sid, "dev11-dev2", "9994A003", "9994A004")
    picks = {}
    for _ in range(6):
        for p in h.T.post(f"/api/t/session/{sid}/spotcheck").json()["picks"]:
            picks[p["student_id"]] = p["id"]
    h.T.post(f"/api/t/spotcheck/{picks['9994A003']}", data={"present": "0"})  # 甲(A003) 抽點不在
    d = h.db_rows(sid)
    assert "spot_absent" in d["9994A003"]["flags"] and "proxy_for" in d["9994A004"]["flags"], d
    pk3 = h.pk("CD", "9994A003")
    r = h.T.post(f"/api/t/session/{sid}/status", data={"pk": pk3, "status": "present"})
    assert r.status_code == 200, r.text
    assert r.json().get("chained") == [{"student_id": "9994A004", "name": "學生4"}], r.json()


# 刪除最新一場後重開，舊表單在新場次有效

def test_reopened_session_rejects_grant_issued_before_reopen(h):
    sid = h.open("CD")
    phone = h.dev("dev12-phone")
    grant = h.grant(sid, phone)
    h.T.post(f"/api/t/session/{sid}/delete")
    sid2 = h.open("CD")
    assert sid2 == sid, "刪除最新一場後重開應該沿用同一個 id（SQLite 沒有 AUTOINCREMENT）"
    r = phone.post("/api/checkin", data={"grant": grant, "student_id": "9994A001", "name": "學生1",
                                          "pin": "1234", "pin2": "1234", "hints": "h"})
    assert r.status_code == 410
    assert r.json()["error"] == "這份簽到表單是之前那一場的，請重新掃描 QR", r.json()
    h.T.post(f"/api/t/session/{sid}/close")


def test_funnel_and_stuck_never_count_events_before_opened_at(h):
    """事件表的舊資料（刪除前留下）不能算進重開後的新場次；用 opened_at 當底線直接驗算。"""
    sid = h.open("CD")
    with h.A.db() as con:
        s = h.A.get_session(con, sid)
        pk = con.execute("SELECT id FROM students WHERE course_id=? AND student_id=?",
                          (h.cid["CD"], "9994A001")).fetchone()["id"]
        con.execute("INSERT INTO events(ts, session_id, kind, reason, device8, ua_class, student_pk) VALUES(?,?,?,?,?,?,?)",
                    (s["opened_at"] - 100, sid, "land_ok", "", "deadbeef", "iOS-Safari", None))
        con.execute("INSERT INTO events(ts, session_id, kind, reason, device8, ua_class, student_pk) VALUES(?,?,?,?,?,?,?)",
                    (s["opened_at"] - 50, sid, "checkin_reject", "not_match", "cafebabe", "iOS-Safari", pk))
        con.execute("INSERT INTO events(ts, session_id, kind, reason, device8, ua_class, student_pk) VALUES(?,?,?,?,?,?,?)",
                    (s["opened_at"] + 1, sid, "land_ok", "", "aaaaaaaa", "iOS-Safari", None))
        con.commit()
        funnel = h.A.compute_funnel(con, sid, s["opened_at"])
        stuck = h.A.compute_stuck(con, sid, s["opened_at"])
    assert funnel["total"]["land_ok"] == 1, funnel
    assert funnel["total"]["reject"] == 0, funnel
    assert stuck == [], stuck


# 整批更正後 low_rate 要跟著消掉

def test_held_message_does_not_quote_current_rate():
    """暫緩提示寫的是「結束當時」的事，整批更正把出席率拉上來之後它仍會留著（要老師手動發），
    所以文案不能再講成「這次出席不到一半」這種描述目前出席率的句子。"""
    import re
    from pathlib import Path
    html = (Path(__file__).parent.parent / "templates" / "session.html").read_text()
    m = re.search(r'<p[^>]*id="heldMsg"[^>]*>(.*?)</p>', html, re.S)
    assert m, "找不到 #heldMsg"
    assert "這一場結束時出席不到一半" in m.group(1), m.group(1)
    assert "這次出席不到一半" not in m.group(1), m.group(1)


def test_low_rate_clears_after_bulk_correction_raises_attendance(h):
    sid = h.open("CD")
    h.checkin(sid, "dev13-only", "9994A001", name="學生1", pin="1234")  # 1/4 出席，低於門檻
    with h.A.db() as con:
        s = h.A.get_session(con, sid)
        with time_machine.travel(s["late_until"] + 1, tick=False):
            h.A.finalize_expired(con)
        con.commit()
    assert h.state(sid)["low_rate"] is True
    r = h.T.post(f"/api/t/session/{sid}/bulk-status", data={"to": "present"})
    assert r.status_code == 200, r.text
    assert h.state(sid)["low_rate"] is False


# 整批更正與逐筆改狀態交錯時蓋掉逐筆改的結果

def test_bulk_status_does_not_clobber_interleaved_manual_change(h, monkeypatch):
    sid = h.open("CD")
    h.T.post(f"/api/t/session/{sid}/close")  # 全班沒人簽到 → 4 筆自動缺席
    pk = h.pk("CD", "9994A002")
    real_loads = h.A.json.loads
    fired = []

    def loads_hook(s, *a, **k):
        if not fired:
            fired.append(1)
            from fastapi.testclient import TestClient
            t2 = TestClient(h.A.app, base_url="http://test")
            t2.cookies = h.T.cookies
            r = t2.post(f"/api/t/session/{sid}/status", data={"pk": pk, "status": "excused"})
            assert r.json()["ok"], r.text
        return real_loads(s, *a, **k)

    monkeypatch.setattr(h.A.json, "loads", loads_hook)
    r = h.T.post(f"/api/t/session/{sid}/bulk-status", data={"to": "present"})
    monkeypatch.setattr(h.A.json, "loads", real_loads)
    assert r.status_code == 200, r.text
    d = h.db_rows(sid)
    assert d["9994A002"]["status"] == "excused", "被搶先改成請假的那一列，整批更正不能蓋回去"
    assert r.json()["n"] == 3, r.json()


# 老師頁靜態文案更新

def test_students_page_has_qr_interval_hint(h):
    html = h.T.get(f"/t/course/{h.cid['CD']}/students").text
    assert "點名進行中不要改：已經顯示的碼會立刻失效。" in html


def test_home_page_faq_text_updated(h):
    html = h.T.get("/t").text
    assert "放行選「出席」（名單標「遲到區簽到」的選「遲到」）" in html
    assert "按一下就完成（不用輸 PIN）" in html
    assert "出席不到一半的場次不會自動發，點名頁會有紅字。" in html
    assert "出席窗還沒關、還有人在簽" in html
    assert "不要重新開始點名（新的一場會把準時的人記缺席）" in html
    assert "出席是在開放窗內掃到 QR（送出晚一點沒關係）" in html


# 第一次來的學生，落地事件記不到裝置

def test_first_time_lander_device_key_used_for_land_ok_and_grant(h):
    """第一次來（沒有裝置 cookie）落地當下 device8 要記得到，且下發的 cookie／grant 用同一把
    key——之後帶著這把 cookie 重新落地、首次綁定簽到，才能吃到 60 秒出席窗。"""
    import re
    with h.A.db() as con:
        con.execute("UPDATE courses SET settings=? WHERE id=?",
                    (json.dumps({"open_minutes": 1, "late_minutes": 15}), h.cid["CD"]))
    sid = h.open("CD")
    with h.A.db() as con:
        s = h.A.get_session(con, sid)
        c = h.A.get_course(con, s["course_id"])
        st = h.A.course_settings(c)
    from fastapi.testclient import TestClient
    phone = TestClient(h.A.app, base_url="http://test")  # 全新客戶端，完全沒有裝置 cookie
    with time_machine.travel(s["opened_at"] + 50, tick=False):
        slot = int(h.A.now() // st["qr_interval"])
        sig = h.A.slot_sig(s, slot)
        r1 = phone.get(f"/s/{sid}/{slot}/{sig}")
        assert r1.status_code == 200, r1.text
    with h.A.db() as con:
        ev = con.execute("SELECT device8 FROM events WHERE session_id=? AND kind='land_ok'", (sid,)).fetchone()
    assert ev is not None and ev["device8"] != "", "第一次來的人落地事件也要記到裝置，不能是空字串"
    with time_machine.travel(s["opened_at"] + 70, tick=False):
        slot2 = int(h.A.now() // st["qr_interval"])
        sig2 = h.A.slot_sig(s, slot2)
        r2 = phone.get(f"/s/{sid}/{slot2}/{sig2}")  # 帶著剛剛拿到的 cookie 重新落地
        assert r2.status_code == 200, r2.text
        grant2 = re.search(r'GRANT="([^"]+)"', r2.text).group(1)
        j = phone.post("/api/checkin", data={"grant": grant2, "student_id": "9994A001", "name": "學生1",
                                              "pin": "1234", "pin2": "1234", "hints": "h"}).json()
    assert j.get("ok") and j["status"] == "出席", j
    d = h.db_rows(sid)["9994A001"]
    assert d["status"] == "present" and "was_late" not in d["flags"], d


# 投影頁與點名頁的補登指示要一致，且細節在點名頁

def _template_text(name):
    # 不 import app：這支測試若在任何設 ATTEND_DB 的 fixture 之前跑（unit 層隨機順序），
    # import app 會用預設路徑在工作樹建出 data/attend.db，而部署 rsync 的目的地正好是正式庫所在目錄
    from pathlib import Path
    return (Path(__file__).parent.parent / "templates" / name).read_text()


def test_projector_watchdog_text_points_to_session_page():
    src = _template_text("projector.html")
    assert "事後在點名頁補登" not in src, "看門狗文字不該再講「事後在點名頁補登」，要改成「補登方法看點名頁」"
    assert "補登方法看點名頁" in src, "看門狗文字要指到點名頁"


def test_projector_netmsg_small_text_points_to_session_page():
    src = _template_text("projector.html")
    assert "事後在點名頁補登" not in src
    # 小字在 showNetOverlay() 裡組字串，含已 N 秒；只驗關鍵句
    assert "超過一分鐘還連不上，請先改用紙本或口頭點名，補登方法看點名頁" in src


def test_session_projmsg_red_states_have_manual_checkin_guidance():
    """紅字文案在 JS 的狀態表裡；這裡只驗證點名頁原始碼含補登說明，
    且含「遲到上限之前」與「不要按結束」兩個關鍵詞。"""
    src = _template_text("session.html")
    assert "事後在這頁補登" not in src
    assert "遲到上限之前" in src
    assert "不要按結束" in src
    assert "改用紙本或口頭點名時" in src


# 「投影頁連線正常」只在投影頁真的收到回應時才顯示

def test_projector_seen_only_updates_when_have_matches_recent_slot(h):
    """have 空白、對不上、或等於這一格／前一格／前兩格的碼，projector_age 的更新結果要
    分別符合預期——只有『對得上最近三格之一』才代表上一次回應真的送到了投影頁。"""
    sid = h.open("CD")
    with h.A.db() as con:
        s = h.A.get_session(con, sid)
        st = h.A.course_settings(h.A.get_course(con, s["course_id"]))
    iv = st["qr_interval"]
    slot = int(h.A.now() // iv)
    frozen = (slot + 0.5) * iv  # 凍結在這一格中段，之後的請求都落在同一格
    with time_machine.travel(frozen, tick=False):
        codes = {g: h.A.code6(h.A.slot_sig(s, g)) for g in (slot, slot - 1, slot - 2)}
        bad = next(c for c in ("000000", "999999") if c not in codes.values())

        h.A.PROJECTOR_SEEN.pop(sid, None)
        h.T.get(f"/api/t/session/{sid}/qr")  # have 空白
        assert h.state(sid)["projector_age"] is None, "have 空白不該更新 projector_age"

        h.A.PROJECTOR_SEEN.pop(sid, None)
        h.T.get(f"/api/t/session/{sid}/qr", params={"have": bad})
        assert h.state(sid)["projector_age"] is None, "have 對不上不該更新 projector_age"

        for label, g in (("這一格", slot), ("前一格", slot - 1), ("前兩格", slot - 2)):
            h.A.PROJECTOR_SEEN.pop(sid, None)
            h.T.get(f"/api/t/session/{sid}/qr", params={"have": codes[g]})
            assert h.state(sid)["projector_age"] is not None, f"have 等於{label}的碼要更新 projector_age"


def test_have_non_ascii_does_not_500(h):
    """fuzz 閘門（schemathesis）餵過含非 ASCII 字元的 have（例如 Runic 字元）打出 500：
    hmac.compare_digest 對含非 ASCII 的 str 直接丟 TypeError，不是回傳不相等。code6() 一定是
    6 位 ASCII 數字，先擋掉不像的值，不像的一律當作不相符（200，不更新 projector_age）。"""
    sid = h.open("CD")
    h.A.PROJECTOR_SEEN.pop(sid, None)
    r = h.T.get(f"/api/t/session/{sid}/qr", params={"have": "᪛᪄᪓᪐᪋᪒"})
    assert r.status_code == 200, r.text
    assert h.state(sid)["projector_age"] is None
    # 太短、太長、混了非數字字元的也都要是「當作不相符」而不是 500
    # 「١٢٣٤٥٦」（阿拉伯－印度數字）：長度 6、isdigit() 為真，只有 isascii() 擋得住——少了它照樣 TypeError
    for bad in ("", "1", "1234567", "abcdef", "12345a", "١٢٣٤٥٦"):
        r = h.T.get(f"/api/t/session/{sid}/qr", params={"have": bad})
        assert r.status_code == 200, (bad, r.text)


# 瀏覽器標籤、待確認原因句、借用手機

def _add_student(h, course, stu, name):
    with h.A.db() as con:
        con.execute("INSERT INTO students(course_id, student_id, name, cls) VALUES(?,?,?,?)",
                    (h.cid[course], stu, name, ""))


# 換瀏覽器被擋，訊息要叫人回去用上次的 App

def test_bound_other_device_message_mentions_app_switch(h):
    sid = h.open("CD")
    h.dev("dev18-owner")
    h.checkin(sid, "dev18-owner", "9994A001", name="學生1", pin="1234")
    other = h.dev("dev18-other")
    # 用對的 PIN（PIN 驗證通過之後才會走到「已綁定在另一個瀏覽器」的 409 檢查；pin2 留空，
    # 不然會先撞「已經設過 PIN」那個 401 分支，見 do_checkin 的 pin2 早退邏輯）
    r = other.post("/api/checkin", data={
        "grant": h.grant(sid, other), "student_id": "9994A001", "name": "學生1", "pin": "1234"})
    assert r.status_code == 409, r.text
    err = r.json()["error"]
    assert "同一個 App" in err, err
    assert "現在就舉手" in err, err


def test_help_html_no_longer_says_after_class(h):
    from pathlib import Path
    html = (Path(h.A.__file__).parent / "templates" / "help.html").read_text()
    assert "下課找老師解除綁定" not in html, "help.html 仍有舊的『下課找老師解除綁定』字樣"
    assert "另一個瀏覽器" in html, "help.html 沒有「另一個瀏覽器」的新標籤"
    assert "從不同 App 開也算不同瀏覽器" in html, "help.html 規則段落沒有補上這句"


def test_me_html_empty_state_mentions_last_app(h):
    from pathlib import Path
    html = (Path(h.A.__file__).parent / "templates" / "me.html").read_text()
    assert "上次簽到時開的 App" in html, "me.html 空狀態沒有提到『上次簽到時開的 App』"


def test_student_html_switch_first_mentions_last_app(h):
    from pathlib import Path
    html = (Path(h.A.__file__).parent / "templates" / "student.html").read_text()
    assert "上次開的 App" in html, "student.html switchFirst() 沒有提到『上次開的 App』"


# 已簽過的收據（again 回應）也要帶待確認原因

def test_again_replies_carry_pending_reason(h):
    _add_student(h, "CD", "DEV1S01", "林溫蒂")
    sid = h.open("CD")
    dev = h.dev("dev19-variant")
    grant = h.grant(sid, dev)
    j1 = dev.post("/api/checkin", data={"grant": grant, "student_id": "DEV1S01", "name": "林温蒂",
                                        "pin": "1234", "pin2": "1234"}).json()
    assert j1["ok"] is True and j1["status"] == "待確認" and j1["pending_reason"] == "name_variant", j1
    # 同一個 grant 重送
    j2 = dev.post("/api/checkin", data={"grant": grant, "student_id": "DEV1S01", "name": "林温蒂",
                                        "pin": "1234", "pin2": "1234"}).json()
    assert j2.get("again") is True, j2
    assert j2.get("pending_reason") == "name_variant", j2
    # 重新落地拿新表單再送
    grant2 = h.grant(sid, dev)
    j3 = dev.post("/api/checkin", data={"grant": grant2, "student_id": "DEV1S01", "name": "林温蒂",
                                        "pin": "1234", "pin2": "1234"}).json()
    assert j3.get("again") is True, j3
    assert j3.get("pending_reason") == "name_variant", j3


def test_pending_reason_from_flags_order(A):
    """待確認原因沿用名字相似優先的順序：name_variant → same_device → device_bound_other。"""
    assert A.pending_reason_from_flags(["device_bound_other", "same_device", "name_variant"]) == "name_variant"
    assert A.pending_reason_from_flags(["device_bound_other", "same_device"]) == "same_device"
    assert A.pending_reason_from_flags(["device_bound_other"]) == "device_bound_other"
    assert A.pending_reason_from_flags([]) is None


# 姓名寫法不同的待確認，請學生核對學號

def test_student_html_name_variant_note_mentions_typo(h):
    from pathlib import Path
    html = (Path(h.A.__file__).parent / "templates" / "student.html").read_text()
    assert "多半是學號打錯" in html, "student.html 沒有學號打錯的新文案"


# 點名剛結束的錯誤訊息要說清楚接下來怎麼做

def test_closed_without_next_session_tells_find_teacher(h):
    sid = h.open("CD")
    phone = h.dev("dev20-phone")
    grant = h.grant(sid, phone)
    h.T.post(f"/api/t/session/{sid}/close")
    r = phone.post("/api/checkin", data={"grant": grant, "student_id": "9994A001", "name": "學生1",
                                         "pin": "1234", "pin2": "1234", "hints": "h"})
    assert r.status_code == 410
    assert r.json()["error"] == "點名已經結束。有到課但沒簽到的同學，請直接找老師。", r.json()


def test_closed_with_next_session_keeps_message(h):
    """有新場次時的訊息不變。"""
    sid = h.open("CD")
    phone = h.dev("dev20-phone2")
    grant = h.grant(sid, phone)
    h.T.post(f"/api/t/session/{sid}/close")
    sid2 = h.open("CD")
    r = phone.post("/api/checkin", data={"grant": grant, "student_id": "9994A002", "name": "學生2",
                                         "pin": "1234", "pin2": "1234", "hints": "h"})
    assert r.status_code == 410
    assert r.json()["error"] == "這一場已經結束，老師開了新的一場，請重新掃描投影幕上的 QR", r.json()
    h.T.post(f"/api/t/session/{sid2}/close")


# 這支手機本場已簽到，重新落地直接顯示結果

def test_revisit_same_url_shows_result_not_form(h):
    sid = h.open("CD")
    phone = h.dev("dev21-phone")
    with h.A.db() as con:
        s = h.A.get_session(con, sid)
    slot = int(h.A.now() // 10)
    sig = h.A.slot_sig(s, slot)
    r0 = phone.get(f"/s/{sid}/{slot}/{sig}")
    assert r0.status_code == 200
    j = h.checkin(sid, "dev21-phone", "9994A001", name="學生1", pin="1234")
    assert j["ok"] is True, j
    r1 = phone.get(f"/s/{sid}/{slot}/{sig}")
    assert r1.status_code == 200, r1.text
    assert "這一場已經簽到了" in r1.text, r1.text
    # 場次還在收簽到，不是完全不給表單——banner 疊在照常輸出、先隱藏的
    # 表單上面，留借用入口；GRANT 照樣要在，否則按「不是我」之後借的人簽不了
    assert 'id="borrow"' in r1.text, r1.text
    assert 'id="form" style="display:none"' in r1.text, r1.text
    assert 'GRANT="' in r1.text, r1.text


def test_still_200_after_qr_slot_expired(h):
    sid = h.open("CD")
    phone = h.dev("dev21-phone2")
    with h.A.db() as con:
        s = h.A.get_session(con, sid)
    slot = int(h.A.now() // 10)
    sig = h.A.slot_sig(s, slot)
    phone.get(f"/s/{sid}/{slot}/{sig}")
    j = h.checkin(sid, "dev21-phone2", "9994A002", name="學生2", pin="1234")
    assert j["ok"] is True, j
    with time_machine.travel(h.A.now() + 9999, tick=False):
        r = phone.get(f"/s/{sid}/{slot}/{sig}")
    assert r.status_code == 200, r.text
    assert "這一場已經簽到了" in r.text, r.text
    # QR 過期路徑跟開放中路徑不一樣——不給表單、不給 GRANT、沒有借用入口，
    # 只多一行「重新掃描」的提示（借用要走沒過期的 QR，不能拿一支已經過期的簽給別人）
    assert "要借這支手機幫別的同學簽到，請重新掃描投影幕上的 QR" in r.text, r.text
    assert 'id="borrow"' not in r.text, r.text
    assert 'id="go"' not in r.text, r.text
    assert 'GRANT="' not in r.text, r.text


def test_unsigned_device_still_gets_410_when_expired(h):
    """沒簽過的罐子行為不變：過期仍 410。"""
    sid = h.open("CD")
    with h.A.db() as con:
        s = h.A.get_session(con, sid)
    slot = int(h.A.now() // 10)
    sig = h.A.slot_sig(s, slot)
    other = h.dev("dev21-other")
    with time_machine.travel(h.A.now() + 9999, tick=False):
        r = other.get(f"/s/{sid}/{slot}/{sig}")
    assert r.status_code == 410, r.text


def test_pending_status_shows_note(h):
    """待確認狀態的第二行改用原因句。"""
    _add_student(h, "CD", "DEV2S02", "林溫蒂")
    sid = h.open("CD")
    dev = h.dev("dev21-variant")
    with h.A.db() as con:
        s = h.A.get_session(con, sid)
    slot = int(h.A.now() // 10)
    sig = h.A.slot_sig(s, slot)
    dev.get(f"/s/{sid}/{slot}/{sig}")
    grant = h.grant(sid, dev)
    j = dev.post("/api/checkin", data={"grant": grant, "student_id": "DEV2S02", "name": "林温蒂",
                                       "pin": "1234", "pin2": "1234"}).json()
    assert j["status"] == "待確認", j
    r = dev.get(f"/s/{sid}/{slot}/{sig}")
    assert r.status_code == 200
    assert "多半是學號打錯" in r.text, r.text


# whoami 式判定＋借用手機＋老師解綁重綁

def test_addendum_borrow_via_c_first_bind_goes_pending_same_device(h):
    """P 這支手機目前綁定的甲已經簽到過：/c 落地仍照常給表單＋GRANT（先隱藏）＋#borrow；
    拿同一把 GRANT 幫乙首綁送出，要走待確認、pending_reason 是 same_device。"""
    sid = h.open("CD")
    P = h.dev("dev21add-P")
    j = h.checkin(sid, "dev21add-P", "9994A001", name="學生1", pin="1234")
    assert j["ok"] is True, j
    code = h.T.get(f"/api/t/session/{sid}/qr").json()["code"]
    r = P.post("/c", data={"code": code})
    assert r.status_code == 200, r.text
    assert "這一場已經簽到了" in r.text, r.text
    assert 'id="borrow"' in r.text, r.text
    m = re.search(r'GRANT="([^"]+)"', r.text)
    assert m is not None, r.text
    r2 = P.post("/api/checkin", data={"grant": m.group(1), "student_id": "9994A002", "name": "學生2",
                                       "pin": "5566", "pin2": "5566", "hints": "h"}).json()
    assert r2["ok"] is True, r2
    assert r2["status"] == "待確認", r2
    assert r2.get("pending_reason") == "same_device", r2


def test_addendum_revisit_after_borrow_shows_device_owner_not_borrower(h):
    """甲簽過、乙借同一支手機簽（待確認）之後，這支手機再落地看到的已簽到畫面要是「這支裝置目前
    綁定」的甲，不是剛剛借用、待確認的乙。"""
    sid = h.open("CD")
    P = h.dev("dev21add-P2")
    j = h.checkin(sid, "dev21add-P2", "9994A001", name="學生1", pin="1234")
    assert j["ok"] is True, j
    grant = h.grant(sid, P)
    r2 = P.post("/api/checkin", data={"grant": grant, "student_id": "9994A003", "name": "學生3",
                                       "pin": "7788", "pin2": "7788", "hints": "h"}).json()
    assert r2["ok"] is True and r2["status"] == "待確認", r2
    code = h.T.get(f"/api/t/session/{sid}/qr").json()["code"]
    r3 = P.post("/c", data={"code": code})
    assert r3.status_code == 200, r3.text
    assert "這一場已經簽到了" in r3.text, r3.text
    assert "9994A001" in r3.text and "學生1" in r3.text, r3.text
    assert "9994A003" not in r3.text and "學生3" not in r3.text, r3.text


def test_addendum_unbind_without_reset_pin_allows_rebind_with_old_pin(h):
    """老師解除綁定（不重設 PIN）：這支手機現在誰都不是——/c 落地要回正常表單（不是已簽到頁），
    用原本的 PIN 重新綁定，要回到原本那筆簽到紀錄（again=True，ts 不變）。"""
    sid = h.open("CD")
    P = h.dev("dev21add-unbindA")
    j = h.checkin(sid, "dev21add-unbindA", "9994A001", name="學生1", pin="1234")
    assert j["ok"] is True, j
    pk = h.pk("CD", "9994A001")
    r_unbind = h.T.post(f"/api/t/student/{pk}/unbind", data={"reset_pin": ""})
    assert r_unbind.status_code == 200, r_unbind.text
    code = h.T.get(f"/api/t/session/{sid}/qr").json()["code"]
    r = P.post("/c", data={"code": code})
    assert r.status_code == 200, r.text
    assert "這一場已經簽到了" not in r.text, r.text
    m = re.search(r'GRANT="([^"]+)"', r.text)
    assert m is not None, r.text
    r2 = P.post("/api/checkin", data={"grant": m.group(1), "student_id": "9994A001", "name": "學生1",
                                       "pin": "1234", "hints": "h"}).json()
    assert r2["ok"] is True, r2
    assert r2.get("again") is True, r2
    assert r2["ts"] == j["ts"], r2


def test_addendum_unbind_with_reset_pin_requires_new_pin_and_recovers_record(h):
    """解除綁定且重設 PIN：舊 PIN 已清空，要用新 PIN 首綁；同樣要回到原本那筆紀錄。"""
    sid = h.open("CD")
    P = h.dev("dev21add-unbindB")
    j = h.checkin(sid, "dev21add-unbindB", "9994A002", name="學生2", pin="4321")
    assert j["ok"] is True, j
    pk = h.pk("CD", "9994A002")
    r_unbind = h.T.post(f"/api/t/student/{pk}/unbind", data={"reset_pin": "1"})
    assert r_unbind.status_code == 200, r_unbind.text
    code = h.T.get(f"/api/t/session/{sid}/qr").json()["code"]
    r = P.post("/c", data={"code": code})
    assert r.status_code == 200, r.text
    assert "這一場已經簽到了" not in r.text, r.text
    m = re.search(r'GRANT="([^"]+)"', r.text)
    assert m is not None, r.text
    r2 = P.post("/api/checkin", data={"grant": m.group(1), "student_id": "9994A002", "name": "學生2",
                                       "pin": "9999", "pin2": "9999", "hints": "h"}).json()
    assert r2["ok"] is True, r2
    assert r2.get("again") is True, r2
    assert r2["ts"] == j["ts"], r2


# 從 /c 進來的簽到頁，截斷時的按鈕文字要提「重新輸入六位數」

def test_via_code_default_button_text(h):
    sid = h.open("CD")
    phone = h.dev("dev22-phone")
    code = h.T.get(f"/api/t/session/{sid}/qr").json()["code"]
    resp = phone.post("/c", data={"code": code})
    assert resp.status_code == 200
    assert "請回上一頁重新輸入六位數" in resp.text, resp.text


def test_qr_entry_keeps_original_button_text(h):
    sid = h.open("CD")
    with h.A.db() as con:
        s = h.A.get_session(con, sid)
    slot = int(h.A.now() // 10)
    sig = h.A.slot_sig(s, slot)
    other = h.dev("dev22-qr")
    r = other.get(f"/s/{sid}/{slot}/{sig}")
    assert "請重新掃描 QR" in r.text, r.text
    assert "請回上一頁重新輸入六位數" not in r.text, r.text


# 學號已簽到、另一支手機又被擋，老師頁要看得到

def test_stolen_binding_shows_conflict_with_late_bind_label(h):
    """甲搶先用正確姓名首綁，乙（本人）稍後被擋——compute_stuck 要看得到，門檻只要 1 筆。"""
    sid = h.open("CD")
    atk = h.checkin(sid, "dev23-attacker", "9994A001", name="學生1", pin="9999")
    assert atk["ok"] is True and atk["status"] == "出席", atk
    vic = h.checkin(sid, "dev23-victim", "9994A001", name="學生1", pin="1234")
    assert vic["ok"] is False, vic
    state = h.state(sid)
    entry = next((x for x in state["stuck"] if x["student_id"] == "9994A001"), None)
    assert entry is not None, state["stuck"]
    assert entry["conflict"] is True, entry
    assert "可能被別人搶先綁定" in entry["reason_label"], entry


def test_prebound_student_conflict_uses_already_checked_in_label(h):
    """上週已綁定、這場正常回簽成功（沒有 late_bind），另一支手機來被擋——標籤不同，且不會重複出現兩筆。"""
    sid0 = h.open("CD")
    h.checkin(sid0, "dev23-preown", "9994A002", name="學生2", pin="4321")
    h.T.post(f"/api/t/session/{sid0}/close")
    sid = h.open("CD")
    ok = h.checkin(sid, "dev23-preown", "9994A002", name="學生2", pin="4321")
    assert ok["ok"] is True and ok.get("again") is not True, ok
    other = h.dev("dev23-preother")
    bad = other.post("/api/checkin", data={"grant": h.grant(sid, other), "student_id": "9994A002", "pin": "0000"}).json()
    assert bad["ok"] is False, bad
    with h.A.db() as con:
        s = h.A.get_session(con, sid)
        stuck = h.A.compute_stuck(con, sid, s["opened_at"])
        # 額外插一筆「本人自己那支已綁裝置」的干擾拒絕事件（現實中不會發生，這裡直接寫 DB 驗排除條件）
        own_checkin = con.execute("SELECT device_hash FROM checkins WHERE session_id=? AND student_pk=?",
                                  (sid, h.pk("CD", "9994A002"))).fetchone()
        con.execute("INSERT INTO events(ts, session_id, kind, reason, device8, ua_class, student_pk) VALUES(?,?,?,?,?,?,?)",
                    (h.A.now(), sid, "checkin_reject", "pin_wrong", own_checkin["device_hash"][:8], "iOS-Safari",
                     h.pk("CD", "9994A002")))
        con.commit()
        stuck2 = h.A.compute_stuck(con, sid, s["opened_at"])
    entries = [x for x in stuck if x["student_id"] == "9994A002"]
    assert len(entries) == 1, stuck
    assert entries[0]["conflict"] is True, entries[0]
    assert entries[0]["reason_label"] == "已經簽到，另一支手機又用這個學號簽：請當場確認本人", entries[0]
    entries2 = [x for x in stuck2 if x["student_id"] == "9994A002"]
    assert len(entries2) == 1, "自己那支已綁裝置的拒絕事件不該多算出第二筆或改變結論"


def test_own_device_only_reject_not_conflict(h):
    """同一支手機（同 cookie）自己重送被拒，不列入——用直接寫 events 模擬（正常流程無法讓已綁裝置產生這類拒絕）。"""
    sid = h.open("CD")
    j = h.checkin(sid, "dev23-selfdev", "9994A003", name="學生3", pin="1357")
    assert j["ok"] is True, j
    with h.A.db() as con:
        s = h.A.get_session(con, sid)
        pk = h.pk("CD", "9994A003")
        own = con.execute("SELECT device_hash FROM checkins WHERE session_id=? AND student_pk=?", (sid, pk)).fetchone()
        con.execute("INSERT INTO events(ts, session_id, kind, reason, device8, ua_class, student_pk) VALUES(?,?,?,?,?,?,?)",
                    (h.A.now(), sid, "checkin_reject", "pin_wrong", own["device_hash"][:8], "iOS-Safari", pk))
        con.commit()
        stuck = h.A.compute_stuck(con, sid, s["opened_at"])
    assert not any(x["student_id"] == "9994A003" for x in stuck), stuck


def test_reject_before_checkin_ignored_reject_after_checkin_counted(h):
    """只算「這筆簽到之後」才發生的拒絕。同一個學號、同一支干擾裝置：
    一筆拒絕發生在簽到之前（正常的「先打錯幾次才簽到成功」，不是搶綁）、一筆發生在簽到之後
    （真的疑似被搶綁）——這個學號仍要被列進 stuck，而且 conflict 是 True（判準看「之後」那一筆，
    不是「之前」那一筆被忽略了就整個不列）。"""
    sid = h.open("CD")
    ok = h.checkin(sid, "dev23-mix-owner", "9994A001", name="學生1", pin="2468")
    assert ok["ok"] is True, ok
    with h.A.db() as con:
        s = h.A.get_session(con, sid)
        pk = h.pk("CD", "9994A001")
        own = con.execute("SELECT ts, device_hash FROM checkins WHERE session_id=? AND student_pk=?", (sid, pk)).fetchone()
        checkin_ts = own["ts"]
        other_dev8 = "eeeeeeee"
        assert other_dev8 != own["device_hash"][:8]
        con.execute("INSERT INTO events(ts, session_id, kind, reason, device8, ua_class, student_pk) VALUES(?,?,?,?,?,?,?)",
                    (checkin_ts - 30, sid, "checkin_reject", "pin_wrong", other_dev8, "iOS-Safari", pk))
        con.execute("INSERT INTO events(ts, session_id, kind, reason, device8, ua_class, student_pk) VALUES(?,?,?,?,?,?,?)",
                    (checkin_ts + 5, sid, "checkin_reject", "pin_wrong", other_dev8, "iOS-Safari", pk))
        con.commit()
        stuck = h.A.compute_stuck(con, sid, s["opened_at"])
    entries = [x for x in stuck if x["student_id"] == "9994A001"]
    assert len(entries) == 1, stuck
    assert entries[0]["conflict"] is True, entries[0]


def test_not_yet_checked_in_threshold_unchanged(h):
    """還沒簽到成功的學生：被拒 1 次不列入、2 次列入（這段行為不變）。"""
    sid0 = h.open("CD")
    h.checkin(sid0, "dev23-notdev", "9994A004", name="學生4", pin="7777")
    h.T.post(f"/api/t/session/{sid0}/close")
    sid = h.open("CD")
    dev2 = h.dev("dev23-notdev-b")
    bad1 = dev2.post("/api/checkin", data={"grant": h.grant(sid, dev2), "student_id": "9994A004", "pin": "0000"}).json()
    assert bad1["ok"] is False, bad1
    stuck_ids_1 = {x["student_id"] for x in h.state(sid)["stuck"]}
    assert "9994A004" not in stuck_ids_1, stuck_ids_1
    bad2 = dev2.post("/api/checkin", data={"grant": h.grant(sid, dev2), "student_id": "9994A004", "pin": "0000"}).json()
    assert bad2["ok"] is False, bad2
    stuck_ids_2 = {x["student_id"] for x in h.state(sid)["stuck"]}
    assert "9994A004" in stuck_ids_2, stuck_ids_2


# session.html：conflict 項目要標紅字

def test_session_html_conflict_gets_err_class(h):
    from pathlib import Path
    html = (Path(h.A.__file__).parent / "templates" / "session.html").read_text()
    assert "x.conflict" in html, "session.html 的卡住名單沒有依 conflict 判斷樣式"


# 覆蓋率補完：本批新增分支的邊界情況

def test_resend_after_grant_expired_pending_status_carries_pending_reason(h):
    """過期重送收據混待確認原因：待確認狀態的舊收據重送也要帶 pending_reason（走
    _resend_receipt_for_device，不是 again_payload 那條早退路徑——同一個 grant 或換一把
    仍未過期的 grant 都會走正常流程提早回 again_payload，只有「grant 簽章有效但已逾時」
    才會走到這裡，見 test_resend_after_grant_expired_returns_receipt）。"""
    sid = h.open("CD")
    r1 = h.checkin(sid, "dev5dev6", "9994A001", name="學生1", pin="6001", pin2="6001")
    assert r1["ok"] is True, r1
    r2 = h.checkin(sid, "dev5dev6", "9994A002", name="學生2", pin="6002", pin2="6002")
    assert r2["ok"] is True and r2["status"] == "待確認", r2
    dev = h.dev("dev5dev6")
    grant = h.grant(sid, dev)
    future = h.A.now() + h.A.DEFAULT_SETTINGS["grant_seconds"] + 1
    with time_machine.travel(future, tick=False):
        r = dev.post("/api/checkin", data={"grant": grant, "student_id": "9994A002", "pin": "6002"})
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["ok"] is True and j["again"] is True and j["status"] == "待確認", j
    assert j.get("pending_reason") == "same_device", j


def test_compute_stuck_first_loop_skips_deleted_student(h):
    """第一段迴圈：events 裡的 student_pk 若查不到學生（例如事後被刪），
    要跳過而不是整支噴例外——用不存在的 student_pk 直接寫 events 模擬。"""
    sid = h.open("CD")
    with h.A.db() as con:
        s = h.A.get_session(con, sid)
        ghost_pk = 999999
        for _ in range(2):
            con.execute("INSERT INTO events(ts, session_id, kind, reason, device8, ua_class, student_pk) "
                        "VALUES(?,?,?,?,?,?,?)", (h.A.now(), sid, "checkin_reject", "pin_wrong", "ffffffff", "iOS-Safari", ghost_pk))
        con.commit()
        stuck = h.A.compute_stuck(con, sid, s["opened_at"])
    assert not any(x["pk"] == ghost_pk for x in stuck), stuck


def test_compute_stuck_second_loop_skips_checkin_with_deleted_student(h):
    """第二段迴圈：checkins 列對應的學生若查不到（同上，事後被刪），
    也要跳過——直接寫一筆不存在學生的 checkins 列＋一筆裝置不同的拒絕事件。"""
    sid = h.open("CD")
    with h.A.db() as con:
        s = h.A.get_session(con, sid)
        ghost_pk = 999998
        con.execute("INSERT INTO checkins(session_id, student_pk, ts, seat, status, flags, ip, device_hash, fp, source) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (sid, ghost_pk, h.A.now(), "", "present", "[]", "", "aaaaaaaaaaaaaaaa", "fp", "qr"))
        con.execute("INSERT INTO events(ts, session_id, kind, reason, device8, ua_class, student_pk) "
                    "VALUES(?,?,?,?,?,?,?)", (h.A.now(), sid, "checkin_reject", "pin_wrong", "bbbbbbbb", "iOS-Safari", ghost_pk))
        con.commit()
        stuck = h.A.compute_stuck(con, sid, s["opened_at"])
    assert not any(x["pk"] == ghost_pk for x in stuck), stuck


# 換了瀏覽器、PIN 對就自動改綁
# 每個測試用獨一無二的裝置名稱（h.dev 的 cookie 罐在同一個行程內跨測試共用）。

def _dh(h, dev):
    """某個「瀏覽器」（cookie 罐）目前的裝置雜湊。"""
    return h.A.hash_device(h.dev(dev).cookies.get(h.A.DK_COOKIE))


def _post_checkin(h, sid, dev, stu, name="", pin="", pin2="", ua="iPhone"):
    cl = h.dev(dev)
    return cl.post("/api/checkin", data={"grant": h.grant(sid, cl), "student_id": stu, "name": name, "pin": pin,
                                         "pin2": pin2, "hints": "h"},
                   headers={"user-agent": ua, "cf-connecting-ip": "140.131.1.1"})


def _prior_week_bound_pair(h, tag, stu="9994A001", name="學生1", pin="1234"):
    """上週：裝置 A 首綁並簽到、結束；本週開新的一場。回 (本週 sid, A 名, B 名)。"""
    s1 = h.open("CD")
    j = h.checkin(s1, f"{tag}-A", stu, name=name, pin=pin)
    assert j["ok"] is True, j
    h.T.post(f"/api/t/session/{s1}/close")
    return h.open("CD"), f"{tag}-A", f"{tag}-B"


def _assert_no_shared_device(h):
    """同一門課不會有兩個學號綁同一個 device_hash。"""
    with h.A.db() as con:
        dup = con.execute("SELECT course_id, device_hash, COUNT(*) n FROM students WHERE device_hash IS NOT NULL "
                          "GROUP BY 1,2 HAVING COUNT(*)>1").fetchall()
    assert dup == [], [dict(x) for x in dup]


def _student_device(h, stu):
    with h.A.db() as con:
        return con.execute("SELECT device_hash FROM students WHERE student_id=? AND course_id=?",
                           (stu, h.cid["CD"])).fetchone()["device_hash"]


def test_rebind_new_browser_with_correct_pin(h):
    s2, a, b = _prior_week_bound_pair(h, "dev25t1")
    r = _post_checkin(h, s2, b, "9994A001", pin="1234", pin2="")
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["ok"] is True and j["status"] == "出席" and j["rebound"] is True, j
    assert "rebind" in json.loads(h.db_rows(s2)["9994A001"]["flags"])
    assert _student_device(h, "9994A001") == _dh(h, b)
    cid = h.cid["CD"]
    assert h.dev(b).post("/api/whoami", data={"course_id": cid}).json()["student_id"] == "9994A001"
    assert h.dev(a).post("/api/whoami", data={"course_id": cid}).json()["known"] is False
    _assert_no_shared_device(h)


def test_rebind_with_first_form_pin2_filled(h):
    s2, a, b = _prior_week_bound_pair(h, "dev25t2")
    r = _post_checkin(h, s2, b, "9994A001", name="學生1", pin="1234", pin2="1234")
    assert r.status_code == 200, r.text
    assert "已經設過 PIN" not in r.text
    assert r.json()["rebound"] is True
    assert _student_device(h, "9994A001") == _dh(h, b)


def test_wrong_pin_never_rebinds_or_leaks(h):
    s2, a, b = _prior_week_bound_pair(h, "dev25t3")
    for p in ("1111", "2222", "3333"):
        r = _post_checkin(h, s2, b, "9994A001", pin=p)
        assert r.status_code == 401, r.text
        assert _student_device(h, "9994A001") == _dh(h, a)
    r = _post_checkin(h, s2, b, "9994A001", pin="1234")
    assert r.status_code == 423, r.text
    assert _student_device(h, "9994A001") == _dh(h, a)


def test_already_signed_this_session_not_rebound(h):
    s2, a, b = _prior_week_bound_pair(h, "dev25t4")
    assert h.checkin(s2, a, "9994A001", pin="1234")["status"] == "出席"
    r = _post_checkin(h, s2, b, "9994A001", pin="1234")
    assert r.status_code == 409, r.text
    err = r.json()["error"]
    for frag in ("另一個瀏覽器", "同一個 App", "現在就舉手", "出席"):
        assert frag in err, err
    assert _student_device(h, "9994A001") == _dh(h, a)
    with h.A.db() as con:
        ev = con.execute("SELECT reason FROM events WHERE kind='checkin_reject' AND session_id=?", (s2,)).fetchall()
        stuck = h.A.compute_stuck(con, s2, h.A.get_session(con, s2)["opened_at"])
    assert [e["reason"] for e in ev] == ["already_recorded"], [dict(e) for e in ev]
    entry = next(x for x in stuck if x["student_id"] == "9994A001")
    # b 是陌生的新環境、打對 PIN，多半是本人又掃一次——改灰字提示，不亮紅
    assert entry["conflict"] is False and entry["info"] is True, entry


def test_teacher_marked_absent_then_new_browser_gets_409(h):
    s2, a, b = _prior_week_bound_pair(h, "dev25t4b")
    h.T.post(f"/api/t/session/{s2}/status", data={"pk": h.pk("CD", "9994A001"), "status": "absent"})
    r = _post_checkin(h, s2, b, "9994A001", pin="1234")
    assert r.status_code == 409, r.text
    assert "缺席" in r.json()["error"], r.text
    assert _student_device(h, "9994A001") == _dh(h, a)


def test_borrowing_someone_elses_bound_phone_does_not_rebind(h):
    s1 = h.open("CD")
    assert h.checkin(s1, "dev25t5-A", "9994A001", name="學生1", pin="1234")["ok"]
    assert h.checkin(s1, "dev25t5-B", "9994A002", name="學生2", pin="2222")["ok"]
    h.T.post(f"/api/t/session/{s1}/close")
    s2 = h.open("CD")
    r = _post_checkin(h, s2, "dev25t5-B", "9994A001", pin="1234")
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["status"] == "待確認" and j["pending_reason"] == "device_bound_other", j
    assert "rebound" not in j
    assert _student_device(h, "9994A001") == _dh(h, "dev25t5-A")
    assert _student_device(h, "9994A002") == _dh(h, "dev25t5-B")
    _assert_no_shared_device(h)


def test_allow_bind_off_does_not_block_rebind(h):
    s2, a, b = _prior_week_bound_pair(h, "dev25t6")
    h.T.post(f"/api/t/course/{h.cid['CD']}/bind", data={"allow": "0"})
    r = _post_checkin(h, s2, b, "9994A001", pin="1234")
    assert r.status_code == 200, r.text
    assert r.json()["rebound"] is True
    assert _student_device(h, "9994A001") == _dh(h, b)


def test_rebind_in_late_window_is_late_with_both_flags(h):
    s2, a, b = _prior_week_bound_pair(h, "dev25t7")
    with time_machine.travel(h.A.now() + 300, tick=False):  # 出席窗 180 秒已過、遲到窗 900 秒內
        r = _post_checkin(h, s2, b, "9994A001", pin="1234")
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "遲到", r.text
    flags = json.loads(h.db_rows(s2)["9994A001"]["flags"])
    assert "rebind" in flags and "was_late" in flags, flags


def test_rebind_event_records_old_and_new_device(h):
    """改綁寫一筆 rebind 事件（reason＝舊雜湊前 8 碼），事後查得到誰從哪一支改綁。"""
    s2, a, b = _prior_week_bound_pair(h, "dev25t8e")
    old = _student_device(h, "9994A001")
    assert _post_checkin(h, s2, b, "9994A001", pin="1234").status_code == 200
    with h.A.db() as con:
        ev = [dict(r) for r in con.execute("SELECT * FROM events WHERE kind='rebind'")]
    assert len(ev) == 1, ev
    e = ev[0]
    assert e["reason"] == old[:8] and e["device8"] == _dh(h, b)[:8], e
    assert e["student_pk"] == h.pk("CD", "9994A001") and e["session_id"] == s2 and e["ua_class"] == h.A.classify_ua("iPhone"), e


def test_stuck_label_when_rebind_then_original_owner_returns(h):
    s2, a, b = _prior_week_bound_pair(h, "dev25t8")
    assert _post_checkin(h, s2, b, "9994A001", pin="1234").json()["rebound"] is True  # 冒名者知道 PIN
    r = _post_checkin(h, s2, a, "9994A001", pin="1234")  # 本人用原本的瀏覽器
    assert r.status_code == 409, r.text
    with h.A.db() as con:
        stuck = h.A.compute_stuck(con, s2, h.A.get_session(con, s2)["opened_at"])
    entry = next(x for x in stuck if x["student_id"] == "9994A001")
    assert entry["conflict"] is True and "改綁" in entry["reason_label"], entry
    _assert_no_shared_device(h)


def test_flag_and_reason_labels(h):
    assert h.A.FLAG_LABEL["rebind"] == "換瀏覽器改綁"
    # 換瀏覽器改綁的第 1 種情況（本場已有紀錄）原因碼是 already_recorded；舊碼 bound_other_device 只留給舊事件
    assert h.A.REJECT_REASON_LABEL["already_recorded"] == "本場已有紀錄（打對 PIN）"
    assert h.A.REJECT_REASON_LABEL["bound_other_device"] == "PIN 對但綁在別支（10/17 以前的舊紀錄）"


def test_template_texts(h):
    from pathlib import Path
    tpl = Path(h.A.__file__).parent / "templates"
    student = (tpl / "student.html").read_text()
    for frag in ("已用 PIN 確認並改綁到這裡", "輸對就會改用這個瀏覽器簽到", "PIN 兩格都填原本那組，就會改綁到這個瀏覽器"):
        assert frag in student, frag
    helpt = (tpl / "help.html").read_text()
    assert "換手機要找老師解除綁定" not in helpt
    for frag in ("輸一次原本的 PIN 改綁", "這個學號這一場已經有紀錄（出席／遲到…）",
                 "PIN 只有換手機、換瀏覽器或被老師解除綁定時才要用", "iPhone 建議用內建相機 App 掃"):
        assert frag in helpt, frag
    me = (tpl / "me.html").read_text()
    assert "下次簽到在那裡輸一次 PIN 改綁之後就看得到" in me
    assert "下次簽到時在這個瀏覽器輸一次原本的 PIN 改綁過來" in me


# 手動記錄後的卡住名單與出錯路徑留紀錄（L1–L8）

def _stuck_for(h, sid, stu):
    with h.A.db() as con:
        s = h.A.get_session(con, sid)
        return [x for x in h.A.compute_stuck(con, sid, s["opened_at"]) if x["student_id"] == stu]


def _events(h, kind=None, **where):
    q, args = "SELECT * FROM events WHERE 1=1", []
    if kind:
        q += " AND kind=?"; args.append(kind)
    for k, v in where.items():
        q += f" AND {k}=?"; args.append(v)
    with h.A.db() as con:
        return [dict(r) for r in con.execute(q + " ORDER BY id", args)]


def _roster_strings(h):
    with h.A.db() as con:
        return [v for r in con.execute("SELECT student_id, name FROM students") for v in (r["student_id"], r["name"])]


def _assert_events_have_no_roster_text(h, rows):
    roster = _roster_strings(h)
    for e in rows:
        for field in ("reason", "device8", "ua_class", "kind"):
            for s in roster:
                assert s not in str(e[field]), (s, e)


# 手動補登之後卡住名單不該轉紅

def test_manual_present_after_two_blocks_clears_stuck_without_red(h):
    s2, a, b = _prior_week_bound_pair(h, "stk-a")
    for p in ("1111", "2222"):
        assert _post_checkin(h, s2, b, "9994A001", pin=p).status_code == 401
    assert [x["conflict"] for x in _stuck_for(h, s2, "9994A001")] == [False]
    pk = h.pk("CD", "9994A001")
    assert h.T.post(f"/api/t/session/{s2}/status", data={"pk": pk, "status": "present"}).json()["ok"]
    assert _stuck_for(h, s2, "9994A001") == []
    ev = _events(h, "manual_status")
    assert len(ev) == 1 and ev[0]["reason"] == "present" and ev[0]["student_pk"] == pk and ev[0]["session_id"] == s2, ev


def test_bulk_correction_after_close_not_red_and_logged(h):
    s2, a, b = _prior_week_bound_pair(h, "stk-b")
    for p in ("1111", "2222"):
        assert _post_checkin(h, s2, b, "9994A001", pin=p).status_code == 401
    h.T.post(f"/api/t/session/{s2}/close")
    assert h.T.post(f"/api/t/session/{s2}/bulk-status", data={"to": "present"}).json()["n"] >= 1
    assert _stuck_for(h, s2, "9994A001") == []
    ev = _events(h, "manual_status", student_pk=h.pk("CD", "9994A001"))
    assert [e["reason"] for e in ev] == ["bulk:present"], ev


def test_manual_row_wrong_pin_from_other_device_grey_for_10_minutes_then_red(h):
    # 老師手動記完 10 分鐘內，別台的錯 PIN 多半是本人又試，灰字（info、非 conflict）；
    # 超過 10 分鐘照舊紅字。
    with time_machine.travel(h.A.now(), tick=False) as trav:
        s2, a, b = _prior_week_bound_pair(h, "stk-c")
        pk = h.pk("CD", "9994A001")
        h.T.post(f"/api/t/session/{s2}/status", data={"pk": pk, "status": "present"})
        trav.shift(120)  # 手動記之後 2 分鐘（時鐘凍結時同一個時間戳不算「之後」）
        assert _post_checkin(h, s2, b, "9994A001", pin="1111").status_code == 401
        entry = _stuck_for(h, s2, "9994A001")
        assert len(entry) == 1 and entry[0]["conflict"] is False and entry[0]["info"] is True and entry[0]["reason"] == "pin_wrong", entry
        trav.shift(9 * 60)  # 手動記之後 11 分鐘
        assert _post_checkin(h, s2, b, "9994A001", pin="2222").status_code == 401
        entry = _stuck_for(h, s2, "9994A001")
        assert len(entry) == 1 and entry[0]["conflict"] is True and not entry[0].get("info"), entry


def test_manual_row_ignores_bound_other_device_after_teacher_handled(h):
    s2, a, b = _prior_week_bound_pair(h, "stk-d")
    h.T.post(f"/api/t/session/{s2}/status", data={"pk": h.pk("CD", "9994A001"), "status": "present"})
    r = _post_checkin(h, s2, b, "9994A001", pin="1234")
    assert r.status_code == 409, r.text  # 換瀏覽器改綁的第 1 種：本場已有老師記的紀錄
    assert _stuck_for(h, s2, "9994A001") == []


def test_qr_row_still_counts_bound_other_device(h):
    s2, a, b = _prior_week_bound_pair(h, "stk-e")
    assert h.checkin(s2, a, "9994A001", pin="1234")["ok"]
    assert _post_checkin(h, s2, b, "9994A001", pin="1234").status_code == 409
    entry = _stuck_for(h, s2, "9994A001")
    # 要守的是「QR 列照舊列出、手動列不列」；陌生新環境打對 PIN 是灰字（info），仍要列出
    assert len(entry) == 1 and entry[0]["conflict"] is False and entry[0]["info"] is True \
        and entry[0]["reason"] == "already_recorded", entry


def test_manual_row_without_manual_status_event_is_skipped(h):
    """舊資料：手動列沒有 manual_status 事件可當底線——不列紅字。"""
    s2, a, b = _prior_week_bound_pair(h, "stk-f")
    pk = h.pk("CD", "9994A001")
    with h.A.db() as con:
        con.execute("DELETE FROM checkins WHERE session_id=? AND student_pk=?", (s2, pk))
        con.execute("INSERT INTO checkins(session_id, student_pk, ts, status, flags, source) VALUES(?,?,0,'present','[\"manual\"]','manual')",
                    (s2, pk))
        con.execute("INSERT INTO events(ts, session_id, kind, reason, device8, ua_class, student_pk) VALUES(?,?,?,?,?,?,?)",
                    (h.A.now() + 1, s2, "checkin_reject", "pin_wrong", "eeeeeeee", "iOS-Safari", pk))
    assert _stuck_for(h, s2, "9994A001") == []


# 老師「重設」要清本場 PIN 失敗次數

def _lock_out(h, sid, dev, stu="9994A001"):
    for p in ("1357", "2468", "9753"):  # 都不是這幾位學號的真 PIN（1234／2222）
        assert _post_checkin(h, sid, dev, stu, pin=p).status_code == 401
    r = _post_checkin(h, sid, dev, stu, pin="4444")
    assert r.status_code == 423, r.text


def test_reset_clears_pin_lock_so_same_browser_can_rebind(h):
    s2, a, b = _prior_week_bound_pair(h, "rst-a")
    pk = h.pk("CD", "9994A001")
    _lock_out(h, s2, b)
    assert h.row(s2, "9994A001")["locked"] is True
    assert h.T.post(f"/api/t/student/{pk}/unbind", data={"reset_pin": "1"}).json()["ok"]
    assert not [k for k in h.A._pin_fail_recent if k[1] == pk]
    assert h.row(s2, "9994A001")["locked"] is False
    r = _post_checkin(h, s2, b, "9994A001", name="學生1", pin="2468", pin2="2468")
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "出席"


def test_reset_clears_total_lock_too(h):
    s2, a, b = _prior_week_bound_pair(h, "rst-b")
    pk = h.pk("CD", "9994A001")
    with h.A.db() as con:
        con.execute("INSERT INTO pin_fail(session_id, student_pk, device_hash, n) VALUES(?,?,'',20)", (s2, pk))
    assert _post_checkin(h, s2, "dev26b-fresh1", "9994A001", name="學生1", pin="5555", pin2="5555").status_code == 423
    h.T.post(f"/api/t/student/{pk}/unbind", data={"reset_pin": "1"})
    r = _post_checkin(h, s2, "dev26b-fresh2", "9994A001", name="學生1", pin="2468", pin2="2468")
    assert r.status_code == 200, r.text


def test_unbind_without_reset_keeps_lock(h):
    """取捨：只解綁、不重設，舊 PIN 還有效，鎖定仍有防猜的作用。"""
    s2, a, b = _prior_week_bound_pair(h, "rst-c")
    pk = h.pk("CD", "9994A001")
    _lock_out(h, s2, b)
    h.T.post(f"/api/t/student/{pk}/unbind", data={"reset_pin": ""})
    assert _post_checkin(h, s2, b, "9994A001", pin="1234").status_code == 423


# 已簽到的人不顯示「PIN 鎖定」

def test_locked_tag_hidden_after_success_but_fail_rows_kept(h):
    s1 = h.open("CD")
    assert h.checkin(s1, "dev27-A", "9994A001", name="學生1", pin="1234")["ok"]
    assert h.checkin(s1, "dev27-A2", "9994A002", name="學生2", pin="2222")["ok"]
    h.T.post(f"/api/t/session/{s1}/close")
    s2 = h.open("CD")
    _lock_out(h, s2, "dev27-C", "9994A001")
    assert h.row(s2, "9994A001")["locked"] is True
    assert h.checkin(s2, "dev27-A", "9994A001")["status"] == "出席"  # 本人回到原本綁定的瀏覽器
    assert h.row(s2, "9994A001")["locked"] is False
    with h.A.db() as con:
        assert con.execute("SELECT COUNT(*) FROM pin_fail WHERE session_id=? AND student_pk=?",
                           (s2, h.pk("CD", "9994A001"))).fetchone()[0] > 0
    # 另一位：鎖定後老師只解綁（不重設），換新罐子用對的 PIN 簽到成功
    _lock_out(h, s2, "dev27-C2", "9994A002")
    h.T.post(f"/api/t/student/{h.pk('CD', '9994A002')}/unbind", data={"reset_pin": ""})
    assert _post_checkin(h, s2, "dev27-D", "9994A002", pin="2222").json()["status"] == "出席"
    assert h.row(s2, "9994A002")["locked"] is False


# PIN 打對回 409 時，失敗次數要真的清掉

def test_correct_pin_409_still_clears_fail_counts(h):
    s2, a, b = _prior_week_bound_pair(h, "pin409")
    assert h.checkin(s2, a, "9994A001")["ok"]
    assert _post_checkin(h, s2, b, "9994A001", pin="9999").status_code == 401
    with h.A.db() as con:
        assert con.execute("SELECT n FROM pin_fail WHERE session_id=? AND device_hash=?", (s2, _dh(h, b))).fetchone()["n"] == 1
    assert _post_checkin(h, s2, b, "9994A001", pin="1234").status_code == 409
    with h.A.db() as con:
        left = con.execute("SELECT device_hash FROM pin_fail WHERE session_id=? AND student_pk=?",
                           (s2, h.pk("CD", "9994A001"))).fetchall()
    assert left == [], [dict(x) for x in left]


# 舊分頁與「結束後馬上開綁定」

BIND_CLOSED_MSG = "這一場已經結束。老師現在開的是只做綁定的場次，不算今天的出缺勤；有到課但沒簽到成功的，請直接找老師補登。"


def _entry_url(h, sid):
    with h.A.db() as con:
        s = h.A.get_session(con, sid)
        st = h.A.course_settings(h.A.get_course(con, s["course_id"]))
    slot = int(h.A.now() // st["qr_interval"])
    return f"/s/{sid}/{slot}/{h.A.slot_sig(s, slot)}"


def test_last_weeks_tab_does_not_claim_already_checked(h):
    s1 = h.open("CD")
    assert h.checkin(s1, "dev28-P", "9994A001", name="學生1", pin="1234")["ok"]
    old_url = _entry_url(h, s1)
    h.T.post(f"/api/t/session/{s1}/close")
    with time_machine.travel(h.A.now() + 7 * 86400, tick=False):
        s2 = h.open("CD")
        r = h.dev("dev28-P").get(old_url)
        assert r.status_code == 410, r.status_code
        assert "這一場已經簽到了" not in r.text and "之前那一場" in r.text, r.text
        h.T.post(f"/api/t/session/{s2}/close")
        r = h.dev("dev28-P").get(old_url)  # 沒有進行中的場次：照舊的過期說法
        assert r.status_code == 410 and "這個 QR 已經過期" in r.text and "之前那一場" not in r.text, r.text
        assert "這一場已經簽到了" not in r.text
        s3 = h.open("CD", "bind")
        r = h.dev("dev28-P").get(old_url)  # 進行中的是綁定場次：用綁定場次那句
        assert r.status_code == 410 and "只做綁定的場次" in r.text, r.text
        assert s3


def test_close_then_bind_old_form_gets_bind_only_message(h):
    s1 = h.open("CD")
    assert h.checkin(s1, "dev29-bound", "9994A001", name="學生1", pin="1234")["ok"]
    h.T.post(f"/api/t/session/{s1}/close")
    s2 = h.open("CD")
    cl = h.dev("dev29-bound")
    grant = h.grant(s2, cl)  # 學號 9994A001 已綁這支，掃到之後
    h.T.post(f"/api/t/session/{s2}/close")  # 老師按結束
    h.open("CD", "bind")
    r = cl.post("/api/checkin", data={"grant": grant, "student_id": "9994A001", "hints": "h"})
    assert r.status_code == 410, r.text
    assert r.json()["error"] == BIND_CLOSED_MSG, r.text
    assert h.A.BIND_ONLY_CLOSED_MSG == BIND_CLOSED_MSG
    from pathlib import Path
    assert BIND_CLOSED_MSG in (Path(h.A.__file__).parent / "templates" / "student.html").read_text()


# 借手機不改動老師定案或抽點確認過的狀態

def _borrow_setup(h, tag):
    """S1 綁在 X 並簽到；S2 是從沒綁過的另一個學號。回 (sid, X 名)。"""
    sid = h.open("CD")
    x = f"{tag}-X"
    assert h.checkin(sid, x, "9994A001", name="學生1", pin="1234")["ok"]
    return sid, x


def test_manual_status_not_flipped_by_borrow(h):
    sid, x = _borrow_setup(h, "borrow-a")
    # 同狀態再按一次不改 source（回 unchanged），所以前置要是真的變更——
    # 先改遲到、再改回出席，保護的就是「老師把遲到改回出席」
    for st in ("late", "present"):
        assert h.T.post(f"/api/t/session/{sid}/status", data={"pk": h.pk("CD", "9994A001"), "status": st}).json().get("unchanged") is None
    r = _post_checkin(h, sid, x, "9994A002", name="學生2", pin="5555", pin2="5555")
    assert r.json()["status"] == "待確認", r.text
    rows = h.db_rows(sid)
    assert rows["9994A001"]["status"] == "present", rows["9994A001"]
    assert "same_device" in json.loads(rows["9994A001"]["flags"])


def test_spot_ok_not_flipped_by_borrow(h):
    sid, x = _borrow_setup(h, "borrow-b")
    picks = h.T.post(f"/api/t/session/{sid}/spotcheck").json()["picks"]
    assert h.T.post(f"/api/t/spotcheck/{picks[0]['id']}", data={"present": "1"}).json()["ok"]
    r = _post_checkin(h, sid, x, "9994A002", name="學生2", pin="5555", pin2="5555")
    assert r.json()["status"] == "待確認", r.text
    rows = h.db_rows(sid)
    assert rows["9994A001"]["status"] == "present"
    assert {"spot_ok", "same_device"} <= set(json.loads(rows["9994A001"]["flags"]))


def test_plain_qr_row_still_turns_pending(h):
    sid, x = _borrow_setup(h, "borrow-c")
    _post_checkin(h, sid, x, "9994A002", name="學生2", pin="5555", pin2="5555")
    assert h.db_rows(sid)["9994A001"]["status"] == "pending"


# 老師處理後的卡住名單

def test_excused_or_absent_by_teacher_leaves_stuck_list(h):
    s2, a, b = _prior_week_bound_pair(h, "excused-leaves")
    for p in ("1111", "2222"):
        assert _post_checkin(h, s2, b, "9994A001", pin=p).status_code == 401
    assert len(_stuck_for(h, s2, "9994A001")) == 1
    pk = h.pk("CD", "9994A001")
    for status in ("excused", "absent"):
        h.T.post(f"/api/t/session/{s2}/status", data={"pk": pk, "status": status})
        assert _stuck_for(h, s2, "9994A001") == [], status


def test_no_qr_after_manual_flag_on_manual_present(h):
    s2, a, b = _prior_week_bound_pair(h, "noqr-after-manual")
    pk = h.pk("CD", "9994A001")
    h.T.post(f"/api/t/session/{s2}/status", data={"pk": pk, "status": "present"})
    h.T.post(f"/api/t/student/{pk}/unbind", data={"reset_pin": ""})
    j = _post_checkin(h, s2, b, "9994A001", pin="1234").json()
    assert j["ok"] and j["again"] is True and j["status"] == "出席", j
    assert "qr_after_manual" not in json.loads(h.db_rows(s2)["9994A001"]["flags"])


# 借手機不繼承出借者的最早落地時間

def _land(h, sid, cl):
    """走 /s/... 掃 QR 落地（記 land_ok），回 grant。"""
    r = cl.get(_entry_url(h, sid), headers={"user-agent": "iPhone"})
    assert r.status_code == 200, r.status_code
    return re.search(r'GRANT="([^"]+)"', r.text).group(1)


def _land_submit(h, sid, dev, stu, **kw):
    cl = h.dev(dev)
    # 同指紋的另一個學號會被轉待確認；不同模擬裝置各用自己的 UA（現實裡是不同手機），同一個 dev 名仍同指紋
    ua = "iPhone/" + hashlib.sha1(dev.encode()).hexdigest()[:8]
    return cl.post("/api/checkin", data={"grant": _land(h, sid, cl), "student_id": stu, "hints": "h", **kw},
                   headers={"user-agent": ua, "cf-connecting-ip": "140.131.1.1"}).json()


def test_borrower_does_not_inherit_owners_early_landing(h):
    with time_machine.travel(h.A.now(), tick=False) as tr:
        sid = h.open("CD")
        tr.shift(60)
        assert _land_submit(h, sid, "dev31-X", "9994A001", name="學生1", pin="1234", pin2="1234")["status"] == "出席"
        tr.shift(300)  # 開場 6:00，已在遲到區
        j = _land_submit(h, sid, "dev31-X", "9994A003", name="學生3", pin="5555", pin2="5555")
        assert j["status"] == "待確認", j
        assert "was_late" in json.loads(h.db_rows(sid)["9994A003"]["flags"])
        own = _land_submit(h, sid, "dev31-own", "9994A004", name="學生4", pin="6666", pin2="6666")
        assert own["status"] == "遲到", own  # 本人自己的手機：照舊
        picks = h.T.post(f"/api/t/session/{sid}/spotcheck").json()["picks"]
        scid = next(p["id"] for p in picks if p["student_id"] == "9994A003")
        h.T.post(f"/api/t/spotcheck/{scid}", data={"present": "1"})
        assert h.db_rows(sid)["9994A003"]["status"] == "late"


def test_owner_own_phone_still_uses_earliest_landing(h):
    """本人自己的手機：開場 1:00 落地、6:00 才送出，仍是出席。"""
    with time_machine.travel(h.A.now(), tick=False) as tr:
        sid = h.open("CD")
        tr.shift(60)
        cl = h.dev("dev31-self")
        grant_early = _land(h, sid, cl)
        tr.shift(300)
        grant_late = _land(h, sid, cl)
        r = cl.post("/api/checkin", data={"grant": grant_late, "student_id": "9994A002", "name": "學生2", "pin": "2222",
                                          "pin2": "2222", "hints": "h"}).json()
        assert r["status"] == "出席", (r, grant_early)


# 加選生不進出席率分子，也不算未簽到

def test_added_student_excluded_from_rate_and_none_count(h):
    with time_machine.travel(h.A.now(), tick=False) as tr:
        sid = h.open("CD")
        assert h.checkin(sid, "dev32-1", "9994A001", name="學生1", pin="1234")["ok"]
        tr.shift(60)
        with h.A.db() as con:
            for stu in ("ADD01", "ADD02"):
                con.execute("INSERT INTO students(course_id, student_id, name, cls, created_at) VALUES(?,?,?,?,?)",
                            (h.cid["CD"], stu, "加選" + stu[-1], "", h.A.now()))
        h.T.post(f"/api/t/session/{sid}/status", data={"pk": h.pk("CD", "ADD01"), "status": "present"})
        assert h.state(sid)["rate"] == pytest.approx(1 / 4)  # 原名冊 4 人、出席 1；加選生不進分子
        h.T.post(f"/api/t/session/{sid}/close")
        st = h.state(sid)
        assert st["counts"]["none"] == 0, st["counts"]
        na = {r["student_id"]: r["na"] for r in st["rows"]}
        assert na["ADD02"] is True and na["ADD01"] is False and na["9994A001"] is False and na["9994A002"] is False, na
        assert st["rate"] == pytest.approx(1 / 4)


# hold／extend 不跟自動結案搶

def test_hold_loses_race_with_autoclose_returns_409(h, monkeypatch):
    sid = h.open("CD")
    assert h.checkin(sid, "dev33-h", "9994A001", name="學生1", pin="1234")["ok"]
    with h.A.db() as con:
        late_until = h.A.get_session(con, sid)["late_until"]
    with time_machine.travel(late_until - 0.01, tick=False) as tr:
        real_phase, armed = h.A.session_phase, {"on": True}

        def racing(s):
            ph = real_phase(s)
            if armed["on"] and ph != "closed":
                armed["on"] = False
                tr.shift(0.02)  # 判定完的下一瞬間跨過 late_until，另一個分頁的輪詢搶先結案
                c2 = h.A.connect(); h.A.finalize_expired(c2); c2.close()
            return ph
        monkeypatch.setattr(h.A, "session_phase", racing)
        r = h.T.post(f"/api/t/session/{sid}/hold", data={"hold": "1"})
    assert r.status_code == 409, r.text
    with h.A.db() as con:
        s = h.A.get_session(con, sid)
    assert s["closed_at"] is not None and s["note"] != "discord_held_manual", dict(s)


@pytest.mark.parametrize("kind,shift,target", [("bind", 0, None), ("normal", 0, None), ("normal", 190, None), ("normal", 190, "open")])
def test_extend_on_stale_row_never_reopens_closed_session(h, kind, shift, target):
    from fastapi import HTTPException
    sid = h.open("CD", kind)
    with time_machine.travel(h.A.now() + shift, tick=False):
        with h.A.db() as con:
            stale = h.A.get_session(con, sid)
        h.T.post(f"/api/t/session/{sid}/close")  # 另一個分頁先結案了
        with h.A.db() as con:
            with pytest.raises(HTTPException) as ei:
                h.A.do_extend(con, stale, 2, target)
        assert ei.value.status_code == 409 and "已經結束" in ei.value.detail
        with h.A.db() as con:
            now_row = h.A.get_session(con, sid)
        assert (now_row["open_until"], now_row["late_until"]) == (stale["open_until"], stale["late_until"])


# L1–L8：學生看得到的失敗，伺服器都要有事件

def test_L1_code_page_without_live_session_logs_event(h):
    cl = h.dev("l1-dev")
    assert cl.post("/c", data={"code": "123456"}).status_code == 400
    ev = _events(h, "code_no_live")
    assert len(ev) == 1 and ev[0]["session_id"] is None and ev[0]["device8"] == _dh(h, "l1-dev")[:8], ev
    assert ev[0]["ua_class"] == h.A.classify_ua(cl.headers.get("user-agent", ""))
    _assert_events_have_no_roster_text(h, ev)


def test_L2_code_throttle_logs_event(h):
    sid = h.open("CD")
    real = h.T.get(f"/api/t/session/{sid}/qr").json()["code"]
    wrong = f"{(int(real) + 1) % 1_000_000:06d}"
    cl = h.dev("l2-dev")
    for _ in range(10):
        assert cl.post("/c", data={"code": wrong}).status_code == 400
    assert cl.post("/c", data={"code": wrong}).status_code == 429
    ev = _events(h, "code_throttle")
    assert len(ev) == 1 and ev[0]["device8"] == _dh(h, "l2-dev")[:8] and ev[0]["session_id"] is None, ev


def test_L3_whoami_logs_all_four_outcomes(h):
    cid = h.cid["CD"]
    cl = h.dev("l3-dev")
    assert cl.post("/api/whoami", data={"course_id": cid}).json()["known"] is False
    sid = h.open("CD")
    assert h.checkin(sid, "l3-dev", "9994A001", name="學生1", pin="1234")["ok"]
    assert cl.post("/api/whoami", data={"course_id": cid}).json()["known"] is True
    assert h.student().post("/api/whoami", data={"course_id": cid}).json() == {"known": False}  # 沒有 cookie
    other = h.dev("l3-throttled")
    for _ in range(31):
        other.post("/api/whoami", data={"course_id": cid})
    ev = _events(h, "whoami")
    by = {}
    for e in ev:
        by.setdefault(e["reason"], []).append(e)
    assert set(by) == {"unknown", "known", "no_cookie", "throttle"}, [e["reason"] for e in ev]
    assert by["unknown"][0]["session_id"] is None and by["unknown"][0]["device8"] == _dh(h, "l3-dev")[:8]
    assert by["known"][0]["session_id"] == sid and by["known"][0]["student_pk"] == h.pk("CD", "9994A001")
    assert by["no_cookie"][0]["device8"] == "" and by["no_cookie"][0]["session_id"] == sid
    assert len(by["throttle"]) == 1 and by["throttle"][0]["session_id"] == sid
    _assert_events_have_no_roster_text(h, ev)


def test_L4_me_throttle_logs_event(h):
    cl = h.dev("l4-dev")
    for _ in range(30):
        assert cl.post("/api/me").status_code == 200
    assert cl.post("/api/me").status_code == 429
    ev = _events(h, "me_throttle")
    assert len(ev) == 1 and ev[0]["device8"] == _dh(h, "l4-dev")[:8], ev


def test_L5_validation_errors_on_student_routes_log_invalid(h):
    cl = h.dev("l5-dev")
    assert cl.post("/api/whoami", data={"course_id": "0"}).status_code == 400
    assert cl.get("/s/0/1/abc").status_code == 400
    assert cl.post("/api/checkin", data={"student_id": "x"}).status_code == 400  # 少了必填的 grant
    reasons = sorted(e["reason"] for e in _events(h, "invalid"))
    assert reasons == ["/api/checkin", "/api/whoami", "/s/{sid}/{slot}/{sig}"], reasons
    assert all(e["device8"] == _dh(h, "l5-dev")[:8] for e in _events(h, "invalid"))
    h.T.post("/t/course/0/open")  # 老師端的驗證錯誤不記
    assert len(_events(h, "invalid")) == 3


def test_L5_route_missing_falls_back_to_first_path_segment(h):
    import asyncio
    from fastapi.exceptions import RequestValidationError
    from starlette.requests import Request
    scope = {"type": "http", "method": "POST", "path": "/api/whoami", "headers": [], "query_string": b"", "path_params": {}}
    asyncio.run(h.A.validation_exc(Request(scope), RequestValidationError([])))
    assert [e["reason"] for e in _events(h, "invalid")] == ["/api"], _events(h, "invalid")


def test_L6_http_errors_on_student_routes_log_http_err(h):
    sid = h.open("CD")
    url = _entry_url(h, sid)
    h.T.post(f"/api/t/session/{sid}/delete")
    cl = h.dev("l6-dev")
    assert cl.get(url).status_code == 404
    ev = _events(h, "http_err")
    assert len(ev) == 1, ev
    assert ev[0]["reason"] == "404 /s/{sid}/{slot}/{sig}" and ev[0]["session_id"] == sid, ev
    assert ev[0]["device8"] == _dh(h, "l6-dev")[:8]
    # 不記：沒對到路由的 404（掃站機器人）、老師端、/api/checkin（do_checkin 自己記了）
    assert cl.get("/wp-login.php").status_code == 404
    assert h.T.get("/api/t/session/999999/state").status_code == 404
    assert cl.post("/api/checkin", data={"grant": "bogus", "student_id": "9994A001"}).status_code == 410
    assert len(_events(h, "http_err")) == 1
    assert len(_events(h, "checkin_reject")) == 1


def test_L7_client_report_hooks_present_and_beacon_accepts_new_kinds(h):
    from pathlib import Path
    tpl = Path(h.A.__file__).parent / "templates"
    assert "window.attendReport" in (tpl / "base.html").read_text()
    student = (tpl / "student.html").read_text()
    for frag in ("attendReport('neterr'", "attendReport('whoami_fail'"):
        assert frag in student, frag
    cl = h.dev("l7-dev")
    for kind, msg in (("neterr", "http 502"), ("whoami_fail", "")):
        assert cl.post("/api/beacon", data={"kind": kind, "msg": msg, "sid": "5"}).status_code == 204
    ev = [e["reason"] for e in _events(h, "client")]
    assert ev == ["neterr:http 502", "whoami_fail:"], ev


# L8：老師的操作都有時間紀錄

def test_L8_teacher_actions_are_logged_in_the_same_transaction(h):
    sid = h.open("CD")
    pk = h.pk("CD", "9994A002")
    h.T.post(f"/api/t/session/{sid}/status", data={"pk": pk, "status": "excused"})        # 新增列
    h.T.post(f"/api/t/session/{sid}/status", data={"pk": pk, "status": "present"})        # 改既有列
    h.T.post(f"/api/t/student/{pk}/unbind", data={"reset_pin": ""})
    h.T.post(f"/api/t/student/{pk}/unbind", data={"reset_pin": "1"})
    h.T.post(f"/api/t/course/{h.cid['CD']}/bind", data={"allow": "0"})
    h.T.post(f"/api/t/course/{h.cid['CD']}/bind", data={"allow": "1"})
    h.T.post(f"/api/t/session/{sid}/extend", data={"minutes": 2})
    h.T.post(f"/api/t/session/{sid}/extend", data={"minutes": 3, "target": "open"})
    got = [(e["kind"], e["reason"], e["session_id"], e["student_pk"]) for e in _events(h)]
    assert got == [("manual_status", "excused", sid, pk), ("manual_status", "present", sid, pk),
                   ("unbind", "unbind", sid, pk), ("unbind", "reset", sid, pk),
                   ("allow_bind", "0", sid, None), ("allow_bind", "1", sid, None),
                   ("extend", "2::open", sid, None), ("extend", "3:open:open", sid, None)], got
    _assert_events_have_no_roster_text(h, _events(h))
    h.T.post(f"/api/t/session/{sid}/close")
    h.T.post(f"/api/t/student/{pk}/unbind", data={"reset_pin": ""})  # 沒有進行中的場次：session_id 為 None
    assert _events(h, "unbind")[-1]["session_id"] is None


def test_L_new_events_do_not_change_compute_funnel(h):
    sid = h.open("CD")
    assert h.checkin(sid, "lfun-A", "9994A001", name="學生1", pin="1234")["ok"]
    with h.A.db() as con:
        opened = h.A.get_session(con, sid)["opened_at"]
        before = h.A.compute_funnel(con, sid, opened)
    for kind in ("code_no_live", "code_throttle", "whoami", "me_throttle", "invalid", "http_err",
                 "manual_status", "unbind", "allow_bind", "extend", "rebind"):
        h.A.log_event(sid, kind, reason="x", device8="abcdef12", ua_class="Mac", student_pk=1)
    with h.A.db() as con:
        assert h.A.compute_funnel(con, sid, opened) == before


# 簽到被擋標記、解綁與重設

def _prior_bound(h, stu, pin="1234", name=None, dev=None):
    """先在一場已結束的點名把 stu 綁好（有 PIN、有裝置），回傳之後可用的狀態。"""
    s0 = h.open("CD")
    assert h.checkin(s0, dev or f"prior-{stu}", stu, name=name or "學生" + stu[-1], pin=pin)["ok"]
    h.T.post(f"/api/t/session/{s0}/close")


def _wrong_pin_attempt(h, sid, dev, stu, pin="0000"):
    cl = h.dev(dev)
    return cl.post("/api/checkin", data={"grant": h.grant(sid, cl), "student_id": stu, "pin": pin})


def test_tried_marks_blocked_student_through_stuck_expiry_and_close(h):
    _prior_bound(h, "9994A001")
    sid = h.open("CD")
    assert _wrong_pin_attempt(h, sid, "dev30-x", "9994A001").status_code == 401
    assert h.row(sid, "9994A001")["tried"] == "pin_wrong"
    with time_machine.travel(h.A.now() + 181, tick=False):  # 卡住名單（180 秒）已不再列
        assert h.row(sid, "9994A001")["tried"] == "pin_wrong"
    assert h.row(sid, "9994A002")["tried"] == ""  # 從沒掃過
    h.T.post(f"/api/t/session/{sid}/close")  # 自動缺席（source=auto）仍然標著
    r = h.row(sid, "9994A001")
    assert r["status"] == "absent" and r["tried"] == "pin_wrong", r
    h.T.post(f"/api/t/session/{sid}/status", data={"pk": h.pk("CD", "9994A001"), "status": "present"})
    assert h.row(sid, "9994A001")["tried"] == ""  # 老師手動處理過


def test_tried_empty_after_successful_checkin_and_error_kind(h):
    _prior_bound(h, "9994A001")
    sid = h.open("CD")
    _wrong_pin_attempt(h, sid, "dev30-y", "9994A001")
    assert h.row(sid, "9994A001")["tried"] == "pin_wrong"
    assert h.checkin(sid, "dev30-y", "9994A001", pin="1234")["ok"]  # 打對 PIN 簽到成功
    assert h.row(sid, "9994A001")["tried"] == ""
    # 另一位：先被擋（兩次 PIN 不同），再簽到成功，一樣不標
    # （姓名對不上的被擋不歸到這個學號，所以前置用姓名對、PIN 兩次不同的被擋）
    assert _post_checkin(h, sid, "dev30-z", "9994A003", name="學生3", pin="1234", pin2="1235").status_code == 400
    assert h.row(sid, "9994A003")["tried"] == "pin_mismatch2"
    assert h.checkin(sid, "dev30-z", "9994A003", name="學生3", pin="1234")["ok"]
    assert h.row(sid, "9994A003")["tried"] == ""
    # checkin_error：標「error」；之後最後一筆是 reject 就以 reject 為準
    pk = h.pk("CD", "9994A004")
    h.A.log_event(sid, "checkin_error", reason="RuntimeError", student_pk=pk)
    assert h.row(sid, "9994A004")["tried"] == "error"
    h.A.log_event(sid, "checkin_reject", reason="pin_wrong", student_pk=pk)
    assert h.row(sid, "9994A004")["tried"] == "pin_wrong"


def test_templates_have_new_text(h):
    from pathlib import Path
    tpl = Path(h.A.__file__).parent / "templates"
    sess = (tpl / "session.html").read_text()
    assert "掃碼被擋：" in sess and "伺服器出錯" in sess
    assert "開場後加選" in sess
    assert "不在場：已撤回這個學號的綁定，今天不能再綁；人其實在場的話，下次上課再綁" in sess
    home = (tpl / "home.html").read_text()
    assert "那個學號今天不能再綁" in home and "那個學號本場不能再綁" not in home


def test_filter_none_excludes_na_in_template(h):
    from pathlib import Path
    sess = (Path(h.A.__file__).parent / "templates" / "session.html").read_text()
    assert "if(f==='none'&&(r.status||r.na))return false;" in sess


def _unbound_with_old_pin(h, stu="9994A001", pin="1234"):
    _prior_bound(h, stu, pin=pin)
    h.T.post(f"/api/t/course/{h.cid['CD']}/bind", data={"allow": "0"})


def test_reset_today_allows_first_bind_with_allow_bind_off(h):
    _unbound_with_old_pin(h)
    sid = h.open("CD")
    h.T.post(f"/api/t/student/{h.pk('CD', '9994A001')}/unbind", data={"reset_pin": "1"})
    j = h.checkin(sid, "dev34-new", "9994A001", name="學生1", pin="9999")
    assert j["ok"] and j["first_time"], j


def test_unbind_today_allows_rebind_with_old_pin(h):
    _unbound_with_old_pin(h)
    sid = h.open("CD")
    h.T.post(f"/api/t/student/{h.pk('CD', '9994A001')}/unbind", data={"reset_pin": ""})
    j = h.checkin(sid, "dev34-new2", "9994A001", pin="1234")
    assert j["ok"] and not j["first_time"], j


def test_unbind_from_yesterday_does_not_count(h):
    """昨天只按「解除綁定」（PIN 還在）的學號，首次綁定關著也照改綁處理，PIN 對就綁到新裝置。
    昨天按「重設」仍 403 bind_closed，見 test_friction.py 裡重設後綁定關閉的那條。"""
    _unbound_with_old_pin(h)
    sid = h.open("CD")
    pk = h.pk("CD", "9994A001")
    h.T.post(f"/api/t/student/{pk}/unbind", data={"reset_pin": ""})
    with h.A.db() as con:
        con.execute("UPDATE events SET ts=? WHERE kind='unbind' AND student_pk=?", (h.A.today_start() - 1, pk))
    r = _post_checkin(h, sid, "dev34-old", "9994A001", pin="1234", pin2="1234")
    assert r.status_code == 200, r.text
    assert _student_device(h, "9994A001") == _dh(h, "dev34-old")


def test_bind_revoked_still_wins_over_unbind_exemption(h):
    sid = _revoke_in_bind_session(h, dev="dev34rev")
    h.T.post(f"/api/t/session/{sid}/close")
    h.T.post(f"/api/t/student/{h.pk('CD', '9994A001')}/unbind", data={"reset_pin": ""})
    h.T.post(f"/api/t/course/{h.cid['CD']}/bind", data={"allow": "0"})
    sid2 = h.open("CD")
    r = _post_checkin(h, sid2, "dev34rev2", "9994A001", name="學生1", pin="1234", pin2="1234")
    assert r.status_code == 403, r.text
    with h.A.db() as con:
        assert con.execute("SELECT reason FROM events WHERE kind='checkin_reject' ORDER BY id DESC LIMIT 1").fetchone()["reason"] == "bind_revoked"


def test_never_unbound_never_bound_still_bind_closed(h):
    h.T.post(f"/api/t/course/{h.cid['CD']}/bind", data={"allow": "0"})
    sid = h.open("CD")
    r = _post_checkin(h, sid, "dev34-never", "9994A002", name="學生2", pin="1234", pin2="1234")
    assert r.status_code == 403, r.text
    with h.A.db() as con:
        assert con.execute("SELECT reason FROM events WHERE kind='checkin_reject' ORDER BY id DESC LIMIT 1").fetchone()["reason"] == "bind_closed"


def test_students_page_text_and_error_handling(h):
    from pathlib import Path
    sp = (Path(h.A.__file__).parent / "templates" / "students.html").read_text()
    assert "const r=await fetch" in sp and "if(!r.ok)" in sp and "j.error" in sp
    assert "連線中斷，沒有成功，請再試一次" in sp
    # 解除後的提示依伺服器回的旗標分三句，舊的「今天可以直接重新掃 QR 綁定」整句拿掉
    assert "已解除。他今天可以直接重新掃 QR 綁定（不受首次綁定開關限制）。" not in sp
    assert "忘了 PIN 的話請改按「重設」；他這堂課已經 PIN 錯太多次被鎖的話" in sp
    assert "要比開放分鐘多幾分鐘；兩個一樣的話，最後一刻掃到、送出時剛好結束的人沒有遲到區可掉，會直接記缺席。" in sp
    assert "新同學、待確認被放行的人要簽時" in sp and "新同學、換手機的人" not in sp
    assert "學生換手機或瀏覽器時，在新的那邊輸入原本的 PIN 就會自動改綁，不用到這裡處理。" in sp


def test_home_and_receipt_texts(h):
    from pathlib import Path
    tpl = Path(h.A.__file__).parent / "templates"
    home = (tpl / "home.html").read_text()
    for frag in ("<tr><td>換手機或換瀏覽器</td>", "名單標「換瀏覽器改綁」", "名單標「掃碼被擋：…」",
                 "被你解除綁定或重設的人當天不受這個開關限制。",
                 "<tr><td>忘記 PIN</td><td>名冊頁該生按「重設」，綁定、PIN 與本場的 PIN 鎖定一起清掉"):
        assert frag in home, frag
    # 純綁定那句加了「今天還沒簽到成功的，請找老師補登」，照新文字改
    assert "這支手機已綁定你的學號。這一場只做綁定、不算出缺勤；今天還沒簽到成功的，請找老師補登。下次上課掃碼後按一下就完成簽到。" in (tpl / "student.html").read_text()


# 名單旗標與重設
# 裝置名稱一律獨一無二。

def _bound_next_session(h, tag, stu="9994A001", name="學生1"):
    """上週首綁並簽到、結束；回本週新的一場 sid 與裝置名。"""
    s1 = h.open("CD")
    assert h.checkin(s1, tag, stu, name=name, pin="1234")["ok"]
    h.T.post(f"/api/t/session/{s1}/close")
    return h.open("CD"), tag


def _row_flags(h, sid, stu="9994A001"):
    return json.loads(h.db_rows(sid)[stu]["flags"])


def _checkin_ok_events(h, sid, stu="9994A001"):
    with h.A.db() as con:
        return con.execute("SELECT * FROM events WHERE session_id=? AND kind='checkin_ok' AND student_pk=?",
                           (sid, h.pk("CD", stu))).fetchall()


@pytest.mark.parametrize("status,label", [("excused", "請假"), ("absent", "缺席")], ids=["excused", "absent"])
def test_manual_off_then_bound_device_gets_form_and_flag(h, status, label):
    sid, dev = _bound_next_session(h, f"dev35-{status}")
    h.T.post(f"/api/t/session/{sid}/status", data={"pk": h.pk("CD", "9994A001"), "status": status})
    r = h.dev(dev).get(_entry_url(h, sid), headers={"user-agent": "iPhone"})
    assert r.status_code == 200, r.status_code
    assert "這一場已經簽到了" not in r.text and "GRANT=" in r.text, r.text[:400]
    j = _land_submit(h, sid, dev, "9994A001")
    assert j["ok"] is True and j["again"] is True and j["status"] == label, j
    assert "qr_after_manual" in _row_flags(h, sid)
    assert len(_checkin_ok_events(h, sid)) == 1


def test_spot_absent_then_same_device_scans_again_keeps_absent_with_flag(h):
    sid = h.open("CD")
    dev = "dev35-spot"
    assert h.checkin(sid, dev, "9994A001", name="學生1", pin="1234")["ok"]
    pick = h.T.post(f"/api/t/session/{sid}/spotcheck").json()["picks"][0]
    assert h.T.post(f"/api/t/spotcheck/{pick['id']}", data={"present": "0"}).json()["ok"]
    row = h.db_rows(sid)["9994A001"]
    assert row["status"] == "absent" and row["source"] == "qr" and "spot_absent" in json.loads(row["flags"])
    r = h.dev(dev).get(_entry_url(h, sid), headers={"user-agent": "iPhone"})
    assert "這一場已經簽到了" not in r.text and "GRANT=" in r.text
    j = _land_submit(h, sid, dev, "9994A001")
    assert j["ok"] is True and j["again"] is True and j["status"] == "缺席", j
    row = h.db_rows(sid)["9994A001"]
    assert row["status"] == "absent"
    assert "qr_after_manual" in json.loads(row["flags"])


def test_manual_absent_then_new_device_correct_pin_409_and_flag(h):
    s2, a, b = _prior_week_bound_pair(h, "dev35n1")
    h.T.post(f"/api/t/session/{s2}/status", data={"pk": h.pk("CD", "9994A001"), "status": "absent"})
    r = _post_checkin(h, s2, b, "9994A001", name="學生1", pin="1234", pin2="1234")
    assert r.status_code == 409, r.text
    assert "已被老師記為「缺席」" in r.json()["error"], r.text
    assert "qr_after_manual" in _row_flags(h, s2)
    assert _student_device(h, "9994A001") == _dh(h, a)
    with h.A.db() as con:
        n = con.execute("SELECT COUNT(*) FROM events WHERE session_id=? AND kind='checkin_reject' AND reason='already_recorded'",
                        (s2,)).fetchone()[0]
    assert n == 1, "Reject 例外已經會記事件，不能再手動多記一筆"


def test_manual_present_then_new_device_keeps_old_409_text_and_no_flag(h):
    s2, a, b = _prior_week_bound_pair(h, "dev35n2")
    h.T.post(f"/api/t/session/{s2}/status", data={"pk": h.pk("CD", "9994A001"), "status": "present"})
    r = _post_checkin(h, s2, b, "9994A001", name="學生1", pin="1234", pin2="1234")
    assert r.status_code == 409, r.text
    assert "這個學號這一場已經有紀錄（出席）" in r.json()["error"], r.text
    assert "已被老師記為" not in r.json()["error"]
    assert "qr_after_manual" not in _row_flags(h, s2)


def test_signed_present_bound_device_rescan_still_shows_banner(h):
    sid = h.open("CD")
    dev = "dev35-reg"
    assert h.checkin(sid, dev, "9994A001", name="學生1", pin="1234")["ok"]
    r = h.dev(dev).get(_entry_url(h, sid), headers={"user-agent": "iPhone"})
    assert r.status_code == 200 and "這一場已經簽到了" in r.text, r.text[:400]


def test_template_receipt_text(h):
    from pathlib import Path
    text = (Path(h.A.__file__).parent / "templates" / "student.html").read_text(encoding="utf-8")
    assert "'老師已把你這次記為「'+j.status+'」。你人在教室的話，現在舉手讓老師改。'" in text


# 延長出席窗的請求晚到（60 秒寬限）時，空檔裡被記遲到的人改回出席

def test_extend_open_within_grace_turns_gap_latecomers_back_to_present(h):
    with time_machine.travel(h.A.now(), tick=False) as tr:
        sid = h.open("CD")
        with h.A.db() as con:
            open_until = h.A.get_session(con, sid)["open_until"]
        tr.move_to(open_until + 3)  # 出席窗結束後 3 秒
        j = _land_submit(h, sid, "dev36-jia", "9994A001", name="學生1", pin="1111", pin2="1111")
        assert j["status"] == "遲到", j
        assert "was_late" in json.loads(h.db_rows(sid)["9994A001"]["flags"])
        with h.A.db() as con:  # 老師手動記成遲到的列（source='manual'）不受影響，連旗標都留著
            pk4 = h.pk("CD", "9994A004")
            con.execute("INSERT INTO checkins(session_id,student_pk,ts,status,source,flags) VALUES(?,?,?,?,?,?)",
                        (sid, pk4, h.A.now(), "late", "manual", json.dumps(["manual", "was_late"])))
        tr.shift(10)
        r = h.T.post(f"/api/t/session/{sid}/extend", data={"minutes": 2, "target": "open"})
        assert r.status_code == 200 and r.json()["phase"] == "open", r.text
        rows = h.db_rows(sid)
        assert rows["9994A001"]["status"] == "present", rows["9994A001"]
        assert "was_late" not in json.loads(rows["9994A001"]["flags"])
        assert rows["9994A004"]["status"] == "late" and "was_late" in json.loads(rows["9994A004"]["flags"])
        j2 = _land_submit(h, sid, "dev36-yi", "9994A002", name="學生2", pin="2222", pin2="2222")
        assert j2["status"] == "出席", j2


def test_extend_open_pending_row_loses_was_late_but_keeps_status(h):
    with time_machine.travel(h.A.now(), tick=False) as tr:
        sid = h.open("CD")
        with h.A.db() as con:
            open_until = h.A.get_session(con, sid)["open_until"]
        tr.move_to(open_until + 3)
        assert _land_submit(h, sid, "dev36-pend", "9994A001", name="學生1", pin="1111", pin2="1111")["status"] == "遲到"
        with h.A.db() as con:
            con.execute("UPDATE checkins SET status='pending' WHERE session_id=?", (sid,))
        tr.shift(10)
        assert h.T.post(f"/api/t/session/{sid}/extend", data={"minutes": 2, "target": "open"}).status_code == 200
        row = h.db_rows(sid)["9994A001"]
        assert row["status"] == "pending" and "was_late" not in json.loads(row["flags"]), row


def test_extend_open_after_grace_keeps_latecomer_late(h):
    with time_machine.travel(h.A.now(), tick=False) as tr:
        sid = h.open("CD")
        with h.A.db() as con:
            open_until = h.A.get_session(con, sid)["open_until"]
        tr.move_to(open_until + 3)
        assert _land_submit(h, sid, "dev36-old", "9994A001", name="學生1", pin="1111", pin2="1111")["status"] == "遲到"
        tr.move_to(open_until + 70)  # 超過 60 秒寬限
        r = h.T.post(f"/api/t/session/{sid}/extend", data={"minutes": 2, "target": "open"})
        assert r.status_code == 200 and r.json()["phase"] == "late", r.text
        row = h.db_rows(sid)["9994A001"]
        assert row["status"] == "late" and "was_late" in json.loads(row["flags"]), row


# 已簽到列路徑的重設底線
# compute_stuck 的 checked 迴圈（c_floor 取 _last_unbind_ts 與 3 分鐘窗 floor）要有測試：
# 否則把 'unbind' 改大寫、拿掉 floor 都測不出來。時間用 time_machine，不 sleep。
def test_checked_row_conflict_resets_at_unbind_and_expires_after_window(h):
    def conflicts():
        return [x for x in _stuck_for(h, sid, "9994A001") if x["conflict"]]

    with time_machine.travel(h.A.now(), tick=False) as tr:
        sid = h.open("CD")
        assert h.checkin(sid, "dev37c-A", "9994A001", name="學生1", pin="1234")["status"] == "出席"  # A 掃碼出席
        tr.shift(1)  # 拒絕事件的 ts 要嚴格大於簽到列的 ts 才算「簽到之後」
        for bad in ("1111", "2222"):  # 另一支裝置用 A 的學號錯 PIN 兩次（兩組不同的 PIN，避開 60 秒去重）
            assert not h.checkin(sid, "dev37c-B", "9994A001", name="學生1", pin=bad)["ok"]
        assert len(conflicts()) == 1, _stuck_for(h, sid, "9994A001")  # 卡住名單出現 A 的紅字衝突

        tr.shift(1)
        r = h.T.post(f"/api/t/student/{h.pk('CD', '9994A001')}/unbind", data={"reset_pin": "1"})  # 老師對 A 按「重設」
        assert r.status_code == 200 and r.json()["reset"] is True, r.text
        assert _stuck_for(h, sid, "9994A001") == [], "重設之前的拒絕不該再算（c_floor 要取最後一次 unbind 的時間）"

        # 重設後學生沒有 PIN：B 先重新設一組新 PIN，之後別的裝置才會打錯
        tr.shift(1)
        assert h.checkin(sid, "dev37c-B", "9994A001", name="學生1", pin="3333")["ok"]
        # 規格沒寫這步的細節：老師處理後 10 分鐘內的 PIN 類拒絕是灰字，所以等超過 600 秒再錯，才會是紅字
        tr.shift(601)
        for bad in ("5555", "6666"):
            assert not h.checkin(sid, "dev37c-C", "9994A001", name="學生1", pin=bad)["ok"]
        assert len(conflicts()) == 1, _stuck_for(h, sid, "9994A001")  # 再錯兩次，紅字又出現

        tr.shift(181)  # 3 分鐘窗（compute_stuck 的 floor＝now-180）
        assert _stuck_for(h, sid, "9994A001") == [], "超過 3 分鐘的拒絕不該還亮著（c_floor 要含 floor）"


# 灰字的邊界
# 灰字只在「全部」紅字拒絕都落在老師處理後 10 分鐘內時才給（compute_stuck 的 len(fresh)==len(red)）；
# 窗內一筆、窗外一筆要維持紅字。灰字那筆也要帶 pk 與姓名（老師頁的重設鈕靠 pk）。
def test_gray_needs_every_rejection_inside_window_and_carries_pk_and_name(h):
    with time_machine.travel(h.A.now(), tick=False) as tr:
        sid = h.open("CD")
        pk = h.pk("CD", "9994A001")
        assert h.checkin(sid, "dev38s-A", "9994A001", name="學生1", pin="1234")["status"] == "出席"
        tr.shift(1)
        r = h.T.post(f"/api/t/student/{pk}/unbind", data={"reset_pin": "1"})  # 老師按「重設」＝handled
        assert r.status_code == 200 and r.json()["reset"] is True, r.text
        tr.shift(1)
        assert h.checkin(sid, "dev38s-B", "9994A001", name="學生1", pin="3333")["ok"]  # 重設後重新設 PIN
        tr.shift(548)  # 處理後 549 秒：窗內
        assert not h.checkin(sid, "dev38s-C", "9994A001", name="學生1", pin="5555")["ok"]
        s = _stuck_for(h, sid, "9994A001")
        assert len(s) == 1 and s[0].get("info") and not s[0]["conflict"], s
        assert s[0]["pk"] == pk and s[0]["name"] == "學生1", s[0]
        tr.shift(100)  # 處理後 649 秒：窗外再錯一次（換一組 PIN，避開 60 秒去重）
        assert not h.checkin(sid, "dev38s-C", "9994A001", name="學生1", pin="6666")["ok"]
        s = _stuck_for(h, sid, "9994A001")
        assert any(x["conflict"] for x in s) and not any(x.get("info") for x in s), s
