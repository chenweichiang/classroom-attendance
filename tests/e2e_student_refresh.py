"""學生端前端回歸。
被 tests/e2e_friction_playwright.py 結尾以子程序呼叫（同 e2e_student_input.py 的手法），也可以獨立跑。
前提同該檔：乾淨庫、ATTEND_TEST_BASE、NTUB-CD 名冊（姓名「測試N」，學號 9994A0NN）；老師端只用 API context。
輸入行為一律 tap 欄位後 keyboard.type 逐字打，不用 fill()（理由見 e2e_student_input.py 檔頭）。
本檔用到的學號：9994A009、A011、A014、A015、A017、A019、A020（避開同一層其他腳本用的號碼）。
"""
import os, re, sys
from playwright.sync_api import sync_playwright

B = os.environ.get("ATTEND_TEST_BASE", "http://127.0.0.1:64890")
PW = os.environ.get("ATTEND_TEACHER_PASSWORD", "pw")
FAIL = []
NETERR_MSG = "連線中斷，不確定有沒有送到。請再按一次簽到：已經簽過的話會直接顯示收據。"
RESCAN_MSG = "剛才可能沒送到，請重新掃 QR 再按一次；已經簽過的會直接顯示收據"


def check(cond, msg):
    print(("ok   " if cond else "FAIL "), msg)
    if not cond:
        FAIL.append(msg)


def close_all_live(teacher):
    html = teacher.request.get(f"{B}/t").text()
    for s in set(re.findall(r"/t/session/(\d+)", html)):
        teacher.request.post(f"{B}/api/t/session/{s}/close")


def open_session(teacher, cid):
    r = teacher.request.post(f"{B}/t/course/{cid}/open", form={"kind": "normal"})
    return int(re.search(r"/t/session/(\d+)", r.url).group(1))


def qr_code(teacher, sid):
    return teacher.request.get(f"{B}/api/t/session/{sid}/qr").json()["code"]


def enter(page, code, sel="#student_id, #returnFields"):
    page.goto(f"{B}/c")
    page.click("#otp input >> nth=0")
    page.keyboard.type(code)
    page.wait_for_selector(sel)


def type_into(page, sel, text):
    page.tap(sel)
    page.keyboard.type(text)


def wait_err(page, needle, timeout=8000):
    page.wait_for_function("n=>document.getElementById('err').innerText.includes(n)", arg=needle, timeout=timeout)


def bind_first(browser, device, teacher, cid, sid_no, name, pin):
    """setup：在一個一次性 context 把學號綁上並設 PIN（簽到後關掉 context ＝ 清掉 cookie）。"""
    sid = open_session(teacher, cid)
    ctx = browser.new_context(**device)
    pg = ctx.new_page()
    enter(pg, qr_code(teacher, sid))
    pg.fill("#student_id", sid_no); pg.fill("#name", name); pg.fill("#pin1", pin); pg.fill("#pin2", pin)
    pg.click("#go")
    pg.wait_for_selector("#receipt", state="visible")
    ctx.close()
    teacher.request.post(f"{B}/api/t/session/{sid}/close")


def settled(page):
    """whoami 回來、init() 決定好模式：送出鈕已啟用或已被鎖定停用。"""
    page.wait_for_function("document.getElementById('modeHint').innerText!=='確認這支手機…' && document.getElementById('go').innerText!=='確認這支手機…'", timeout=15000)


