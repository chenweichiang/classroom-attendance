"""老師端前端回歸（重設鈕、IP 不顯示、卡住名單版面、老師頁名單輪詢）。
被 tests/e2e_teacher_friction_playwright.py 結尾以子程序呼叫（同 e2e_teacher_roster.py 的手法），也可以獨立跑。
前提同該檔：乾淨庫、ATTEND_TEST_BASE、NTUB-CD 名冊（姓名「測試N」，學號 9994A0NN）。
本檔用到的學號：9994A009、A010、A011–A017、A019、A025、A026、A044（避開同一層其他腳本用的號碼）。
輸入行為用 tap＋keyboard.type，不用 fill()。
"""
import os, re, sys
from playwright.sync_api import sync_playwright

B = os.environ.get("ATTEND_TEST_BASE", "http://127.0.0.1:64890")
PW = os.environ.get("ATTEND_TEACHER_PASSWORD", "pw")
FAIL = []


def check(cond, msg):
    print(("ok   " if cond else "FAIL "), msg)
    if not cond:
        FAIL.append(msg)


def close_all_live(req):
    html = req.get(f"{B}/t").text()
    for s in set(re.findall(r"/t/session/(\d+)", html)):
        req.post(f"{B}/api/t/session/{s}/close")


def open_session(req, cid, kind="normal"):
    r = req.post(f"{B}/t/course/{cid}/open", form={"kind": kind})
    return int(re.search(r"/t/session/(\d+)", r.url).group(1))


def scan_checkin(browser, treq, sid, stu, name, pin="1234", ip=None):
    """學生掃碼簽到（一個一次性 context ＝ 一支手機）。"""
    ctx = browser.new_context()
    try:
        code = treq.get(f"{B}/api/t/session/{sid}/qr").json()["code"]
        hd = {"cf-connecting-ip": ip} if ip else {}
        html = ctx.request.post(f"{B}/c", form={"code": code}, headers=hd).text()
        grant = re.search(r'GRANT="([^"]+)"', html).group(1)
        return ctx.request.post(f"{B}/api/checkin", form={"grant": grant, "student_id": stu, "name": name, "pin": pin, "pin2": pin, "hints": "h"}, headers=hd).json()
    finally:
        ctx.close()


def wrong_pins(browser, treq, sid, stu, pins=("1111", "2222")):
    for wrong in pins:
        ctx = browser.new_context()
        code = treq.get(f"{B}/api/t/session/{sid}/qr").json()["code"]
        html = ctx.request.post(f"{B}/c", form={"code": code}).text()
        g = re.search(r'GRANT="([^"]+)"', html).group(1)
        ctx.request.post(f"{B}/api/checkin", form={"grant": g, "student_id": stu, "name": "", "pin": wrong, "pin2": "", "hints": "h"})
        ctx.close()


def teacher_page(browser, kwargs, storage):
    ctx = browser.new_context(**kwargs, storage_state=storage)
    return ctx, ctx.new_page()


