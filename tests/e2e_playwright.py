"""真瀏覽器端對端：Chromium 當老師（筆電）、WebKit 模擬 iPhone 當學生。
前提：本機 uvicorn 已在 ATTEND_TEST_BASE（預設 http://127.0.0.1:8765）跑、老師密碼 pw、名冊已匯入 NTUB-CD。
跑法：.venv/bin/python tests/e2e_playwright.py（gate.sh 會另外指定 ATTEND_TEST_BASE 以免撞埠）"""
import os, re, sys
from playwright.sync_api import sync_playwright

B = os.environ.get("ATTEND_TEST_BASE", "http://127.0.0.1:8765")
FAIL = []
def check(c, m):
    print(("ok   " if c else "FAIL ") + m)
    if not c: FAIL.append(m)

with sync_playwright() as p:
    errors = []
    chrome = p.chromium.launch(); teacher = chrome.new_context(viewport={"width": 1280, "height": 800})
    tp = teacher.new_page(); tp.on("console", lambda m: errors.append(("teacher", m.text)) if m.type == "error" else None)
    tp.goto(f"{B}/t"); check("登入" in tp.title() or tp.url.endswith("/t/login?next=/t"), "未登入導到登入頁")
    tp.fill("input[name=password]", "pw"); tp.click("button:has-text('登入')"); tp.wait_for_url(f"{B}/t")
    check(tp.locator("h1").inner_text() == "點名簿", "主頁標題")
    tp.locator("form[action$='/open'] button").first.click(); tp.wait_for_url(re.compile(r"/t/session/\d+"))
    sid = int(tp.url.rsplit("/", 1)[1]); check(sid > 0, f"開點名 session {sid}")
    tp.wait_for_function("document.getElementById('phase').innerText==='簽到中'"); check(True, "點名頁顯示簽到中")
    proj = teacher.new_page(); proj.on("console", lambda m: errors.append(("projector", m.text)) if m.type == "error" else None)
    proj.goto(f"{B}/t/projector/{sid}"); proj.wait_for_function("document.querySelector('#qr svg')!==null")
    code = proj.locator("#code").inner_text().replace(" ", ""); check(re.fullmatch(r"\d{6}", code) is not None, f"投影頁六位數 {code}")

    webkit = p.webkit.launch(); iphone = p.devices["iPhone 13"]; student = webkit.new_context(**iphone)
    sp = student.new_page(); sp.on("console", lambda m: errors.append(("student", m.text)) if m.type == "error" else None)
    # 用真 QR 網址：從投影頁 svg 拿不到網址，改走 /c
    sp.goto(f"{B}/c"); code = proj.locator("#code").inner_text().replace(" ", "")
    sp.click("#otp input >> nth=0"); sp.keyboard.type(code); sp.wait_for_selector("#student_id")
    check(sp.locator("#modeHint").inner_text().startswith("第一次"), "學生首次表單")
    sp.fill("#student_id", "9994a001"); sp.fill("#name", "陳大文"); sp.fill("#pin1", "1234"); sp.fill("#pin2", "1234"); sp.click("#go")
    sp.wait_for_selector("#receipt", state="visible"); check(sp.locator("#rstamp").inner_text() == "到" and sp.locator("#rname").inner_text() == "陳大文", "收據紅章＋姓名")
    check(sp.locator("#rseat").inner_text() == "9994A001", "收據學號大寫")
    # 老師名單看到她
    tp.wait_for_function("[...document.querySelectorAll('#rows tr')].some(t=>t.innerText.includes('陳大文')&&t.innerText.includes('出席'))", timeout=8000); check(True, "老師名單三秒內更新")
    proj.wait_for_function("document.getElementById('count').innerText.startsWith('1 /')", timeout=5000); check(True, "投影頁人數 1")
    # 回簽（A2）：辨識姓名，已綁裝置免 PIN，畫面沒有 PIN 欄位，按「簽到」直接完成
    sp.goto(f"{B}/c"); code = proj.locator("#code").inner_text().replace(" ", ""); sp.click("#otp input >> nth=0"); sp.keyboard.type(code)
    sp.wait_for_selector("#returnFields", state="visible"); check("陳大文" in sp.locator("#who").inner_text(), "回簽顯示姓名")
    check(not sp.locator("#pin").count(), "A2：回簽畫面沒有 PIN 欄位")
    sp.click("#go"); sp.wait_for_selector("#receipt", state="visible"); check("已經簽過" in sp.locator("#rnote").inner_text(), "回簽免 PIN 拿回原紀錄")
    # 自查
    sp.goto(f"{B}/me"); sp.wait_for_function("document.getElementById('out').innerText.length>10"); check("陳大文" in sp.locator("#out").inner_text() or "還沒有" in sp.locator("#out").inner_text(), "自查頁可開")
    # 老師：抽點 → 在；下拉改遲到；手動簽到；結束
    tp.click("#spotBtn"); tp.wait_for_selector("#spot .pick"); tp.locator("#spot .pick button").first.click()
    tp.wait_for_function("document.querySelector('#spot .pick .tag')!==null"); check("在" in tp.locator("#spot .pick .tag").first.inner_text(), "抽點判在")
    tp.select_option("#rows tr:has-text('9994A002') select", "late"); tp.wait_for_function("[...document.querySelectorAll('#rows tr')].some(t=>t.innerText.includes('9994A002')&&t.innerText.includes('遲到'))"); check(True, "下拉改遲到即時更新")
    # BUGLOG B031：手動簽到輸入框改成後 3 碼／姓名比對＋直接出席／遲到鈕，不再有獨立的狀態下拉＋送出鈕；
    # 「請假」不在那兩顆快速鈕裡，改用名單每列本來就有的「改」下拉（setStatus，B031 沒有動它）。
    tp.select_option("#rows tr:has-text('9994A003') select", "excused")
    tp.wait_for_function("[...document.querySelectorAll('#rows tr')].some(t=>t.innerText.includes('9994A003')&&t.innerText.includes('請假'))"); check(True, "手動請假")
    tp.once("dialog", lambda d: d.accept()); tp.click("#closeBtn"); tp.wait_for_function("document.getElementById('phase').innerText==='已結束'")
    check(tp.locator("#spotBtn").is_disabled() and tp.locator("#extendBtn").is_disabled(), "結束後抽點與延長停用")
    proj.wait_for_function("document.body.classList.contains('closed')"); check(True, "投影頁顯示已結束")
    tp.goto(f"{B}/t/course/1/report"); check("100%" in tp.content(), "報表出席率")
    # 三視口不溢出
    for w in (375, 820, 1440):
        tp.set_viewport_size({"width": w, "height": 900}); tp.goto(f"{B}/t")
        check(tp.evaluate("document.documentElement.scrollWidth<=window.innerWidth"), f"主頁 {w} 無橫向溢出")
    check(not errors, f"主控台無錯誤 {errors[:3]}")  # A2 之後這條流程不再故意觸發 4xx，不用再濾錯
    chrome.close(); webkit.close()
print("\n==>", "全部通過" if not FAIL else f"{len(FAIL)} 項失敗：{FAIL}")
sys.exit(1 if FAIL else 0)