def run_refresh_pin_only(webkit, device, teacher, cid):
    # 首綁表單被擋 → 重新整理 → 直接是只輸 PIN（學號已填）→ 打舊 PIN → 收據
    stu, n, old = "9994A011", 11, "1111"
    bind_first(webkit, device, teacher, cid, stu, f"測試{n}", old)
    sid = open_session(teacher, cid)
    ctx = webkit.new_context(**device)
    pg = ctx.new_page()
    try:
        enter(pg, qr_code(teacher, sid))
        type_into(pg, "#student_id", stu); type_into(pg, "#name", f"測試{n}")
        type_into(pg, "#pin1", "5555"); type_into(pg, "#pin2", "5555")
        pg.click("#go")
        wait_err(pg, "已經設過 PIN")
        pg.reload()
        pg.wait_for_function("document.getElementById('modeHint').innerText.includes('已經設過 PIN')", timeout=15000)
        check(not pg.locator("#nameField").is_visible() and not pg.locator("#pin2Field").is_visible(), "D1：重新整理後直接是只輸 PIN（姓名與第二個 PIN 欄藏起來）")
        check(pg.locator("#student_id").input_value() == stu, f"D1：學號已預填，實得 {pg.locator('#student_id').input_value()!r}")
        check(pg.locator("#pin1").input_value() == "", "D1：PIN 欄是空白")
        type_into(pg, "#pin1", old)
        pg.click("#go")
        try:
            pg.wait_for_selector("#receipt", state="visible", timeout=6000)
            got = True
        except Exception:
            got = False
        check(got, f"D1：重新整理後在只輸 PIN 畫面打舊 PIN 拿到收據，訊息 {pg.locator('#err').inner_text()!r}")
    except Exception as e:
        check(False, f"D1：重新整理段拋例外 {e!r}")
    finally:
        ctx.close()
        teacher.request.post(f"{B}/api/t/session/{sid}/close")

    # 達上限後重新整理：直接是鎖定狀態
    stu, n, old = "9994A014", 14, "1414"
    bind_first(webkit, device, teacher, cid, stu, f"測試{n}", old)
    sid = open_session(teacher, cid)
    ctx = webkit.new_context(**device)
    pg = ctx.new_page()
    try:
        enter(pg, qr_code(teacher, sid))
        type_into(pg, "#student_id", stu); type_into(pg, "#name", f"測試{n}")
        type_into(pg, "#pin1", "5555"); type_into(pg, "#pin2", "5555")
        pg.click("#go")
        wait_err(pg, "已經設過 PIN")
        for wrong in ("0001", "0002"):
            type_into(pg, "#pin1", wrong); pg.click("#go")
            wait_err(pg, "PIN 錯誤" if wrong == "0001" else "錯太多次")
        pg.reload()
        pg.wait_for_function("document.getElementById('go').innerText==='這一場請找老師'", timeout=15000)
        check(pg.locator("#go").is_disabled(), "D1：鎖定後重新整理，簽到鈕停用")
        hint = pg.locator("#modeHint").inner_text()
        check("重設" in hint and "重新掃 QR" in hint and "輸對就會改用" not in hint, f"D1：鎖定後重新整理，說明是鎖定與重設的字，實得 {hint!r}")
    except Exception as e:
        check(False, f"D1：鎖定重新整理段拋例外 {e!r}")
    finally:
        ctx.close()
        teacher.request.post(f"{B}/api/t/session/{sid}/close")


def run_whoami_502(webkit, device, teacher, cid):
    sid = open_session(teacher, cid)
    ctx = webkit.new_context(**device)
    pg = ctx.new_page()
    beacons = []
    try:
        pg.route("**/api/whoami", lambda route: route.fulfill(status=502, content_type="text/html", body="<html><body>Bad Gateway</body></html>"))

        def on_beacon(route):
            beacons.append(route.request.post_data or "")
            route.continue_()
        pg.route("**/api/beacon", on_beacon)
        enter(pg, qr_code(teacher, sid), sel="#student_id")
        pg.wait_for_function("document.getElementById('modeHint').innerText.includes('連線不穩')", timeout=15000)
        pg.wait_for_timeout(300)  # 讓 sendBeacon 的請求送出（沒有可輪詢的畫面條件）
        qlen = pg.evaluate("(JSON.parse(localStorage.getItem('attendBeaconQ')||'[]')).length")
        sent = [b for b in beacons if "whoami_fail" in b and "http 502" in b]
        check(len(sent) == 1, f"whoami 回 502 HTML，前端當場送出 whoami_fail（msg 為 http 502），攔到 {beacons!r}")
        check(qlen == 0, f"沒有進 localStorage 佇列，實得 {qlen}")
    except Exception as e:
        check(False, f"拋例外{e!r}")
    finally:
        ctx.close()
        teacher.request.post(f"{B}/api/t/session/{sid}/close")


