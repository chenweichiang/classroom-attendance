"""老師端與投影頁摩擦的回歸測試（BUGLOG B004/B025/B026/B027/B031/B032/B033/B034/B035/B036）。
每條指回 docs/FIELD_2026-09-10.md 或 BUGLOG.tsv 對應的一句話根因。"""
import json
import sqlite3
import threading
import time

import pytest
import time_machine


# ───────────────────────── B004/B026：events 表與 log_event ─────────────────────────

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
    """2026-09-21 突變測試發現：既有樣本裡每個 or 條件的各個子字串多半是「跟別的子字串一起出現」，
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


# ───────────────────────── B004：簽到事件記錄不得跟主流程互鎖（database is locked 回歸） ─────────────────────────

def test_checkin_ok_events_do_not_deadlock_main_transaction(h):
    """2026-09-21 實測：api_checkin 早退（already/IntegrityError 分支）還握著 con 的寫入交易時，
    log_event 另開連線去寫 events 會跟它互鎖到 busy timeout（10 秒），e2e 卡住等不到 #receipt。
    連續多次含成功與重複送出（both 走 already 分支）、以及錯誤 PIN（走 reject 分支）都不得變慢或出現
    database is locked；events 記錄的筆數要精確。
    A2：已綁裝置回簽免 PIN，錯 PIN 那次改用另一支未綁定的裝置才吃得到 PIN 驗證。
    2026-09-26 修正：原本斷言「10 輪×3 次請求的總時間 < 6 秒」，但 PIN 雜湊（pbkdf2 600,000 輪）
    在較慢機器上單次就要約 0.45 秒，20 次雜湊（每輪 2 次）就吃掉 9 秒以上，會把單純「機器比較慢」
    誤判成「疑似 database is locked」（見 test_single_request_ceiling_survives_slow_pin_hash 的證明）。
    真正的 bug 特徵是單一請求卡到 busy timeout（10 秒）以上，所以改成量每一次請求的耗時，
    斷言單次最慢 < 5 秒（遠低於 10 秒 busy timeout、又遠高於一次 PIN 雜湊）；總時間不再斷言。"""
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
    """迴歸測試：證明上面那條測試原本的「總時間 < 6 秒」判準會被較慢的 PIN 雜湊拖垮而誤報，
    新的「單次請求 < 5 秒」判準則不受影響。用固定 sleep 取代真實運算時間的差異來源
    （不是在猜計時，是直接控制耗時本身，這裡就是在測計時判準的行為，符合例外）：
    每次 hash_pin 呼叫多花 0.37 秒，模擬 BUGLOG 記錄的較慢機器單次雜湊約 0.45 秒；
    10 輪×每輪最多 2 次雜湊呼叫，20 次 × 0.37 秒 = 7.4 秒，必定推過舊門檻的 6 秒，
    但單次請求的雜湊次數固定是 1 次，遠低於新門檻的 5 秒——不會是巧合，是刻意設計的。
    2026-09-26 執行證明：把這裡的斷言暫時換回舊寫法（`assert elapsed < 6`）跑過一次，
    在同樣的 monkeypatch 下量到 9.38 秒、確實失敗，證實舊判準會誤報。"""
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


# ───────────────────────── B004/B026：/api/t/session/{sid}/state 的 funnel 與 stuck ─────────────────────────

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
    # A2：fdev1 已綁定，回簽免 PIN；錯 PIN 這筆改用另一支未綁定的裝置才吃得到 PIN 驗證
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


def test_stuck_lists_student_after_two_rejects(h):
    """A2：已綁裝置回簽免 PIN，錯 PIN 這段改用另一支未綁定的裝置（stuck 判定只看 student_pk，不看裝置）。"""
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
    """A2：已綁裝置回簽免 PIN，錯 PIN 這段改用另一支未綁定的裝置；之後用本人已綁定的裝置成功簽到。"""
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


# ───────────────────────── B004/B026：/api/beacon ─────────────────────────

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


# ───────────────────────── B032：QR 靜區 ≥4 模組 ─────────────────────────

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


# ───────────────────────── B027：自動結案成功率過低不自動發 Discord ─────────────────────────

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
    """B041：暫緩之後老師要看得到、手動發完要消掉。後端回了 discord_held 但頁面沒接＝老師不知道名單沒發。"""
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
    """出席／遲到／待確認／缺席各至少一人時的精確出席率（2026-09-21 突變測試發現：
    既有測試沒有同時含 late 與 pending 的場次，也沒有斷言過 excused 預設值為 0 的精確結果，
    ok 元組裡的 late/pending 被換掉、或 excused 的 counts.get 預設值被改成 1，測試都測不出來）。"""
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


def test_manual_close_always_sends_regardless_of_rate(h, monkeypatch):
    """老師手動按「結束點名」行為照舊：不看成功率，一律照 discord_channel 設定發送。"""
    sent = []
    monkeypatch.setattr(h.A, "discord_post", lambda ch, c: (sent.append((ch, c)) or (True, "HTTP 200")))
    h.A.DISCORD_TOKEN = "x"
    with h.A.db() as con:
        con.execute("UPDATE courses SET settings=? WHERE id=?",
                    (json.dumps({"discord_channel": "123456789012345"}), h.cid["CD"]))
    sid = h.open("CD")  # 0 人出席，遠低於門檻
    r = h.T.post(f"/api/t/session/{sid}/close")
    assert r.json()["discord"] is True
    assert len(sent) == 1


# ───────────────────────── B036：首綁 ≥5 人之後 same_fp／bind_burst 都不再標 ─────────────────────────

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


# ───────────────────────── B031：老師頁手動簽到改用 /status 端點，接受學號後 3 碼與姓名片段（頁面邏輯在 e2e-teacher 測） ─────────────────────────

def test_status_endpoint_still_used_for_manual_checkin(h):
    """B031 前端改成候選清單直接呼叫 /status；後端端點本身不變，這裡守住它沒被改壞。"""
    sid = h.open("CD")
    pk = h.pk("CD", "9994A002")
    r = h.T.post(f"/api/t/session/{sid}/status", data={"pk": pk, "status": "present"})
    assert r.json()["ok"] is True
    row = h.row(sid, "9994A002")
    assert row["status"] == "present"


# ───────────────────────── B030：綁定場次（共用定義＋新場次種類） ─────────────────────────

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


def test_A4_variant_name_first_bind_flagged(h):
    """A4／C-S1：姓名寫法不同的首綁要記到 name_variant 旗標（這條原只在 test_friction.py
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