def run_reset_button(p, chrome, webkit, treq, storage, cid):
    # 一般場次：卡住名單旁的「重設 PIN」→ 學生重掃 → 首綁表單 → 設新 PIN → 收據
    close_all_live(treq)
    s0 = open_session(treq, cid)
    check(scan_checkin(chrome, treq, s0, "9994A009", "測試9", pin="1234").get("ok") is True, "D1：前置，9994A009 綁定")
    check(scan_checkin(chrome, treq, s0, "9994A010", "測試10", pin="1234").get("ok") is True, "D1：前置，9994A010 綁定")
    treq.post(f"{B}/api/t/session/{s0}/close")
    sid = open_session(treq, cid)
    wrong_pins(chrome, treq, sid, "9994A009")
    ctx, pg = teacher_page(chrome, dict(viewport={"width": 1280, "height": 900}), storage)
    dlg = []
    pg.on("dialog", lambda d: (dlg.append(d.message), d.accept()))
    try:
        pg.goto(f"{B}/t/session/{sid}")
        pg.wait_for_selector("#stuck .stuckpick", timeout=15000)
        btn = pg.locator("#stuck button", has_text="重設 PIN")
        check(btn.count() == 1, f"D1：卡住名單的人旁有「重設 PIN」鈕，實得 {btn.count()}")
        btn.click()
        pg.wait_for_function("document.getElementById('mdone').innerText.includes('已重設')", timeout=8000)
        check(dlg == ["重設 9994A009 測試9 的 PIN？他要重新掃 QR，照畫面設一組新的。"], f"D1：確認框字句，實得 {dlg}")
        check(pg.locator("#mdone").inner_text() == "已重設 9994A009 測試9，請他重新掃 QR", f"D1：回饋行，實得 {pg.locator('#mdone').inner_text()!r}")
        # 遮姓名：確認框與回饋行姓名照遮蔽開關
        pg.evaluate("localStorage.setItem('attend_mask_names','1')")
        wrong_pins(chrome, treq, sid, "9994A010")
        pg.reload(); pg.wait_for_selector("#stuck .stuckpick", timeout=15000)
        dlg.clear()
        pg.locator("#stuck button", has_text="重設 PIN").click()
        pg.wait_for_function("document.getElementById('mdone').innerText.includes('已重設')", timeout=8000)
        check(dlg and "測○○" in dlg[0] and "測試10" not in dlg[0], f"D1：遮姓名開啟時確認框遮姓名，實得 {dlg}")
        pg.evaluate("localStorage.removeItem('attend_mask_names')")
        # 學生重掃：首綁表單，設新 PIN，拿到收據
        sctx = webkit.new_context(**dict(p.devices["iPhone 13"]))
        sp = sctx.new_page()
        sp.goto(f"{B}/c")
        sp.click("#otp input >> nth=0")
        sp.keyboard.type(treq.get(f"{B}/api/t/session/{sid}/qr").json()["code"])
        sp.wait_for_selector("#student_id, #returnFields")
        sp.tap("#student_id"); sp.keyboard.type("9994A009")
        sp.tap("#name"); sp.keyboard.type("測試9")
        sp.tap("#pin1"); sp.keyboard.type("7777"); sp.tap("#pin2"); sp.keyboard.type("7777")
        sp.click("#go")
        try:
            sp.wait_for_selector("#receipt", state="visible", timeout=8000); got = True
        except Exception:
            got = False
        check(got, f"D1：重設後學生重掃、首綁表單設新 PIN 拿到收據，訊息 {sp.locator('#err').inner_text()!r}")
        sctx.close()
    except Exception as e:
        check(False, f"D1：重設鈕（一般場次）拋例外 {e!r}")
    finally:
        ctx.close()
        treq.post(f"{B}/api/t/session/{sid}/close")

    # 綁定場次也顯示這顆鈕，回饋行看得到
    close_all_live(treq)
    sb = open_session(treq, cid, kind="bind")
    wrong_pins(chrome, treq, sb, "9994A010", pins=("3333", "4444"))
    ctx, pg = teacher_page(chrome, dict(viewport={"width": 1280, "height": 900}), storage)
    pg.on("dialog", lambda d: d.accept())
    try:
        pg.goto(f"{B}/t/session/{sb}")
        pg.wait_for_selector("#stuck", state="visible", timeout=15000)
        btn = pg.locator("#stuck button", has_text="重設 PIN")
        check(btn.count() >= 1, f"D1：綁定場次的卡住名單也有「重設 PIN」鈕，實得 {btn.count()}")
        btn.first.click()
        pg.wait_for_function("document.body.innerText.includes('已重設 9994A010')", timeout=8000)
        check(pg.locator("#stuckdone").is_visible(), "D1：綁定場次（手動簽到區隱藏）的回饋行看得到")
    except Exception as e:
        check(False, f"D1：重設鈕（綁定場次）拋例外 {e!r}")
    finally:
        ctx.close()
        treq.post(f"{B}/api/t/session/{sb}/close")


def ensure_ip_check(treq, cid):
    """前面的腳本可能關過這門課的 IP 比對；這條要驗「資料照存、畫面不顯示」，先把它開回來（其餘設定值照原樣送回）。"""
    html = treq.get(f"{B}/t/course/{cid}/students").text()
    form = {k: re.search(rf'name="{k}"[^>]*value="([^"]*)"', html).group(1)
            for k in ("open_minutes", "late_minutes", "qr_interval", "qr_grace", "spot_n", "bind_minutes")}
    ch = re.search(r'name="discord_channel" value="([^"]*)"', html)
    form["discord_channel"] = ch.group(1) if ch else ""
    form["ip_check"] = "1"
    treq.post(f"{B}/t/course/{cid}/settings", form=form)


def run_ip_hidden(chrome, treq, storage, cid):
    ensure_ip_check(treq, cid)
    close_all_live(treq)
    sid = open_session(treq, cid)
    treq.post(f"{B}/api/t/session/{sid}/teacher-ip", headers={"cf-connecting-ip": "1.1.1.1"})
    j = scan_checkin(chrome, treq, sid, "9994A044", "測試44", ip="9.9.9.9")
    check(j.get("ok") is True, f"D4：前置，9994A044 從別的 IP 簽到：{j}")
    row = next(r for r in treq.get(f"{B}/api/t/session/{sid}/state").json()["rows"] if r["student_id"] == "9994A044")
    check("ip_differs" in row["flags"] and row["ip"] == "9.9.9.9", f"D4：state 仍帶 ip_differs 與 IP（資料在），實得 {row['flags']} {row['ip']!r}")
    ctx, pg = teacher_page(chrome, dict(viewport={"width": 1280, "height": 900}), storage)
    try:
        pg.goto(f"{B}/t/session/{sid}")
        pg.wait_for_function("state.rows.length>0 && state.rows.find(r=>r.student_id==='9994A044').status==='present'", timeout=15000)
        pg.wait_for_selector("#rows tr")
        txt = pg.locator("#rows").inner_text()
        check("IP" not in txt and "9.9.9.9" not in txt, f"D4：名單不出現「IP」字樣與學生 IP，實得片段 {txt[txt.find('9994A044'):][:80]!r}")
        pg.select_option("#filter", "flag")
        pg.wait_for_function("document.querySelectorAll('#rows tr').length===0 || ![...document.querySelectorAll('#rows tr')].some(t=>t.innerText.includes('9994A044'))")
        check("9994A044" not in pg.locator("#rows").inner_text(), "D4：「有標記」篩選不含只有 ip_differs 的列")
        check("記錄用" in pg.locator("body").inner_text(), "D4：「以這台為教室 IP」的說明改成「記錄用」")
    except Exception as e:
        check(False, f"D4：拋例外 {e!r}")
    finally:
        ctx.close()
        treq.post(f"{B}/api/t/session/{sid}/close")