def run_form_expiry(webkit, device, teacher, cid):
    sid = open_session(teacher, cid)
    ctx = webkit.new_context(**device)
    pg = ctx.new_page()
    try:
        enter(pg, qr_code(teacher, sid))
        type_into(pg, "#student_id", "9994A015"); type_into(pg, "#name", "測試15")
        type_into(pg, "#pin1", "1515"); type_into(pg, "#pin2", "1515")
        pg.route("**/api/checkin", lambda route: route.abort("connectionfailed"))
        pg.click("#go")
        wait_err(pg, "連線中斷")
        check(pg.locator("#err").inner_text() == NETERR_MSG, f"前置，網路錯誤那句，實得 {pg.locator('#err').inner_text()!r}")
        # 等表單時效到（條件：按鈕變成重新掃 QR），不猜固定秒數
        pg.wait_for_function("document.getElementById('go').innerText.includes('重新掃 QR')", timeout=60000)
        check(pg.locator("#err").inner_text() == RESCAN_MSG + "。" or pg.locator("#err").inner_text() == RESCAN_MSG,
              f"表單時效到後紅字換成「剛才可能沒送到…」，實得 {pg.locator('#err').inner_text()!r}")
        check(pg.locator("#go").is_enabled(), "按鈕是可按的重新掃 QR")
    except Exception as e:
        check(False, f"拋例外{e!r}")
    finally:
        ctx.close()
        teacher.request.post(f"{B}/api/t/session/{sid}/close")


def run_keyboard_scroll(p, webkit, teacher, cid):
    device = dict(p.devices["iPhone SE (3rd gen)"])
    stu, n, old = "9994A017", 17, "2468"
    bind_first(webkit, dict(p.devices["iPhone 13"]), teacher, cid, stu, f"測試{n}", old)
    sid = open_session(teacher, cid)
    ctx = webkit.new_context(**device)
    pg = ctx.new_page()
    kb = 260  # 數字鍵盤＋表單輔助列（iOS：版面視窗不變，鍵盤蓋住下半；同 e2e_friction 的 iPhone 假設）
    try:
        enter(pg, qr_code(teacher, sid))
        type_into(pg, "#student_id", stu); type_into(pg, "#name", f"測試{n}")
        type_into(pg, "#pin1", "1357"); type_into(pg, "#pin2", "1357")
        pg.click("#go")
        wait_err(pg, "已經設過 PIN")
        pg.wait_for_timeout(300)  # 捲動動畫沒有可輪詢的條件
        vh = pg.viewport_size["height"]

        def geom():
            return pg.evaluate("(()=>{const e=document.getElementById('err').getBoundingClientRect();const p=document.getElementById('pin1').getBoundingClientRect();return {eb:e.bottom,et:e.top,pt:p.top}})()")
        g = geom()
        check(g["et"] >= 0 and g["eb"] <= vh - kb, f"[pin_exists]#err 底緣 {g['eb']:.0f} 在鍵盤線 {vh - kb} 以上（頂 {g['et']:.0f}）")
        check(g["eb"] <= g["pt"] + 1, f"[pin_exists]#err 在 PIN 欄上方（{g['eb']:.0f} ≤ {g['pt']:.0f}）")
        type_into(pg, "#pin1", "9999"); pg.click("#go")
        wait_err(pg, "PIN 錯誤")
        pg.wait_for_timeout(300)
        g = geom()
        check(g["et"] >= 0 and g["eb"] <= vh - kb, f"[pin_wrong]#err 底緣 {g['eb']:.0f} 在鍵盤線 {vh - kb} 以上")
        # PIN 欄 focus 時全選：上一個還留著的字，再點進去直接蓋掉
        type_into(pg, "#pin1", "12")
        pg.tap("#student_id")
        pg.tap("#pin1")
        sel = pg.evaluate("(()=>{const e=document.getElementById('pin1');return [e.selectionStart,e.selectionEnd,e.value.length]})()")
        check(sel == [0, 2, 2], f"PIN 欄 focus 時全選，實得 {sel}")
    except Exception as e:
        check(False, f"拋例外{e!r}")
    finally:
        ctx.close()
        teacher.request.post(f"{B}/api/t/session/{sid}/close")