def test_C_S2_bind_session_spotcheck_now_enabled(h):
    """C-S2（SPEC 2026-09-21）：B030「抽點在綁定場次停用」作廢，改為開放；候選人規則不變。
    舊測試名叫 test_bind_session_spotcheck_disabled，斷言 409——這裡改成期望能抽到人，
    對應 SPEC C-S2「api_spotcheck 拿掉綁定場次的 409」。"""
    sid = h.open("CD", kind="bind")
    h.checkin(sid, "binddev5", "9994A001", name="學生1", pin="5555")
    r = h.T.post(f"/api/t/session/{sid}/spotcheck")
    assert r.status_code == 200, r.text
    picks = r.json()["picks"]
    assert len(picks) == 1 and picks[0]["student_id"] == "9994A001", picks


def test_C_S2_bind_session_spotcheck_absent_revokes_binding(h):
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


@pytest.mark.parametrize("rebind_dev", ["proxydev", "freshcookiedev"], ids=["同一個cookie", "換一個cookie"])
def test_B060_no_rebind_in_same_bind_session_after_spot_absent(h, rebind_dev):
    """B060（使用者 2026-09-21 裁定：本場不能再綁）：綁定場次抽點判不在、綁定被撤回之後，代綁者用同一支
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


def test_B060_revoked_student_can_bind_in_next_session(h):
    """本場擋下不是永久封鎖：下一場（老師打開首次綁定的一般場次）本人照常可以綁。"""
    sid = _revoke_in_bind_session(h)
    h.T.post(f"/api/t/session/{sid}/close")
    sid2 = h.open("CD")
    j = h.checkin(sid2, "ownerdev", "9994A001", name="學生1", pin="1234")
    assert j["ok"] is True and j["first_time"] is True, j
    assert h.row(sid2, "9994A001")["bound"] is True


def test_B060_other_students_unaffected_in_same_bind_session(h):
    """只擋被判不在場的那個學號，同一場其他人的首綁不受影響。"""
    sid = _revoke_in_bind_session(h)
    j = h.checkin(sid, "otherdev", "9994A002", name="學生2", pin="2222")
    assert j["ok"] is True and j["first_time"] is True, j


def test_C_S2_normal_session_spotcheck_absent_does_not_unbind(h):
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


# ───────────────────────── B028：本場作廢但保留＋自動缺席整批更正 ─────────────────────────

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
    # C-T5：整批更正拿掉「缺席」這個目標狀態（absent 現在直接 400），改用仍合法的 present 驗證冪等
    r3 = h.T.post(f"/api/t/session/{sid}/bulk-status", data={"to": "present"})
    assert r3.status_code == 200 and r3.json()["n"] == 0
    row1_again = h.row(sid, "9994A001")
    assert row1_again["status"] == "present", "第二次整批更正不該把已經處理過的人改回缺席"


def test_bulk_status_rejects_invalid_status(h):
    sid = h.open("CD")
    h.T.post(f"/api/t/session/{sid}/close")
    r = h.T.post(f"/api/t/session/{sid}/bulk-status", data={"to": "pending"})
    assert r.status_code == 400, r.text


# ───────────────────────── C-T1（SPEC 2026-09-21）：通知的守門 ─────────────────────────

def test_C_T1_notify_rejects_bind_session(h):
    sid = h.open("CD", kind="bind")
    h.checkin(sid, "notifybind1", "9994A001", name="學生1", pin="2222")
    h.T.post(f"/api/t/session/{sid}/close")
    r = h.T.post(f"/api/t/session/{sid}/notify")
    assert r.status_code == 409, r.text
    assert "綁定" in r.json()["error"], r.text


def test_C_T1_notify_rejects_voided_session(h):
    sid = h.open("CD")
    h.checkin(sid, "notifyvoid1", "9994A001", name="學生1", pin="3333")
    h.T.post(f"/api/t/session/{sid}/close")
    r0 = h.T.post(f"/api/t/session/{sid}/void", data={"void": "1"})
    assert r0.status_code == 200, r0.text
    r = h.T.post(f"/api/t/session/{sid}/notify")
    assert r.status_code == 409, r.text
    assert "作廢" in r.json()["error"], r.text


def test_C_T1_void_clears_discord_held_note(h, monkeypatch):
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
    r = h.T.post(f"/api/t/session/{sid}/void", data={"void": "1"})
    assert r.status_code == 200, r.text
    with h.A.db() as con:
        row2 = con.execute("SELECT note FROM sessions WHERE id=?", (sid,)).fetchone()
    assert row2["note"] == "", "作廢已結案且暫緩發送的場次，note 不該再留 discord_held"


def test_C_T1_notify_runs_finalize_expired_first(h, monkeypatch):
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


# ───────────────────────── C-T2（SPEC 2026-09-21）：綁定場次不接受手動改狀態 ─────────────────────────

def test_C_T2_manual_status_rejected_for_existing_row_in_bind_session(h):
    sid = h.open("CD", kind="bind")
    h.checkin(sid, "t2dev1", "9994A001", name="學生1", pin="1234")
    r = h.T.post(f"/api/t/session/{sid}/status", data={"pk": h.pk("CD", "9994A001"), "status": "absent"})
    assert r.status_code == 409, r.text
    assert "另開一般點名" in r.json()["error"], r.text


def test_C_T2_manual_checkin_rejected_for_bind_session(h):
    """手動簽到走的是同一支端點，一個從沒簽過的學號也該被擋。"""
    sid = h.open("CD", kind="bind")
    r = h.T.post(f"/api/t/session/{sid}/status", data={"pk": h.pk("CD", "9994A002"), "status": "present"})
    assert r.status_code == 409, r.text


def test_C_T2_status_unaffected_for_normal_session(h):
    """對照組：一般場次手動改狀態完全不變。"""
    sid = h.open("CD")
    r = h.T.post(f"/api/t/session/{sid}/status", data={"pk": h.pk("CD", "9994A001"), "status": "present"})
    assert r.status_code == 200, r.text


def test_C_T2_state_reports_bound_n_for_bind_session(h):
    sid = h.open("CD", kind="bind")
    h.checkin(sid, "t2dev2", "9994A001", name="學生1", pin="1111")
    h.checkin(sid, "t2dev3", "9994A002", name="學生2", pin="2222")
    st = h.state(sid)
    assert st["bound_n"] == 2, st


# ───────────────────────── C-T3（SPEC 2026-09-21）：同課同時只能一場未結案 ─────────────────────────

def test_C_T3_concurrent_open_only_one_live_session(A):
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


def test_C_T3_open_survives_losing_finalize_race(A):
    """B059：finalize_expired 的 SELECT 讀到過期場次之後、UPDATE 之前，另一個輪詢（投影頁／老師頁）
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