def run_stuck_list_layout(p, webkit, chrome, treq, storage, cid):
    close_all_live(treq)
    sid = open_session(treq, cid)
    for n in (11, 12, 13, 14, 15, 16, 17, 19, 25, 26):
        scan_checkin(chrome, treq, sid, f"9994A0{n}", f"測試{n}", pin="1234")
    treq.post(f"{B}/api/t/session/{sid}/close")
    kw = dict(p.devices["iPhone 13"]); kw["viewport"] = {"width": 390, "height": 844}
    ctx, pg = teacher_page(webkit, kw, storage)
    try:
        pg.goto(f"{B}/t/course/{cid}/students")
        pg.wait_for_selector("#roster tr[data-q]")
        r = pg.evaluate("""(()=>{const w=innerWidth;const bs=[...document.querySelectorAll('#roster button')];
          const over=bs.filter(b=>b.getBoundingClientRect().right>w+0.5||b.getBoundingClientRect().left<0).length;
          return {w, n:bs.length, over, sw:document.documentElement.scrollWidth}})()""")
        check(r["n"] >= 20 and r["over"] == 0, f"390 寬、有人已綁，每顆按鈕右緣 ≤ 視窗寬，實得 {r}")
        check(r["sw"] <= r["w"], f"頁面沒有橫向溢出，實得 {r}")
        pg.tap("#rq"); pg.keyboard.type("012")
        pg.wait_for_function("[...document.querySelectorAll('#roster tr[data-q]')].filter(t=>t.style.display!=='none').length===1")
        shown = pg.evaluate("[...document.querySelectorAll('#roster tr[data-q]')].filter(t=>t.style.display!=='none').map(t=>t.dataset.q)")
        check(len(shown) == 1 and "9994A012" in shown[0].upper(), f"搜尋打學號後 3 碼只剩符合的列，實得 {shown}")
        pg.evaluate("document.getElementById('rq').select()"); pg.keyboard.press("Backspace")
        pg.wait_for_function("[...document.querySelectorAll('#roster tr[data-q]')].every(t=>t.style.display!=='none')")
        pg.tap("#rq"); pg.keyboard.type("測試26")
        pg.wait_for_function("[...document.querySelectorAll('#roster tr[data-q]')].filter(t=>t.style.display!=='none').length===1")
        check(True, "搜尋姓名也找得到")
    except Exception as e:
        check(False, f"拋例外{e!r}")
    finally:
        ctx.close()


def run_poll(chrome, treq, storage, cid):
    close_all_live(treq)
    sid = open_session(treq, cid)
    ctx, pg = teacher_page(chrome, dict(viewport={"width": 1280, "height": 900}), storage)
    try:
        pg.goto(f"{B}/t/session/{sid}")
        pg.wait_for_selector("#rows tr")
        pg.wait_for_function("!inflight")
        pg.evaluate("document.querySelector('#rows tr').__mark=1")
        seq0 = pg.evaluate("loadSeq")
        pg.wait_for_function(f"loadSeq>={seq0}+2 && !inflight", timeout=15000)  # 資料沒變的兩次輪詢（條件，不是固定秒數）
        check(pg.evaluate("document.querySelector('#rows tr').__mark===1"), "名單輪詢：資料不變時 #rows 的第一個 <tr> 還是同一個 DOM 節點")
    except Exception as e:
        check(False, f"名單輪詢：拋例外 {e!r}")
    finally:
        ctx.close()
        treq.post(f"{B}/api/t/session/{sid}/close")


def main():
    with sync_playwright() as p:
        chrome = p.chromium.launch()
        webkit = p.webkit.launch()
        tctx = chrome.new_context(viewport={"width": 1280, "height": 900})
        r = tctx.request.post(f"{B}/t/login", form={"password": PW, "next": "/t"})
        assert r.ok, r.status
        storage = tctx.storage_state()
        treq = tctx.request
        cid = int(re.search(r"course/(\d+)/open", treq.get(f"{B}/t").text()).group(1))
        run_reset_button(p, chrome, webkit, treq, storage, cid)
        run_ip_hidden(chrome, treq, storage, cid)
        run_stuck_list_layout(p, webkit, chrome, treq, storage, cid)
        run_poll(chrome, treq, storage, cid)
        chrome.close(); webkit.close()


main()
print("\n==>", "全部通過" if not FAIL else f"{len(FAIL)} 項失敗：{FAIL}")
sys.exit(1 if FAIL else 0)