def run_keyboard_scroll_second_path(p, webkit, teacher, cid):
    """第二條路徑，新瀏覽器先打錯姓名（名冊不符），改對再送才被告知「已經設過 PIN」。
    切成只輸 PIN 時姓名欄收掉、提示變短，Safari 沒有捲動錨定，錯誤訊息與 PIN 欄曾一起被推出畫面頂端（top 約 -150）。
    只量 top ≥ 0 不夠（只驗「底緣在鍵盤線以上」的話，捲出頂端也會算過），兩個元素都要整個落在 0 到鍵盤線之間。"""
    device = dict(p.devices["iPhone SE (3rd gen)"])
    stu, n = "9994A037", 37
    bind_first(webkit, dict(p.devices["iPhone 13"]), teacher, cid, stu, f"測試{n}", "2468")
    sid = open_session(teacher, cid)
    ctx = webkit.new_context(**device)
    pg = ctx.new_page()
    kb = 260
    try:
        enter(pg, qr_code(teacher, sid))
        type_into(pg, "#student_id", stu); type_into(pg, "#name", "王小明")
        type_into(pg, "#pin1", "1357"); type_into(pg, "#pin2", "1357")
        pg.click("#go")
        pg.wait_for_function("document.getElementById('err').innerText.length>0", timeout=8000)
        pg.wait_for_timeout(300)
        pg.fill("#name", ""); type_into(pg, "#name", f"測試{n}")
        pg.fill("#pin1", ""); type_into(pg, "#pin1", "1357"); pg.fill("#pin2", ""); type_into(pg, "#pin2", "1357")
        pg.click("#go")
        wait_err(pg, "已經設過 PIN")
        pg.wait_for_timeout(300)
        vh = pg.viewport_size["height"]
        g = pg.evaluate("(()=>{const r=id=>{const b=document.getElementById(id).getBoundingClientRect();return [b.top,b.bottom]};return {err:r('err'),pin:r('pin1')}})()")
        for k in ("err", "pin"):
            check(g[k][0] >= 0 and g[k][1] <= vh - kb, f"[先打錯姓名再送]#{k} 整個在 0 到鍵盤線 {vh - kb} 之間，實得 {g[k][0]:.0f}～{g[k][1]:.0f}")
    except Exception as e:
        check(False, f"拋例外{e!r}")
    finally:
        ctx.close()
        teacher.request.post(f"{B}/api/t/session/{sid}/close")


def run_not_me(webkit, device, teacher, cid):
    # 一支手機先綁定（留著 context），再開同一支的新分頁看回簽畫面的「不是我」
    sid = open_session(teacher, cid)
    ctx = webkit.new_context(**device)
    pg = ctx.new_page()
    try:
        enter(pg, qr_code(teacher, sid))
        for label, sel in (("學號", "student_id"), ("姓名", "name"), ("四到六位數字", "pin1"), ("再輸一次 PIN", "pin2")):
            loc = pg.get_by_label(label)
            check(loc.count() == 1, f"get_by_label({label!r}) 找得到唯一欄位，實得 {loc.count()}")
        pg.locator("label", has_text="學號").click()
        check(pg.evaluate("document.activeElement.id") == "student_id", "點「學號」label 會聚焦學號欄")
        pg.locator("label", has_text="姓名").click()
        check(pg.evaluate("document.activeElement.id") == "name", "點「姓名」label 會聚焦姓名欄")
        type_into(pg, "#student_id", "9994A019"); type_into(pg, "#name", "測試19")
        type_into(pg, "#pin1", "1919"); type_into(pg, "#pin2", "1919")
        pg.click("#go")
        pg.wait_for_selector("#receipt", state="visible")
        teacher.request.post(f"{B}/api/t/session/{sid}/close")
        sid2 = open_session(teacher, cid)
        pg2 = ctx.new_page()
        enter(pg2, qr_code(teacher, sid2), sel="#returnFields")
        pg2.wait_for_function("document.getElementById('returnFields').style.display===''", timeout=15000)
        box = pg2.locator("#returnFields a").bounding_box()
        check(box is not None and box["width"] >= 44 and box["height"] >= 44, f"「不是我」可點範圍 ≥ 44×44，實得 {box}")
        teacher.request.post(f"{B}/api/t/session/{sid2}/close")
    except Exception as e:
        check(False, f"拋例外{e!r}")
    finally:
        ctx.close()
        close_all_live(teacher)


