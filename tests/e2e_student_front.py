"""學生端前端回歸。
被 tests/e2e_friction_playwright.py 結尾以子程序呼叫（同 e2e_teacher_friction_playwright.py
呼叫 e2e_out_of_order.py 那個手法），也可以獨立直接跑（自己登入老師、自己開場次）。
前提同 e2e_friction_playwright.py：乾淨庫、ATTEND_TEST_BASE、NTUB-CD 名冊；本檔全程只用
teacher.request（API context）操作老師端，不開老師的瀏覽器分頁。

四條各自包成一個函式、外層 try/except 隔離：任何一條中途拋例外（逾時等）都不該讓其餘三條
連跑都沒跑到——尤其拿舊版程式碼當紅燈基準時，需要看到「四項都紅」而不是第一條例外就整支中斷。

倒數那一條的「fast_forward 60 秒 → 剩餘秒數少至少 55」那一項：gate.sh 的 e2e-friction 層把這門課
的 grant_seconds 設成 30（倒數測試要用，見 tests/gate.sh 的 layer_e2e_friction），填表視窗只剩
max(10,30-5)=25 秒，字面上的「60 秒」會讓表單整個過期而不是「還在倒數但變少」。這裡改成讀
實際的 GRANT_SECONDS，等比例操作（60 秒與 55 秒的 5 秒安全邊界保留），驗的是同一件事：剩餘
秒數是用絕對 deadline 算出來的，不是每秒遞減、會被背景凍結拖累的變數——這正是被測的
機制本身。在預設 grant_seconds=180（例如直接對單一版本跑這支檔案時）ff_s 會等於字面的 60。
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


def teacher_login(browser):
    ctx = browser.new_context(viewport={"width": 1280, "height": 900})
    r = ctx.request.post(f"{B}/t/login", form={"password": PW, "next": "/t"})
    if not r.ok:
        raise RuntimeError(f"老師登入失敗：{r.status} {r.text()}")
    return ctx


def course_id(teacher):
    html = teacher.request.get(f"{B}/t").text()
    m = re.search(r"course/(\d+)/open", html)
    return int(m.group(1)) if m else 1


def close_all_live(teacher):
    html = teacher.request.get(f"{B}/t").text()
    for s in set(re.findall(r"/t/session/(\d+)", html)):
        teacher.request.post(f"{B}/api/t/session/{s}/close")


def open_session(teacher, cid, kind="normal"):
    r = teacher.request.post(f"{B}/t/course/{cid}/open", form={"kind": kind})
    return int(re.search(r"/t/session/(\d+)", r.url).group(1))


def qr_code(teacher, sid):
    return teacher.request.get(f"{B}/api/t/session/{sid}/qr").json()["code"]


def enter(page, code):
    """跟 e2e_friction_playwright.py 的 enter() 手法一致：走 /c 輸六位數落地學生頁。"""
    page.goto(f"{B}/c")
    page.click("#otp input >> nth=0")
    page.keyboard.type(code)
    page.wait_for_selector("#student_id, #returnFields")


def find_pk(students_html, student_id):
    """從名冊頁抓 unbind(pk, ...) 裡的 pk（students.html 的既有寫法，沒有別的 API 能查）。"""
    idx = students_html.index(f">{student_id}<")
    row = students_html[idx: students_html.index("</tr>", idx)]
    return int(re.search(r"unbind\((\d+),", row).group(1))


def test_countdown_deadline(p, teacher, cid):
    """倒數以絕對 deadline 算，切 App 回來要馬上對。"""
    for engine_name, device_name in (("webkit", "iPhone 13"), ("chromium", "Pixel 7")):
        engine = getattr(p, engine_name)
        eng_browser = engine.launch()
        try:
            # 1) fast_forward 超過 GRANT_SECONDS → 1 秒內倒數顯示「時間到」，按鈕文字改對
            close_all_live(teacher)
            sid = open_session(teacher, cid)
            code = qr_code(teacher, sid)
            ctx1 = eng_browser.new_context(**p.devices[device_name])
            pg1 = ctx1.new_page()
            pg1.clock.install()  # 一定要在 goto 之前安裝，才能連頁面載入那一刻的 Date.now() 都是假的
            enter(pg1, code)
            gs = pg1.evaluate("GRANT_SECONDS")
            pg1.clock.fast_forward((gs + 3) * 1000)
            pg1.wait_for_function("document.getElementById('countdown').innerText==='時間到'", timeout=1500)
            check(True, f"[{engine_name}]fast_forward 超過 GRANT_SECONDS（{gs}s）後 1 秒內倒數顯示「時間到」")
            btn_txt = pg1.locator("#go").inner_text()
            check("重新掃 QR" in btn_txt and "改輸六位數" in btn_txt,
                  f"[{engine_name}]逾時後按鈕文字是「重新掃 QR 或改輸六位數」，實得 {btn_txt!r}")
            ctx1.close()
        except Exception as e:
            check(False, f"[{engine_name}]第 1 段（fast_forward 超過 GRANT_SECONDS）拋例外：{e!r}")

        try:
            # 2) fast_forward 一部分時間 → 剩餘秒數對應減少（不是累加誤差的 left--）
            close_all_live(teacher)
            sid2 = open_session(teacher, cid)
            code2 = qr_code(teacher, sid2)
            ctx2 = eng_browser.new_context(**p.devices[device_name])
            pg2 = ctx2.new_page()
            pg2.clock.install()
            enter(pg2, code2)
            gs2 = pg2.evaluate("GRANT_SECONDS")
            deadline_span = max(10, gs2 - 5)
            ff_s = min(60, deadline_span - 5) if deadline_span > 15 else max(5, deadline_span - 3)
            left0 = pg2.evaluate("Math.ceil((deadline-Date.now())/1000)")
            pg2.clock.fast_forward(ff_s * 1000)
            left1 = pg2.evaluate("Math.ceil((deadline-Date.now())/1000)")
            check(left0 - left1 >= ff_s - 5,
                  f"[{engine_name}]fast_forward {ff_s} 秒後剩餘秒數對應減少（{left0}→{left1}）")
            ctx2.close()
        except Exception as e:
            check(False, f"[{engine_name}]第 2 段（fast_forward 部分時間）拋例外：{e!r}")
        finally:
            eng_browser.close()

    try:
        # 3) /api/checkin 回 410「填表時間已到」→ 前端不等下一次倒數，直接進入逾時狀態
        close_all_live(teacher)
        sid3 = open_session(teacher, cid)
        code3 = qr_code(teacher, sid3)
        ck_browser = p.chromium.launch()
        try:
            ctx3 = ck_browser.new_context(**p.devices["iPhone 13"])
            pg3 = ctx3.new_page()
            enter(pg3, code3)
            pg3.route("**/api/checkin", lambda route: route.fulfill(
                status=410, content_type="application/json",
                body='{"ok":false,"error":"填表時間已到，請重新掃描 QR"}'))
            pg3.fill("#student_id", "9994A021"); pg3.fill("#name", "測試21")
            pg3.fill("#pin1", "1234"); pg3.fill("#pin2", "1234")
            pg3.click("#go")
            pg3.wait_for_function("document.getElementById('countdown').innerText==='時間到'", timeout=3000)
            check(True, "/api/checkin 回 410「填表時間已到」→ 倒數立刻變「時間到」")
            ctx3.close()
        finally:
            ck_browser.close()
    except Exception as e:
        check(False, f"第 3 段（/api/checkin 回 410）拋例外：{e!r}")


def test_whoami_pending_disabled(p, teacher, cid):
    """確認手機還沒完成前，簽到鈕不能按。"""
    browser_whoami = p.webkit.launch()
    try:
        close_all_live(teacher)
        sid_whoami = open_session(teacher, cid)
        code_whoami = qr_code(teacher, sid_whoami)
        ctx_whoami = browser_whoami.new_context(**p.devices["iPhone 13"])
        pg_whoami = ctx_whoami.new_page()
        checkin_reqs = []
        pg_whoami.on("request", lambda r: checkin_reqs.append(r.url) if "/api/checkin" in r.url else None)
        hung_routes = []
        pg_whoami.route("**/api/whoami", lambda route: hung_routes.append(route))  # 掛住不回
        pg_whoami.goto(f"{B}/c")
        pg_whoami.click("#otp input >> nth=0")
        pg_whoami.keyboard.type(code_whoami)
        pg_whoami.wait_for_selector("#go")
        pg_whoami.wait_for_timeout(700)
        check(pg_whoami.eval_on_selector("#go", "e=>e.disabled"), "whoami 掛住不回時送出鈕仍停用")
        go_txt = pg_whoami.locator("#go").inner_text()
        check("確認這支手機" in go_txt, f"按鈕文字含「確認這支手機…」，實得 {go_txt!r}")
        for _ in range(3):
            pg_whoami.click("#go", force=True, timeout=1000)  # 原生 disabled，force 點下去也不該觸發 onclick
        pg_whoami.wait_for_timeout(300)
        check(checkin_reqs == [], f"whoami 未回應前點擊不送出任何 /api/checkin，實得 {checkin_reqs}")
        # 放行（規格：「放行（或兩次逾時）」二選一）：主動把還吊著的請求 abort 掉，讓 init() 的重試迴圈
        # 很快跑完兩次，不用真的等滿 2×5 秒——同時避免關 context 時還吊著沒解決的 route 產生的雜訊
        for _ in range(4):
            while hung_routes:
                hung_routes.pop().abort()
            pg_whoami.wait_for_timeout(300)
        pg_whoami.unroute("**/api/whoami")
        pg_whoami.wait_for_function("!document.getElementById('go').disabled", timeout=13000)
        check(True, "放行後（兩次重試都失敗）退回首綁模式，按鈕恢復可按")
        err_after = pg_whoami.locator("#err").inner_text()
        check(err_after == "", f"#err 清空，實得 {err_after!r}")
        ctx_whoami.close()
    finally:
        browser_whoami.close()


def test_pin_only_mismatch_switches_form(p, teacher, cid):
    """只輸 PIN 被判名冊不符，要切回完整表單。"""
    browser_mismatch = p.webkit.launch()
    try:
        close_all_live(teacher)
        sid_mismatch = open_session(teacher, cid)
        code_mismatch = qr_code(teacher, sid_mismatch)

        # 裝置 A：真的首綁，設定真正的 PIN
        ctxA = browser_mismatch.new_context(**p.devices["iPhone 13"]); A = ctxA.new_page()
        enter(A, code_mismatch)
        A.fill("#student_id", "9994A022"); A.fill("#name", "測試22")
        A.fill("#pin1", "1212"); A.fill("#pin2", "1212")
        A.click("#go"); A.wait_for_selector("#receipt", state="visible")
        ctxA.close()

        # 裝置 B（換瀏覽器）：不知道已經設過 PIN，帶 pin2 送出、PIN 猜錯 → 已經設過 PIN → 只輸 PIN 模式
        ctxB = browser_mismatch.new_context(**p.devices["iPhone 13"]); Bp = ctxB.new_page()
        enter(Bp, code_mismatch)
        Bp.fill("#student_id", "9994A022"); Bp.fill("#name", "測試22")
        Bp.fill("#pin1", "9999"); Bp.fill("#pin2", "9999")
        Bp.click("#go")
        Bp.wait_for_function("document.getElementById('err').innerText.includes('已經設過 PIN')")
        check(True, "換瀏覽器猜錯 PIN → 已經設過 PIN，切成只輸 PIN 模式")
        check(not Bp.locator("#nameField").is_visible(), "只輸 PIN 模式姓名欄隱藏")

        # 老師按「重設」（清掉 PIN 與裝置）
        students_html = teacher.request.get(f"{B}/t/course/{cid}/students").text()
        pk = find_pk(students_html, "9994A022")
        r_reset = teacher.request.post(f"{B}/api/t/student/{pk}/unbind", form={"reset_pin": "1"})
        check(r_reset.ok, f"老師重設 PIN 成功，實得 {r_reset.status}")

        # 同一頁只輸 PIN 送出（pinOnly 模式姓名欄是空的）→ 空姓名對不上名冊 →「名冊不符」→ 前端切回完整表單
        Bp.fill("#pin1", "9999")
        Bp.click("#go")
        Bp.wait_for_function("document.getElementById('nameField').style.display!=='none'", timeout=5000)
        check(True, "重設後只輸 PIN 送出撞「名冊不符」→ 姓名欄重新出現（switchFirst）")
        err_txt = Bp.locator("#err").inner_text()
        check(err_txt == "請把姓名也填上，並重新設定 PIN（兩次）。如果學號打錯了，先改學號。",
              f"#err 是規格文案，實得 {err_txt!r}")
        check(Bp.locator("#student_id").input_value() == "9994A022", "學號保留")

        # 填姓名與兩次 PIN 再送 → 收據出現
        Bp.fill("#name", "測試22"); Bp.fill("#pin1", "3434"); Bp.fill("#pin2", "3434")
        Bp.click("#go")
        Bp.wait_for_selector("#receipt", state="visible")
        check(True, "補填姓名與兩次 PIN 後送出成功，收據出現")
        ctxB.close()
    finally:
        browser_mismatch.close()


def test_closed_midform(p, teacher, cid):
    """送出時點名剛結束，說清楚接下來怎麼做。"""
    browser_closed = p.chromium.launch()
    try:
        close_all_live(teacher)
        sid_closed = open_session(teacher, cid)
        code_closed = qr_code(teacher, sid_closed)
        ctx_closed = browser_closed.new_context(**p.devices["Pixel 7"])
        pg_closed = ctx_closed.new_page()
        enter(pg_closed, code_closed)
        pg_closed.fill("#student_id", "9994A023"); pg_closed.fill("#name", "測試23")
        pg_closed.fill("#pin1", "5656"); pg_closed.fill("#pin2", "5656")
        # 老師結束點名（這門課同時沒有別場在進行，才會走「請直接找老師」那句，不是「開了新的一場」那句）
        r_close = teacher.request.post(f"{B}/api/t/session/{sid_closed}/close")
        check(r_close.ok, f"老師結束點名成功，實得 {r_close.status}")
        pg_closed.click("#go")
        pg_closed.wait_for_function("document.getElementById('err').innerText.includes('請直接找老師')", timeout=5000)
        err_135 = pg_closed.locator("#err").inner_text()
        check(err_135 == "點名已經結束。有到課但沒簽到的同學，請直接找老師。",
              f"410 訊息含「請直接找老師」，實得 {err_135!r}")
        go_135 = pg_closed.locator("#go").inner_text()
        check(go_135 == "點名已結束", f"按鈕文字「點名已結束」，實得 {go_135!r}")
        check(pg_closed.eval_on_selector("#go", "e=>e.disabled"), "按鈕停用")
        ctx_closed.close()
    finally:
        browser_closed.close()


with sync_playwright() as p:
    api_browser = p.chromium.launch()
    teacher = teacher_login(api_browser)
    cid = course_id(teacher)

    for name, fn in (("B130", test_countdown_deadline), ("B131", test_whoami_pending_disabled), ("B134", test_pin_only_mismatch_switches_form), ("B135", test_closed_midform)):
        try:
            fn(p, teacher, cid)
        except Exception as e:
            check(False, f"{name}：測試腳本拋出未預期例外，視為這一條失敗：{e!r}")

    api_browser.close()

print("\n==>", "全部通過" if not FAIL else f"{len(FAIL)} 項失敗：{FAIL}")
sys.exit(1 if FAIL else 0)