# ───────────────────────── C-T4（SPEC 2026-09-21）：已有未結案場次而種類不同 ─────────────────────────

def test_C_T4_open_different_kind_while_live_shows_error(h):
    h.open("CD", kind="bind")
    r = h.T.post(f"/t/course/{h.cid['CD']}/open", data={"kind": "normal"}, follow_redirects=False)
    assert r.status_code == 409, r.text
    assert "綁定場次" in r.text and "先結束" in r.text, r.text
    with h.A.db() as con:
        rows = [(row["id"], row["kind"]) for row in con.execute(
            "SELECT id, kind FROM sessions WHERE course_id=? AND closed_at IS NULL", (h.cid["CD"],))]
    assert len(rows) == 1, f"不該新建也不該沿用，資料庫應仍只有那一場綁定場次：{rows}"


def test_C_T4_open_same_kind_while_live_reuses_session(h):
    """對照組：種類相同照舊沿用（連點防護）。"""
    sid1 = h.open("CD", kind="bind")
    r = h.T.post(f"/t/course/{h.cid['CD']}/open", data={"kind": "bind"}, follow_redirects=False)
    assert r.status_code == 303, r.text
    sid2 = int(r.headers["location"].rsplit("/", 1)[1])
    assert sid1 == sid2


# ───────────────────────── C-T5（SPEC 2026-09-21）：整批更正拿掉「缺席」 ─────────────────────────

