"""端對端異常路徑：待確認、代簽連坐、換手機被擋→解綁→重綁、關閉首次綁定、重設 PIN、過期 QR、錯碼、無點名、自查有紀錄、投影頁結束。
前提同 e2e_playwright.py（乾淨庫、ATTEND_TEST_BASE 預設 8765、NTUB-CD 名冊）。"""
import os, re, sys
from playwright.sync_api import sync_playwright

B = os.environ.get("ATTEND_TEST_BASE", "http://127.0.0.1:8765"); FAIL = []
def check(c, m):
    print(("ok   " if c else "FAIL ") + m)
    if not c: FAIL.append(m)

def code_of(proj): return proj.locator("#code").inner_text().replace(" ", "")
def enter(page, proj):
    page.goto(f"{B}/c"); page.click("#otp input >> nth=0"); page.keyboard.type(code_of(proj))
def first_sign(page, proj, sid, name, pin):
    enter(page, proj); page.wait_for_function("document.getElementById('modeHint')!==null&&(document.getElementById('returnFields').style.display!=='none'||document.getElementById('modeHint').innerText.length>0)")
    if page.locator("#returnFields").is_visible():
        page.click("text=不是我")  # 這支手機已認出別人，要換人簽
    page.wait_for_selector("#student_id", state="visible")
    page.fill("#student_id", sid); page.fill("#name", name); page.fill("#pin1", pin); page.fill("#pin2", pin); page.click("#go")
    page.wait_for_function("document.getElementById('receipt').style.display!=='none'||document.getElementById('err').innerText!==''")
    return page.locator("#err").inner_text()