def run_zoom_200(p, webkit, teacher, cid):
    device = dict(p.devices["iPhone SE (3rd gen)"])
    device["viewport"] = {"width": 188, "height": 334}  # 375×667 放大 200% 之後的 CSS 視窗
    sid = open_session(teacher, cid)
    ctx = webkit.new_context(**device)
    pg = ctx.new_page()
    try:
        enter(pg, qr_code(teacher, sid))
        type_into(pg, "#student_id", "9994A020"); type_into(pg, "#name", "測試20")
        type_into(pg, "#pin1", "2020"); type_into(pg, "#pin2", "2020")
        pg.evaluate("window.scrollTo(0, document.documentElement.scrollHeight)")
        check(pg.evaluate("scrollY") > 0, "前置，送出前頁面已捲到下面")
        pg.click("#go")
        pg.wait_for_selector("#receipt", state="visible")
        pg.wait_for_function("scrollY===0", timeout=3000)
        r = pg.evaluate("(()=>{const b=document.getElementById('rstamp').getBoundingClientRect();return {t:b.top,b:b.bottom,l:b.left,r:b.right,w:innerWidth,h:innerHeight}})()")
        check(r["t"] >= 0 and r["b"] <= r["h"] and r["l"] >= 0 and r["r"] <= r["w"], f"200% 縮放下收據章在視窗內，實得 {r}")
        pg.goto(f"{B}/me")
        pg.wait_for_selector("#out table", timeout=10000)
        check(pg.locator("#out .tablewrap table").count() >= 1, "/me 的表格包在 .tablewrap 裡")
        check(pg.evaluate("document.documentElement.scrollWidth<=innerWidth"), "/me 在 200% 縮放下頁面沒有橫向溢出")
    except Exception as e:
        check(False, f"拋例外{e!r}")
    finally:
        ctx.close()
        teacher.request.post(f"{B}/api/t/session/{sid}/close")


def main():
    with sync_playwright() as p:
        webkit = p.webkit.launch()
        device = p.devices["iPhone 13"]
        tctx = webkit.new_context(viewport={"width": 1280, "height": 900})
        r = tctx.request.post(f"{B}/t/login", form={"password": PW, "next": "/t"})
        assert r.ok, r.status
        cid = int(re.search(r"course/(\d+)/open", tctx.request.get(f"{B}/t").text()).group(1))

        class T:  # 只借 .request，同 e2e_student_input.py 的用法
            request = tctx.request
        close_all_live(T)
        run_refresh_pin_only(webkit, device, T, cid)
        run_whoami_502(webkit, device, T, cid)
        run_keyboard_scroll(p, webkit, T, cid)
        run_keyboard_scroll_second_path(p, webkit, T, cid)
        run_not_me(webkit, device, T, cid)
        run_zoom_200(p, webkit, T, cid)
        run_form_expiry(webkit, device, T, cid)  # 最慢（等表單時效），放最後
        webkit.close()


main()
print("\n==>", "全部通過" if not FAIL else f"{len(FAIL)} 項失敗：{FAIL}")
sys.exit(1 if FAIL else 0)
