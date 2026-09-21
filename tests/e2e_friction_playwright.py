"""9/10 實地摩擦的前端回歸（BUGLOG B003/B014/B016/B017/B019/B020）。
前提同 e2e_edge_playwright.py（乾淨庫、ATTEND_TEST_BASE 預設 8765、NTUB-CD 名冊）；
gate.sh 的 e2e-friction 層另外把這門課的 grant_seconds 設成最低值 30，讓 B020 倒數測試不用等 3 分鐘。"""
import os, re, sys
from playwright.sync_api import sync_playwright

B = os.environ.get("ATTEND_TEST_BASE", "http://127.0.0.1:8765"); FAIL = []
def check(c, m):
    print(("ok   " if c else "FAIL ") + m)
    if not c: FAIL.append(m)

def code_of(proj): return proj.locator("#code").inner_text().replace(" ", "")
def enter(page, proj):
    page.goto(f"{B}/c"); page.click("#otp input >> nth=0"); page.keyboard.type(code_of(proj))
    page.wait_for_selector("#student_id, #returnFields")

with sync_playwright() as p:
    errors = []
    chrome = p.chromium.launch(); teacher = chrome.new_context(viewport={"width": 1280, "height": 900}); tp = teacher.new_page()
    tp.goto(f"{B}/t/login"); tp.fill("input[name=password]", "pw"); tp.click("button:has-text('登入')"); tp.wait_for_url(f"{B}/t")
    tp.locator("form[action$='/open'] button").first.click(); tp.wait_for_url(re.compile(r"/t/session/\d+")); sid = int(tp.url.rsplit("/", 1)[1])
    proj = teacher.new_page(); proj.goto(f"{B}/t/projector/{sid}"); proj.wait_for_function("document.querySelector('#qr svg')!==null")

    webkit = p.webkit.launch(); iphone = p.devices["iPhone 13"]

    # ───── B003：前端先驗，不合法就不發請求、焦點與錯誤移到欄位、#err 是 role=alert ─────
    ctxA = webkit.new_context(**iphone); A = ctxA.new_page()
    reqs = []
    A.on("request", lambda r: reqs.append(r.url) if "/api/checkin" in r.url else None)
    enter(A, proj)
    check(A.locator("#err").get_attribute("role") == "alert", "B003：#err 有 role=alert")
    A.click("#go")  # 全部空白
    A.wait_for_function("document.getElementById('err').innerText.length>0")
    check("學號" in A.locator("#err").inner_text(), "B003：空學號擋下並提示")
    check(A.evaluate("document.activeElement.id") == "student_id", "B003：焦點移到學號欄")
    check(A.evaluate("document.getElementById('err').nextElementSibling && document.getElementById('err').nextElementSibling.id==='student_id'"),
          "B003：錯誤訊息移到學號欄正上方（瀏覽器把聚焦欄位頂到鍵盤上緣時，下方那行會被鍵盤蓋住）")
    # 鍵盤開著時可視高度約 364px（iPhone 13 視窗 664 扣鍵盤 300）：最下面那個欄位的錯誤也必須落在這條線以上
    A.fill("#student_id", "9994A001"); A.fill("#name", "陳大文"); A.fill("#pin1", "1234"); A.fill("#pin2", "9999"); A.click("#go")
    A.wait_for_function("document.getElementById('err').innerText.includes('不一樣')")
    A.wait_for_timeout(400)
    box = A.locator("#err").bounding_box()
    check(box is not None and box["y"] >= 0 and box["y"] + box["height"] <= 364, f"B003：最下面欄位的錯誤仍在鍵盤上方可視區（y={box and round(box['y'])}）")
    A.fill("#student_id", ""); A.fill("#name", ""); A.fill("#pin1", ""); A.fill("#pin2", "")
    A.fill("#student_id", "9994A001"); A.click("#go")
    A.wait_for_function("document.getElementById('err').innerText.includes('姓名')")
    check(True, "B003：學號有了但姓名空白，擋下並提示姓名")
    A.fill("#name", "陳大文"); A.click("#go")
    A.wait_for_function("document.getElementById('err').innerText.includes('PIN')")
    check("4 到 6" in A.locator("#err").inner_text(), "B003：PIN 格式不對被擋")
    A.fill("#pin1", "1234"); A.fill("#pin2", "9999"); A.click("#go")
    A.wait_for_function("document.getElementById('err').innerText.includes('不一樣')")
    check(True, "B003：兩次 PIN 不一樣被擋")
    check(reqs == [], f"B003：前面幾次不合法送出，一個 /api/checkin 都不該發出去：{reqs}")
    # 雙擊防護：模擬網路慢（扣住請求），快速點兩下，只該真的送出一次。第二下用 force=True
    # 繞過 Playwright 自己的「等到可互動」重試（否則它會一路等到 fetchWithTimeout 的 10 秒
    # 逾時把按鈕重新啟用，那時第二下才「合法地」點進去，量到的就不是雙擊防護，是逾時重試）。
    A.fill("#pin2", "1234")
    held = []
    A.route("**/api/checkin", lambda route: held.append(route))
    A.click("#go")
    check(A.eval_on_selector("#go", "el=>el.disabled"), "B003：第一下點完按鈕立刻停用")
    A.click("#go", force=True, timeout=1000)  # 按鈕原生 disabled，force 點下去也不該真的觸發 onclick
    A.wait_for_timeout(300)
    check(len(reqs) == 1, f"B003：連點防重複送出，扣住回應時只有一個 /api/checkin 真的發出去：{reqs}")
    for r_ in held:
        r_.continue_()
    A.wait_for_selector("#receipt", state="visible")
    ctxA.close()

    # ───── B014：拿掉 AbortSignal.timeout 的舊瀏覽器一樣要能簽到成功 ─────
    ctxB = webkit.new_context(**iphone)
    ctxB.add_init_script("delete AbortSignal.timeout;")
    B_ = ctxB.new_page()
    enter(B_, proj)
    B_.fill("#student_id", "9994A002"); B_.fill("#name", "吳家豪"); B_.fill("#pin1", "2222"); B_.fill("#pin2", "2222")
    B_.click("#go")
    B_.wait_for_selector("#receipt", state="visible")
    check(B_.locator("#rstamp").inner_text() == "到", "B014：沒有 AbortSignal.timeout 一樣能簽到成功")
    ctxB.close()

    # ───── B016：換瀏覽器被誤認首次，錯 PIN 給「已經設過 PIN」並切成只輸 PIN 模式 ─────
    ctxC1 = webkit.new_context(**iphone); C1 = ctxC1.new_page()
    enter(C1, proj)
    C1.fill("#student_id", "9994A003"); C1.fill("#name", "林小華"); C1.fill("#pin1", "3333"); C1.fill("#pin2", "3333")
    C1.click("#go"); C1.wait_for_selector("#receipt", state="visible")
    ctxC1.close()  # 這支「手機」的 cookie 罐關掉，模擬清資料／換瀏覽器

    ctxC2 = webkit.new_context(**iphone); C2 = ctxC2.new_page()
    enter(C2, proj)
    check(C2.locator("#firstFields").is_visible(), "B016：新 cookie 罐認不得，畫面照舊是首次表單")
    C2.fill("#student_id", "9994A003"); C2.fill("#name", "林小華"); C2.fill("#pin1", "9999"); C2.fill("#pin2", "9999")
    C2.click("#go")
    C2.wait_for_function("document.getElementById('err').innerText.includes('已經設過 PIN')")
    check(True, "B016：錯 PIN 給出「已經設過 PIN」訊息")
    check(not C2.locator("#nameField").is_visible(), "B016：切成只輸 PIN 模式後姓名欄隱藏")
    check(not C2.locator("#pin2Field").is_visible(), "B016：切成只輸 PIN 模式後第二個 PIN 欄隱藏")
    check(C2.locator("#student_id").input_value() == "9994A003", "B016：學號保留")
    C2.fill("#pin1", "3333"); C2.click("#go")  # 這次輸對原本的 PIN
    C2.wait_for_function("document.getElementById('err').innerText.length>0")
    check("另一支手機" in C2.locator("#err").inner_text(), "B016：PIN 對了之後給出可行動的換裝置訊息，不是悶著再猜")
    ctxC2.close()

    # ───── B017：sessionStorage 存草稿，重新整理不用重打 ─────
    ctxD = webkit.new_context(**iphone); D = ctxD.new_page()
    enter(D, proj)
    D.fill("#student_id", "9994A004"); D.fill("#name", "張志明")
    D.reload(); D.wait_for_selector("#student_id")
    check(D.locator("#student_id").input_value() == "9994A004", "B017：重新整理後學號從 sessionStorage 復原")
    check(D.locator("#name").input_value() == "張志明", "B017：重新整理後姓名從 sessionStorage 復原")
    ctxD.close()

    # ───── B019：學號欄全形英數轉半形，不是靜默刪掉 ─────
    ctxE = webkit.new_context(**iphone); E = ctxE.new_page()
    enter(E, proj)
    E.fill("#student_id", "９９９４Ａ００１")
    check(E.locator("#student_id").input_value() == "9994A001", "B019：全形學號自動轉半形")
    ctxE.close()

    # ───── B020：表單倒數到 0，按鈕變成「重新掃 QR 或改輸六位數」並連到 /c ─────
    ctxF = webkit.new_context(**iphone); F = ctxF.new_page()
    enter(F, proj)
    F.wait_for_function("document.getElementById('countdown').innerText==='時間到'", timeout=35000)
    check("重新掃 QR" in F.locator("#go").inner_text() and "改輸六位數" in F.locator("#go").inner_text(), "B020：逾時後按鈕文字改對")
    F.click("#go"); F.wait_for_url(f"{B}/c")
    check(True, "B020：按鈕連到 /c")
    ctxF.close()

    # ───── B030：綁定場次的收據章面字是「綁」，說明文字提醒下次上課掃碼即可 ─────
    tp.on("dialog", lambda d: d.accept())
    tp.goto(f"{B}/t/session/{sid}")
    tp.wait_for_selector("#closeBtn")
    tp.click("#closeBtn")  # 先結束原本的一般場次，同課同時只能有一場 live
    tp.wait_for_timeout(300)
    tp.goto(f"{B}/t")
    tp.locator("form[action$='/open'] button[value=bind]").click()
    tp.wait_for_url(re.compile(r"/t/session/\d+"))
    bind_sid = int(tp.url.rsplit("/", 1)[1])
    bproj = teacher.new_page(); bproj.goto(f"{B}/t/projector/{bind_sid}")
    bproj.wait_for_function("document.querySelector('#qr svg')!==null")
    check("綁定場次" in bproj.locator(".top").inner_text(), "B030：投影頁標題列顯示「綁定場次」")

    ctxG = webkit.new_context(**iphone); G = ctxG.new_page()
    enter(G, bproj)
    G.fill("#student_id", "9994A010"); G.fill("#name", "測試10"); G.fill("#pin1", "6060"); G.fill("#pin2", "6060")
    G.click("#go")
    G.wait_for_selector("#receipt", state="visible")
    check(G.locator("#rstamp").inner_text() == "綁", f"B030：綁定場次收據章面字是「綁」，實得 {G.locator('#rstamp').inner_text()!r}")
    check("下次上課掃碼後按一下就完成簽到" in G.locator("#rnote").inner_text(), "B030：綁定場次收據說明文字正確")
    ctxG.close()

    chrome.close(); webkit.close()

print("\n==>", "全部通過" if not FAIL else f"{len(FAIL)} 項失敗：{FAIL}")
sys.exit(1 if FAIL else 0)