with sync_playwright() as p:
    errors = []
    chrome = p.chromium.launch(); teacher = chrome.new_context(viewport={"width": 1280, "height": 900}); tp = teacher.new_page()
    tp.on("console", lambda m: errors.append(("teacher", m.text)) if m.type == "error" and "status of 4" not in m.text else None)
    tp.goto(f"{B}/t/login"); tp.fill("input[name=password]", "pw"); tp.click("button:has-text('登入')"); tp.wait_for_url(f"{B}/t")
    # 沒點名時輸碼
    s0 = p.webkit.launch(); ctxA = s0.new_context(**p.devices["iPhone 13"]); A = ctxA.new_page()
    A.goto(f"{B}/c"); A.click("#otp input >> nth=0"); A.keyboard.type("123456"); A.wait_for_selector("text=目前沒有進行中的點名"); check(True, "無點名時輸碼提示")
    tp.locator("form[action$='/open'] button").first.click(); tp.wait_for_url(re.compile(r"/t/session/\d+")); sid = int(tp.url.rsplit("/", 1)[1])
    proj = teacher.new_page(); proj.goto(f"{B}/t/projector/{sid}"); proj.wait_for_function("document.querySelector('#qr svg')!==null")
    # 錯碼
    A.goto(f"{B}/c"); A.click("#otp input >> nth=0"); A.keyboard.type("000000"); A.wait_for_selector("text=代碼不對"); check(True, "錯碼提示")
    # 過期 QR 網址
    A.goto(f"{B}/s/{sid}/1/deadbeef00"); check("QR 已過期" in A.locator("h1").inner_text(), "過期 QR 頁"); A.click("text=改輸六位數"); A.wait_for_url(f"{B}/c")
    # A 首簽；B 用 A 的手機（同 context）代簽 → 待確認＋候章
    check(first_sign(A, proj, "9994A001", "陳大文", "1111") == "", "A001 首簽")
    check(first_sign(A, proj, "9994A002", "吳家豪", "2222") == "", "A002 在同手機送出")
    check(A.locator("#rstamp").inner_text() == "候" and "老師會當場確認" in A.locator("#rnote").inner_text(), "待確認顯示黃色候章與說明")
    tp.wait_for_function("[...document.querySelectorAll('#rows tr')].filter(t=>t.innerText.includes('同裝置多人')).length===2", timeout=8000); check(True, "老師名單兩人都標同裝置")
    tp.select_option("#filter", "pending"); tp.wait_for_function("document.querySelectorAll('#rows tr').length===2"); t=tp.locator("#rows").inner_text(); check("9994A001" in t and "9994A002" in t, "待確認篩選同手機兩人都在"); tp.select_option("#filter", "")
    # 抽點：抽到 A002 判不在 → A002 缺席、A001 連坐
    tp.click("#spotBtn"); tp.wait_for_selector("#spot .pick")
    for _ in range(3):
        if "9994A002" in tp.locator("#spot").inner_text(): break
        tp.click("#spotBtn"); tp.wait_for_timeout(500)
    pick = tp.locator("#spot .pick", has_text="9994A002"); pick.locator("button.danger").click()
    tp.wait_for_function("[...document.querySelectorAll('#rows tr')].some(t=>t.innerText.includes('9994A001')&&t.innerText.includes('代簽連坐'))", timeout=8000); check(True, "A001 連坐標記")
    # C 用新手機簽 A001（已綁 A 手機）→ 被擋；老師解綁 → 可簽
    ctxC = s0.new_context(**p.devices["iPhone 13"]); C = ctxC.new_page()
    err = first_sign(C, proj, "9994A001", "陳大文", "1111"); check("另一支手機" in err, "換手機被擋")
    tp.goto(f"{B}/t/course/1/students"); row = tp.locator("table tr", has_text="9994A001"); tp.once("dialog", lambda d: d.accept()); row.locator("button", has_text="解除綁定").click(); tp.wait_for_load_state()
    err = first_sign(C, proj, "9994A001", "陳大文", "5555"); check("原本的 PIN" in err, "解綁後設新 PIN 被提示要輸舊 PIN")
    check(not C.locator("#pin2").is_visible(), "提示後表單切成只輸一欄 PIN（B016）")
    C.fill("#pin1", "1111"); C.click("#go"); C.wait_for_selector("#receipt", state="visible"); check("缺席" in C.locator("#rnote").inner_text() and C.locator("#rstamp").inner_text() == "缺", "舊 PIN 綁新手機，拿回本場紀錄（先前被連坐記缺席，收據照實顯示）")
    # 關閉首次綁定 → D 首簽被擋
    tp.goto(f"{B}/t/session/{sid}"); tp.wait_for_function("document.getElementById('bindBtn').innerText.includes('開放中')"); tp.click("#bindBtn"); tp.wait_for_function("document.getElementById('bindBtn').innerText.includes('已關閉')"); check(True, "點名頁一鍵關閉首次綁定")
    ctxD = s0.new_context(**p.devices["iPhone 13"]); D = ctxD.new_page()
    err = first_sign(D, proj, "9994A003", "林小華", "3333"); check("不開放首次綁定" in err, "關閉後首簽被擋")
    tp.click("#bindBtn"); tp.wait_for_function("document.getElementById('bindBtn').innerText.includes('開放中')")
    check(first_sign(D, proj, "9994A003", "林小華", "3333") == "", "重開後可簽")
    # 老師重設 A003 → D 再掃當首次，新 PIN
    tp.goto(f"{B}/t/course/1/students"); row = tp.locator("table tr", has_text="9994A003"); tp.once("dialog", lambda d: d.accept()); row.locator("button", has_text="重設").click(); tp.wait_for_load_state()
    tp.wait_for_function("[...document.querySelectorAll('table tr')].some(t=>t.innerText.includes('9994A003')&&t.innerText.includes('未使用'))", timeout=8000); check(True, "重設後名冊顯示未使用")
    check(first_sign(D, proj, "9994A003", "林小華", "9999") == "" and "已經簽過" in D.locator("#rnote").inner_text(), "重設後新 PIN 綁定並拿回原紀錄")
    # 自查有紀錄（要結束後）
    tp.goto(f"{B}/t/session/{sid}"); tp.wait_for_function("document.getElementById('phase').innerText==='簽到中'"); tp.once("dialog", lambda d: d.accept()); tp.click("#closeBtn"); tp.wait_for_function("document.getElementById('phase').innerText==='已結束'")
    D.goto(f"{B}/me"); D.wait_for_function("document.getElementById('out').innerText.includes('林小華')"); check("100%" in D.locator("#out").inner_text(), "自查頁顯示紀錄與出席率")
    A.goto(f"{B}/me"); A.wait_for_function("document.getElementById('out').innerText.length>10"); check("吳家豪" not in A.locator("#out").inner_text(), "代簽者手機看不到被代簽者紀錄")
    proj.wait_for_function("document.body.classList.contains('closed')"); check("點名已結束" in proj.locator("#msg").inner_text(), "投影頁結束畫面")
    # 結束後學生掃碼
    A.goto(f"{B}/c"); A.click("#otp input >> nth=0"); A.keyboard.type("123456"); A.wait_for_selector("text=目前沒有進行中的點名"); check(True, "結束後輸碼提示無點名")
    # 報表
    tp.goto(f"{B}/t/course/1/report"); t = tp.locator("table").inner_text(); check("9994A001" in t and "9994A002" in t, "報表列出")
    check(not errors, f"主控台無錯誤 {errors[:3]}")
    chrome.close(); s0.close()
print("\n==>", "全部通過" if not FAIL else f"{len(FAIL)} 項失敗：{FAIL}")
sys.exit(1 if FAIL else 0)