def test_C_T5_bulk_status_rejects_absent(h):
    sid = h.open("CD")
    h.T.post(f"/api/t/session/{sid}/close")
    r = h.T.post(f"/api/t/session/{sid}/bulk-status", data={"to": "absent"})
    assert r.status_code == 400, r.text


def test_C_T5_bulk_status_still_accepts_present_late_excused(h):
    sid = h.open("CD")
    h.T.post(f"/api/t/session/{sid}/close")
    for to in ("present", "late", "excused"):
        r = h.T.post(f"/api/t/session/{sid}/bulk-status", data={"to": to})
        assert r.status_code == 200, (to, r.text)


# ───────────────────────── C-T6（SPEC 2026-09-21）：綁定場次的首綁開關顯示 ─────────────────────────

def test_C_T6_state_forces_allow_bind_true_for_bind_session(h):
    h.T.post(f"/api/t/course/{h.cid['CD']}/bind", data={"allow": "0"})
    sid = h.open("CD", kind="bind")
    st = h.state(sid)
    assert st["allow_bind"] is True, st
    assert st["bind_forced"] is True, st


def test_C_T6_state_bind_forced_false_for_normal_session(h):
    sid = h.open("CD")
    st = h.state(sid)
    assert st["bind_forced"] is False, st


# ───────────────────────── C-T7（SPEC 2026-09-21）：bind_minutes 進設定頁 ─────────────────────────

def test_C_T7_save_settings_accepts_bind_minutes(h):
    r = h.T.post(f"/t/course/{h.cid['CD']}/settings", data={
        "open_minutes": 3, "late_minutes": 15, "qr_interval": 10, "qr_grace": 30,
        "spot_n": 3, "bind_minutes": 12}, follow_redirects=False)
    assert r.status_code == 303, r.text
    with h.A.db() as con:
        c = h.A.get_course(con, h.cid["CD"])
    assert h.A.course_settings(c)["bind_minutes"] == 12


def test_C_T7_save_settings_clamps_bind_minutes_to_3_30(h):
    r = h.T.post(f"/t/course/{h.cid['CD']}/settings", data={
        "open_minutes": 3, "late_minutes": 15, "qr_interval": 10, "qr_grace": 30,
        "spot_n": 3, "bind_minutes": 999}, follow_redirects=False)
    assert r.status_code == 303, r.text
    with h.A.db() as con:
        c = h.A.get_course(con, h.cid["CD"])
    assert h.A.course_settings(c)["bind_minutes"] == 30


def test_C_T7_students_page_has_bind_minutes_field(h):
    resp = h.T.get(f"/t/course/{h.cid['CD']}/students")
    assert 'name="bind_minutes"' in resp.text, resp.text
    assert 'min="3"' in resp.text and 'max="30"' in resp.text, resp.text


# ───────────────────────── C-T8（SPEC 2026-09-21）：說明文字 ─────────────────────────

def test_C_T8_home_page_mentions_bind_first_step_and_exceptions(h):
    resp = h.T.get("/t")
    assert "只做綁定" in resp.text
    assert "第一週留五分鐘" not in resp.text, "舊句子應被改掉"
    assert "抽點" in resp.text and "結束點名" in resp.text and "Discord" in resp.text


def test_C_T8_report_page_mentions_excluded_sessions(h):
    resp = h.T.get(f"/t/course/{h.cid['CD']}/report")
    assert "不含綁定場次與已作廢場次" in resp.text, resp.text


# ───────────────────────── C-S6（SPEC 2026-09-21）：多數人不走教室網路時，靜音 ip_differs ─────────────────────────


def test_C_S6_majority_off_campus_mutes_ip_differs(h):
    """3 人全走各自行動網路（都跟教室 IP 不同）→ state 沒有 ip_differs、ip_check_muted 為真，
    但資料庫裡原始旗標仍在（CSV 匯出照舊）。"""
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


def test_C_S6_minority_off_campus_still_shows_flag(h):
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
