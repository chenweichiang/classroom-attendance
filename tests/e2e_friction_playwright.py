"""實地摩擦的前端回歸。
前提同 e2e_edge_playwright.py（乾淨庫、ATTEND_TEST_BASE 預設 8765、NTUB-CD 名冊）；
gate.sh 的 e2e-friction 層另外把這門課的 grant_seconds 設成最低值 30，讓倒數測試不用等 3 分鐘。"""
import os, re, subprocess, sys
from playwright.sync_api import sync_playwright

# 直接讀寫伺服器資料庫的路徑從 ATTEND_E2E_DB 取（gate.sh 傳 $LOGDIR 底下的唯一路徑，跟伺服器用同一個檔）；
# 沒給就落在 tempfile.mkdtemp()，不寫死 /tmp 底下的固定檔名（兩個閘門同時跑會互踩）
import tempfile
E2E_DB = os.environ.get("ATTEND_E2E_DB") or os.path.join(tempfile.mkdtemp(prefix="attend-e2e-"), "e2e-friction.db")

B = os.environ.get("ATTEND_TEST_BASE", "http://127.0.0.1:8765"); FAIL = []
def check(c, m):
    print(("ok   " if c else "FAIL ") + m)
    if not c: FAIL.append(m)

def code_of(proj): return proj.locator("#code").inner_text().replace(" ", "")
def enter(page, proj):
    page.goto(f"{B}/c"); page.click("#otp input >> nth=0"); page.keyboard.type(code_of(proj))
    page.wait_for_selector("#student_id, #returnFields")

_UA_N = [0]


def uctx(browser, **kw):
    """同指紋（IP＋UA＋語言）的另一個學號會被轉待確認。現實裡不同學生是不同手機，
    這支腳本每個新的手機 context 在 UA 後面加一個流水號，指紋各自不同；斷言不動。"""
    _UA_N[0] += 1
    if kw.get("user_agent"):
        kw["user_agent"] = f"{kw['user_agent']} Sim/{_UA_N[0]}"
    return browser.new_context(**kw)


with sync_playwright() as p:
    errors = []
    chrome = p.chromium.launch(); teacher = uctx(chrome, viewport={"width": 1280, "height": 900}); tp = teacher.new_page()
    tp.goto(f"{B}/t/login"); tp.fill("input[name=password]", "pw"); tp.click("button:has-text('登入')"); tp.wait_for_url(f"{B}/t")
    tp.locator("form[action$='/open'] button").first.click(); tp.wait_for_url(re.compile(r"/t/session/\d+")); sid = int(tp.url.rsplit("/", 1)[1])
    proj = teacher.new_page(); proj.goto(f"{B}/t/projector/{sid}"); proj.wait_for_function("document.querySelector('#qr svg')!==null")

    webkit = p.webkit.launch(); iphone = p.devices["iPhone 13"]

    # 前端先驗，不合法就不發請求、焦點與錯誤移到欄位、#err 是 role=alert
    ctxA = uctx(webkit, **iphone); A = ctxA.new_page()
    reqs = []
    A.on("request", lambda r: reqs.append(r.url) if "/api/checkin" in r.url else None)
    enter(A, proj)
    check(A.locator("#err").get_attribute("role") == "alert", "#err 有 role=alert")
    A.click("#go")  # 全部空白
    A.wait_for_function("document.getElementById('err').innerText.length>0")
    check("學號" in A.locator("#err").inner_text(), "空學號擋下並提示")
    check(A.evaluate("document.activeElement.id") == "student_id", "焦點移到學號欄")
    check(A.evaluate("document.getElementById('err').nextElementSibling && document.getElementById('err').nextElementSibling.id==='student_id'"),
          "錯誤訊息移到學號欄正上方（瀏覽器把聚焦欄位頂到鍵盤上緣時，下方那行會被鍵盤蓋住）")
    # 鍵盤開著時可視高度約 364px（iPhone 13 視窗 664 扣鍵盤 300）：最下面那個欄位的錯誤也必須落在這條線以上
    A.fill("#student_id", "9994A001"); A.fill("#name", "蘇虛一"); A.fill("#pin1", "1234"); A.fill("#pin2", "9999"); A.click("#go")
    A.wait_for_function("document.getElementById('err').innerText.includes('不一樣')")
    A.wait_for_timeout(400)
    box = A.locator("#err").bounding_box()
    check(box is not None and box["y"] >= 0 and box["y"] + box["height"] <= 364, f"最下面欄位的錯誤仍在鍵盤上方可視區（y={box and round(box['y'])}）")
    A.fill("#student_id", ""); A.fill("#name", ""); A.fill("#pin1", ""); A.fill("#pin2", "")
    A.fill("#student_id", "9994A001"); A.click("#go")
    A.wait_for_function("document.getElementById('err').innerText.includes('姓名')")
    check(True, "學號有了但姓名空白，擋下並提示姓名")
    A.fill("#name", "蘇虛一"); A.click("#go")
    A.wait_for_function("document.getElementById('err').innerText.includes('PIN')")
    check("4 到 6" in A.locator("#err").inner_text(), "PIN 格式不對被擋")
    A.fill("#pin1", "1234"); A.fill("#pin2", "9999"); A.click("#go")
    A.wait_for_function("document.getElementById('err').innerText.includes('不一樣')")
    check(True, "兩次 PIN 不一樣被擋")
    check(reqs == [], f"前面幾次不合法送出，一個 /api/checkin 都不該發出去：{reqs}")
    # 雙擊防護：模擬網路慢（扣住請求），快速點兩下，只該真的送出一次。第二下用 force=True
    # 繞過 Playwright 自己的「等到可互動」重試（否則它會一路等到 fetchWithTimeout 的 10 秒
    # 逾時把按鈕重新啟用，那時第二下才「合法地」點進去，量到的就不是雙擊防護，是逾時重試）。
    A.fill("#pin2", "1234")
    held = []
    A.route("**/api/checkin", lambda route: held.append(route))
    A.click("#go")
    check(A.eval_on_selector("#go", "el=>el.disabled"), "第一下點完按鈕立刻停用")
    A.click("#go", force=True, timeout=1000)  # 按鈕原生 disabled，force 點下去也不該真的觸發 onclick
    A.wait_for_timeout(300)
    check(len(reqs) == 1, f"連點防重複送出，扣住回應時只有一個 /api/checkin 真的發出去：{reqs}")
    for r_ in held:
        r_.continue_()
    A.wait_for_selector("#receipt", state="visible")
    ctxA.close()

    # 拿掉 AbortSignal.timeout 的舊瀏覽器一樣要能簽到成功
    ctxB = uctx(webkit, **iphone)
    ctxB.add_init_script("delete AbortSignal.timeout;")
    B_ = ctxB.new_page()
    enter(B_, proj)
    B_.fill("#student_id", "9994A002"); B_.fill("#name", "傅構二"); B_.fill("#pin1", "2222"); B_.fill("#pin2", "2222")
    B_.click("#go")
    B_.wait_for_selector("#receipt", state="visible")
    check(B_.locator("#rstamp").inner_text() == "到", "沒有 AbortSignal.timeout 一樣能簽到成功")
    ctxB.close()

    # 換瀏覽器被誤認首次，錯 PIN 給「已經設過 PIN」並切成只輸 PIN 模式
    ctxC1 = uctx(webkit, **iphone); C1 = ctxC1.new_page()
    enter(C1, proj)
    C1.fill("#student_id", "9994A003"); C1.fill("#name", "林擬三"); C1.fill("#pin1", "3333"); C1.fill("#pin2", "3333")
    C1.click("#go"); C1.wait_for_selector("#receipt", state="visible")
    ctxC1.close()  # 這支「手機」的 cookie 罐關掉，模擬清資料／換瀏覽器

    ctxC2 = uctx(webkit, **iphone); C2 = ctxC2.new_page()
    enter(C2, proj)
    check(C2.locator("#firstFields").is_visible(), "新 cookie 罐認不得，畫面照舊是首次表單")
    C2.fill("#student_id", "9994A003"); C2.fill("#name", "林擬三"); C2.fill("#pin1", "9999"); C2.fill("#pin2", "9999")
    C2.click("#go")
    C2.wait_for_function("document.getElementById('err').innerText.includes('已經設過 PIN')")
    check(True, "錯 PIN 給出「已經設過 PIN」訊息")
    check(not C2.locator("#nameField").is_visible(), "切成只輸 PIN 模式後姓名欄隱藏")
    check(not C2.locator("#pin2Field").is_visible(), "切成只輸 PIN 模式後第二個 PIN 欄隱藏")
    check(C2.locator("#student_id").input_value() == "9994A003", "學號保留")
    C2.fill("#pin1", "3333"); C2.click("#go")  # 這次輸對原本的 PIN
    C2.wait_for_function("document.getElementById('err').innerText.length>0")
    check("另一個瀏覽器" in C2.locator("#err").inner_text(), "PIN 對了之後給出可行動的換裝置訊息，不是悶著再猜")  # 訊息改叫人回上次的 App
    ctxC2.close()

    # sessionStorage 存草稿，重新整理不用重打
    ctxD = uctx(webkit, **iphone); D = ctxD.new_page()
    enter(D, proj)
    D.fill("#student_id", "9994A004"); D.fill("#name", "涂演四")
    D.reload(); D.wait_for_selector("#student_id")
    check(D.locator("#student_id").input_value() == "9994A004", "重新整理後學號從 sessionStorage 復原")
    check(D.locator("#name").input_value() == "涂演四", "重新整理後姓名從 sessionStorage 復原")
    ctxD.close()

    # 學號欄全形英數轉半形，不是靜默刪掉
    ctxE = uctx(webkit, **iphone); E = ctxE.new_page()
    enter(E, proj)
    E.fill("#student_id", "９９９４Ａ００１")
    check(E.locator("#student_id").input_value() == "9994A001", "全形學號自動轉半形")
    ctxE.close()

    # 表單倒數到 0，按鈕變成「重新掃 QR 或改輸六位數」並連到 /c
    ctxF = uctx(webkit, **iphone); F = ctxF.new_page()
    enter(F, proj)
    F.wait_for_function("document.getElementById('countdown').innerText==='時間到'", timeout=35000)
    check("重新掃 QR" in F.locator("#go").inner_text() and "改輸六位數" in F.locator("#go").inner_text(), "逾時後按鈕文字改對")
    F.click("#go"); F.wait_for_url(f"{B}/c")
    check(True, "按鈕連到 /c")
    ctxF.close()

    # 連線中斷不是「瀏覽器太舊」（iPhone 與 Pixel 各一）
    def _drop_after_processed(route):
        # 規格明寫「route.fetch() 後 route.abort()」：先讓請求真的送到伺服器並處理完成（存進 DB），
        # 只是不讓瀏覽器收到那個回應——模擬「伺服器其實有收到，但連線在回應途中斷掉」，
        # 跟請求根本沒送到（單純 route.abort()）是兩回事，重送後才會查到「已經簽過」。
        route.fetch()
        route.abort()

    def _sign_on_device(page, device_label, sid_num, name, pin):
        enter(page, proj)
        page.fill("#student_id", sid_num); page.fill("#name", name); page.fill("#pin1", pin); page.fill("#pin2", pin)
        page.route("**/api/checkin", _drop_after_processed)
        page.click("#go")
        page.wait_for_function("document.getElementById('err').innerText.length>0")
        msg = page.locator("#err").inner_text()
        check("再按一次" in msg, f"{device_label} 連線中斷訊息含「再按一次」")
        check("瀏覽器太舊" not in msg, f"{device_label} 不再講「瀏覽器太舊」")
        page.unroute("**/api/checkin")
        page.click("#go")
        page.wait_for_selector("#receipt", state="visible")
        check("已經簽過" in page.locator("#rnote").inner_text(), f"{device_label} 解除攔截重送後註記「已經簽過」")

    ctxH1 = uctx(webkit, **iphone); H1 = ctxH1.new_page()
    _sign_on_device(H1, "iPhone", "9994A005", "測試5", "5050")
    ctxH1.close()

    pixel = p.devices["Pixel 7"]
    ctxH2 = uctx(chrome, **pixel); H2 = ctxH2.new_page()
    _sign_on_device(H2, "Pixel", "9994A006", "測試6", "5151")
    ctxH2.close()

    # 已綁手機的 whoami 兩次都失敗時的提示文字，與 pin_unchanged 收據註記
    # pin_unchanged 只在「這一場還沒簽過」時才看得到（同一場重送會先撞「已經簽過」的短路），
    # 所以第二段要換到一場新的場次：關掉目前這場、開一場新的，剩下的測試接著用這場新的 sid/proj。
    ctxJ = uctx(webkit, **iphone); J = ctxJ.new_page()
    enter(J, proj)
    J.fill("#student_id", "9994A007"); J.fill("#name", "測試7"); J.fill("#pin1", "6060"); J.fill("#pin2", "6060")
    J.click("#go"); J.wait_for_selector("#receipt", state="visible")
    check(True, "先用這支手機正常綁定一次")

    tp.once("dialog", lambda d: d.accept())  # closeSession() 有 confirm('結束點名？沒簽的會記缺席。')
    tp.goto(f"{B}/t/session/{sid}")
    tp.wait_for_selector("#closeBtn")
    tp.click("#closeBtn")
    tp.wait_for_timeout(300)
    tp.goto(f"{B}/t")
    tp.once("dialog", lambda d: d.accept())  # 剛結束的場次，主頁「開始點名」會先跳確認框；這一步本來就要開新的一場，接受它
    tp.locator("form[action$='/open'] button").first.click()
    tp.wait_for_url(re.compile(r"/t/session/\d+"))
    sid = int(tp.url.rsplit("/", 1)[1])
    proj = teacher.new_page(); proj.goto(f"{B}/t/projector/{sid}"); proj.wait_for_function("document.querySelector('#qr svg')!==null")

    J.route("**/api/whoami", lambda route: route.abort())
    enter(J, proj)
    check(J.locator("#modeHint").inner_text().find("連線不穩") >= 0, "whoami 兩次都失敗時 #modeHint 顯示「連線不穩」提示")
    # 首綁表單一定要填學號、姓名、PIN 兩次（validateForm），提示不可叫人「只填學號與 PIN」
    check("欄位都填完" in J.locator("#modeHint").inner_text(), "連線不穩的提示要叫人把欄位都填完（首綁表單要姓名與兩次 PIN）")
    check(J.locator("#firstFields").is_visible(), "whoami 兩次都失敗退回首綁表單（即使這支手機已經綁定過）")
    J.fill("#student_id", "9994A007"); J.fill("#name", "測試7"); J.fill("#pin1", "9999"); J.fill("#pin2", "9999")
    J.click("#go")
    J.wait_for_selector("#receipt", state="visible")
    check("PIN 沒有變更" in J.locator("#rnote").inner_text(), "已綁裝置被誤導填首綁表單時，收據註記 PIN 沒有變更")
    ctxJ.close()

    # 新 cookie 罐（換瀏覽器）、新的一場、輸對原本的 PIN → 自動改綁，收據說明改綁
    # 9994A005 在上一場由 H1（已關閉的 cookie 罐）綁定；這一場是新的，他還沒簽。
    ctxR = uctx(webkit, **iphone); R = ctxR.new_page()
    enter(R, proj)
    check(R.locator("#firstFields").is_visible(), "新 cookie 罐認不得，畫面是首次表單（提示可以直接填原本的 PIN 改綁）")
    check("改綁到這個瀏覽器" in R.locator("#modeHint").inner_text(), "首次表單的提示說明 PIN 填原本那組就會改綁")
    # iOS Safari 對小於 16px 的輸入欄聚焦時會整頁放大（第三方開發者報告，非 Apple 文件）
    for _f in ("student_id", "name", "pin1", "pin2"):
        _px = R.evaluate(f"parseFloat(getComputedStyle(document.getElementById('{_f}')).fontSize)")
        check(_px >= 16, f"#{_f} 字級 ≥ 16px，實得 {_px}")
    R.fill("#student_id", "9994A005"); R.fill("#name", "測試5"); R.fill("#pin1", "5050"); R.fill("#pin2", "5050")
    R.click("#go"); R.wait_for_selector("#receipt", state="visible")
    check("改綁到這裡" in R.locator("#rnote").inner_text(), f"收據說明改綁，實得 {R.locator('#rnote').inner_text()!r}")
    check(R.locator("#rstamp").inner_text() == "到", "改綁後照常記出席（紅色「到」章）")
    ctxR.close()

    # L7：送出時網路中斷（伺服器沒回），前端用 /api/beacon 回報 neterr
    ctxL7 = uctx(webkit, **iphone); L7 = ctxL7.new_page()
    beacons = []
    L7.on("request", lambda r: beacons.append(r.post_data or "") if "/api/beacon" in r.url else None)
    enter(L7, proj)
    L7.fill("#student_id", "9994A008"); L7.fill("#name", "測試8"); L7.fill("#pin1", "8080"); L7.fill("#pin2", "8080")
    L7.route("**/api/checkin", lambda route: route.abort())
    L7.click("#go")
    L7.wait_for_function("document.getElementById('err').innerText.includes('連線中斷')")
    L7.wait_for_timeout(500)
    # neterr 是網路壞了才有的回報，當場送一定送不出去，改成先放佇列；下一次請求有回應時才補送
    check(not any("neterr" in b for b in beacons), f"L7：網路中斷當下 neterr 先放佇列、不當場送（收到的 beacon：{beacons}）")
    L7.unroute("**/api/checkin")
    L7.click("#go")  # 學生再按一次送出：這次有回應，attendFlush 補送佇列
    L7.wait_for_timeout(1500)
    check(any("neterr" in b for b in beacons), f"L7：網路恢復、再送一次之後，補送 neterr 回報（收到的 beacon：{beacons}）")
    ctxL7.close()

    # 頁面沒載完，按鈕不能看起來可以按（student.html）
    # student.html 是「POST /c」直接把表單頁面當回應內容送回來（同一個 URL，不是導到 /s/.../.../...
    # 那種可以重複 GET 的連結），所以不能像 code.html 那樣先取原始碼再另開一頁 fulfill；改成攔截
    # 這次 POST 本身：先讓它真的送到伺服器換到完整內容（route.fetch()），截斷後再回給瀏覽器。
    # GET /c（頁面第一次載入的 code.html）要放行，不能一起截斷，否則連 code.html 都壞掉。
    def _truncate_student_post(route):
        if route.request.method != "POST":
            route.continue_(); return
        resp = route.fetch()
        body = resp.text()
        cut = body.index("const GRANT=")
        route.fulfill(response=resp, body=body[:cut])

    ctxTr1 = uctx(webkit, **iphone); tr1 = ctxTr1.new_page()
    tr1.route(f"{B}/c", _truncate_student_post)
    tr1.goto(f"{B}/c"); tr1.click("#otp input >> nth=0"); tr1.keyboard.type(code_of(proj))
    tr1.wait_for_selector("#go")
    check(tr1.eval_on_selector("#go", "el=>el.disabled"), "student.html 截斷時送出鈕停用")
    check("頁面還沒載完" in tr1.locator("#go").inner_text(), "student.html 截斷時按鈕文字提示重新掃描 QR")
    ctxTr1.close()

    ctxN1 = uctx(webkit, **iphone); n1 = ctxN1.new_page()
    enter(n1, proj)
    # 按鈕在 whoami 回來、判定完是首次還是回簽之前一律停用，所以「正常載入」的斷言要等
    # whoami 回來之後再量，不能一進頁面就量。
    n1.wait_for_function("!document.getElementById('go').disabled")
    check(not n1.eval_on_selector("#go", "el=>el.disabled"), "student.html 正常載入按鈕可按")
    check(n1.locator("#go").inner_text() == "簽到", "student.html 正常載入按鈕文字是「簽到」")
    ctxN1.close()

    # 頁面沒載完，按鈕不能看起來可以按（code.html）
    raw_code_html = teacher.request.get(f"{B}/c").text()
    cut_c = raw_code_html.index("const cells=")
    truncated_code = raw_code_html[:cut_c]

    ctxTr2 = uctx(webkit, **iphone); tr2 = ctxTr2.new_page()
    tr2.route(f"{B}/c", lambda route: route.fulfill(status=200, content_type="text/html; charset=utf-8", body=truncated_code))
    tr2.goto(f"{B}/c")
    check(tr2.eval_on_selector("#go", "el=>el.disabled"), "code.html 截斷時送出鈕停用")
    check("頁面還沒載完" in tr2.locator("#go").inner_text(), "code.html 截斷時按鈕文字提示重新掃描 QR")
    check("重新整理" in tr2.locator("#go").inner_text(), "輸碼頁不是掃 QR 進來的，截斷時要叫人重新整理這一頁")
    ctxTr2.close()

    ctxN2 = uctx(webkit, **iphone); n2 = ctxN2.new_page()
    n2.goto(f"{B}/c")
    check(not n2.eval_on_selector("#go", "el=>el.disabled"), "code.html 正常載入按鈕可按")
    check(n2.locator("#go").inner_text() == "下一步", "code.html 正常載入按鈕文字是「下一步」")
    ctxN2.close()

    # 綁定場次的收據章面字是「綁」，說明文字提醒下次上課掃碼即可
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
    check("綁定場次" in bproj.locator(".top").inner_text(), "投影頁標題列顯示「綁定場次」")

    ctxG = uctx(webkit, **iphone); G = ctxG.new_page()
    enter(G, bproj)
    G.fill("#student_id", "9994A010"); G.fill("#name", "測試10"); G.fill("#pin1", "6060"); G.fill("#pin2", "6060")
    G.click("#go")
    G.wait_for_selector("#receipt", state="visible")
    check(G.locator("#rstamp").inner_text() == "綁", f"綁定場次收據章面字是「綁」，實得 {G.locator('#rstamp').inner_text()!r}")
    check("下次上課掃碼後按一下就完成簽到" in G.locator("#rnote").inner_text(), "綁定場次收據說明文字正確")
    ctxG.close()

    # 綁定場次裡新環境輸對原本的 PIN 改綁，收據要同時說改綁與「不算出缺勤」
    # 9994A007 在第一場由 J 綁定（PIN 6060）；這一場綁定場次他還沒簽。
    ctxBr = uctx(webkit, **iphone); Br = ctxBr.new_page()
    enter(Br, bproj)
    Br.fill("#student_id", "9994A007"); Br.fill("#name", "測試7"); Br.fill("#pin1", "6060"); Br.fill("#pin2", "6060")
    Br.click("#go"); Br.wait_for_selector("#receipt", state="visible")
    _rn = Br.locator("#rnote").inner_text()
    check("改綁到這裡" in _rn and "不算出缺勤" in _rn, f"綁定場次改綁的收據同時說改綁與不算出缺勤，實得 {_rn!r}")
    ctxBr.close()

    # /me 逾時前一直卡在「讀取中」，加 10 秒逾時後跟其他失敗顯示同一句
    ctxMe = uctx(webkit, **iphone); mepg = ctxMe.new_page()
    mepg.route(f"{B}/api/me", lambda route: None)  # 掛著不回應，模擬上游卡住
    mepg.goto(f"{B}/me")
    mepg.wait_for_function("document.getElementById('out').innerText.includes('連線失敗')", timeout=11000)
    check(True, "/me 逾時（10 秒）後顯示「連線失敗」")
    mepg.unroute_all(behavior="ignoreErrors")
    ctxMe.close()

    # 倒數起始值改成 GRANT_SECONDS-5（最少 10 秒），不是滿額 GRANT_SECONDS
    # 用 bproj（綁定場次的投影頁）：這裡的 proj 指向的場次已經在綁定場次那一段結束前
    # 被結案，code_of(proj) 會拿到一個已經失效的六位數，進不了簽到表單。
    ctxCd = uctx(webkit, **iphone); cdpg = ctxCd.new_page()
    enter(cdpg, bproj)
    grant_seconds = cdpg.evaluate("GRANT_SECONDS")
    expected = max(10, grant_seconds - 5)
    # 倒數改成算 deadline（絕對時間戳）減目前時間，不是每秒遞減的變數 left（see student.html）
    left0 = cdpg.evaluate("Math.ceil((deadline-Date.now())/1000)")
    check(left0 <= expected and left0 >= expected - 3,
          f"倒數起始值接近 GRANT_SECONDS-5（GRANT_SECONDS={grant_seconds}，left={left0}，預期≈{expected}）")
    ctxCd.close()

    # 回簽模式送出時，綁定已經不是這個瀏覽器（老師剛解除）→ 切成只輸 PIN，輸對就簽到
    teacher.request.post(f"{B}/api/t/session/{bind_sid}/close")
    def open_normal():
        r = teacher.request.post(f"{B}/t/course/{COURSE_ID}/open", form={"kind": "normal"})
        n = int(re.search(r"/t/session/(\d+)", r.url).group(1))
        pj = teacher.new_page(); pj.goto(f"{B}/t/projector/{n}"); pj.wait_for_function("document.querySelector('#qr svg')!==null")
        return n, pj
    COURSE_ID = int(re.search(r"course/(\d+)/open", teacher.request.get(f"{B}/t").text()).group(1))
    n1, pj1 = open_normal()
    ctxR = uctx(webkit, **iphone); R = ctxR.new_page()
    enter(R, pj1)
    R.fill("#student_id", "9994A040"); R.fill("#name", "測試40"); R.fill("#pin1", "1234"); R.fill("#pin2", "1234"); R.click("#go")
    R.wait_for_selector("#receipt", state="visible")
    R.close(); pj1.close()
    teacher.request.post(f"{B}/api/t/session/{n1}/close")
    n2, pj2 = open_normal()
    R = ctxR.new_page(); R.goto(f"{B}/c"); R.click("#otp input >> nth=0"); R.keyboard.type(code_of(pj2))
    R.wait_for_selector("#returnFields", state="visible")
    R.wait_for_function("document.getElementById('go') && !document.getElementById('go').disabled && document.getElementById('go').textContent==='簽到'")
    check(R.is_visible("#returnFields") and not R.is_visible("#pin1"), "前置，已綁的瀏覽器開簽到頁是回簽模式（免 PIN）")
    # 回簽畫面按「不是我」，提示要說清楚借用別人手機會列待確認
    R2 = ctxR.new_page(); R2.goto(f"{B}/c"); R2.click("#otp input >> nth=0"); R2.keyboard.type(code_of(pj2))
    R2.wait_for_selector("#returnFields", state="visible")
    R2.click("#returnFields a")
    check("借用別人的手機" in R2.inner_text("#modeHint"), f"按「不是我」後 #modeHint 說明借用別人手機，實得 {R2.inner_text('#modeHint')!r}")
    R2.close()
    pk40 = next(r["pk"] for r in teacher.request.get(f"{B}/api/t/session/{n2}/state").json()["rows"] if r["student_id"] == "9994A040")
    teacher.request.post(f"{B}/api/t/student/{pk40}/unbind", form={"reset_pin": ""})
    unknown_return_posts = []  # 回簽模式被判不認得、切成只輸 PIN 時，不該帶 name_hint
    R.on("request", lambda r: unknown_return_posts.append(r.post_data or "") if "/api/checkin" in r.url else None)
    R.click("#go"); R.wait_for_function("document.getElementById('err').textContent.length>0")
    check(R.is_visible("#pin1"), "綁定被解除後按簽到，PIN 欄出現")
    check(R.inner_text("#err") == "這個瀏覽器已經不是你綁定的那一個（可能剛在別的瀏覽器改綁，或老師解除了綁定）。請輸入原本的 PIN。",
          f"提示文字，實得 {R.inner_text('#err')!r}")
    check(R.input_value("#student_id") == "9994A040" and not R.is_visible("#name"), "學號保留、姓名欄不出現（只輸 PIN）")
    R.fill("#pin1", "1234"); R.click("#go")
    R.wait_for_selector("#receipt", state="visible")
    check(R.locator("#rstamp").inner_text() in ("到", "遲"), f"輸入原 PIN 後拿到收據，實得 {R.locator('#rstamp').inner_text()!r}")
    check(len(unknown_return_posts) == 2 and all("name_hint" not in x for x in unknown_return_posts), f"回簽被判不認得的路徑兩次送出都沒有 name_hint，實得 {len(unknown_return_posts)} 筆")
    ctxR.close(); pj2.close()
    teacher.request.post(f"{B}/api/t/session/{n2}/close")

    # 同姓同字數打錯學號，切成只輸 PIN 之後要帶 name_hint，被擋事件不歸到同學
    import sqlite3 as _sq
    _dbp = E2E_DB
    _c = _sq.connect(_dbp, timeout=10)
    for _stu, _nm in (("999000001", "王測甲"), ("999000002", "王測乙")):
        _c.execute("INSERT INTO students(course_id,student_id,name,cls) VALUES(?,?,?,?)", (COURSE_ID, _stu, _nm, ""))
    _c.commit()
    n27, pj27 = open_normal()
    ctxY = uctx(webkit, **iphone); Y = ctxY.new_page()
    enter(Y, pj27); Y.fill("#student_id", "999000002"); Y.fill("#name", "王測乙"); Y.fill("#pin1", "5678"); Y.fill("#pin2", "5678"); Y.click("#go")
    Y.wait_for_selector("#receipt", state="visible"); ctxY.close()
    ctxJ = uctx(webkit, **iphone); J = ctxJ.new_page()
    posts27 = []
    J.on("request", lambda r: posts27.append(r.post_data or "") if "/api/checkin" in r.url else None)
    enter(J, pj27)
    J.fill("#student_id", "999000002"); J.fill("#name", "王測甲"); J.fill("#pin1", "9999"); J.fill("#pin2", "9999"); J.click("#go")
    J.wait_for_function("document.getElementById('err').innerText.includes('已經設過 PIN')")
    check(not J.locator("#nameField").is_visible(), "前置，切成只輸 PIN 模式")
    J.fill("#pin1", "9998"); J.click("#go")
    J.wait_for_function("document.getElementById('err').innerText.includes('剩 1 次')")
    check(len(posts27) == 2, f"共送出兩次，實得 {len(posts27)}")
    check('name="name_hint"' in posts27[1] and "王測甲" in posts27[1], "第二次帶 name_hint=王測甲")
    check(re.search(r'name="name"\r?\n\r?\n\r?\n', posts27[1]) is not None, "第二次 name 是空字串")
    check('name="name_hint"' not in posts27[0], "第一次（完整表單）沒有 name_hint")
    _rej = _c.execute("SELECT student_pk FROM events WHERE session_id=? AND kind='checkin_reject' ORDER BY id", (n27,)).fetchall()
    check(len(_rej) == 2 and all(x[0] is None for x in _rej), f"兩筆 checkin_reject 都沒有 student_pk，實得 {_rej}")
    ctxJ.close(); pj27.close(); _c.close()
    teacher.request.post(f"{B}/api/t/session/{n27}/close")

    # 場次快結束時，倒數看場次什麼時候結束，不只看憑證
    # 憑證時效在這一層是 30 秒（倒數從 25 秒起算）；用伺服器直接把這場的 late_until 改成 9 秒後，
    # 落地頁帶的 SESSION_LEFT 就比憑證剩餘短。
    import sqlite3, time as _time
    n3, pj3 = open_normal()
    _db = sqlite3.connect(E2E_DB, timeout=10)
    _db.execute("UPDATE sessions SET late_until=? WHERE id=?", (_time.time() + 9, n3)); _db.commit(); _db.close()
    ctxCd2 = uctx(webkit, **iphone); cd2 = ctxCd2.new_page()
    enter(cd2, pj3)
    cd2.wait_for_function("document.getElementById('countdown').innerText.includes('點名再')", timeout=5000)
    check(True, "場次剩不到憑證時效時，倒數顯示「點名再 N 秒結束」")
    cd2.wait_for_function("document.getElementById('countdown').innerText.includes('點名時間到了')", timeout=15000)
    check(not cd2.eval_on_selector("#go", "el=>el.disabled"), "場次到點但憑證還沒到期，按鈕維持可按（交給伺服器判斷）")
    ctxCd2.close(); pj3.close()

    # 送出時沒網路，回報先存佇列（放 localStorage），網路回來才補送，且帶得到場次號
    # 從 /c 進表單（網址沒有場次號），所以場次號靠 window.ATTEND_SID。Chromium 與 WebKit 各跑一次。
    for _label, _browser, _ctxargs, _stu in (("Chromium", chrome, {**p.devices["Pixel 5"]}, "9994A041"), ("WebKit", webkit, iphone, "9994A042")):
        _n, _pj = open_normal()
        _ctx = uctx(_browser, **_ctxargs); _pg = _ctx.new_page()
        enter(_pg, _pj)
        check(_pg.evaluate("String(window.ATTEND_SID)") == str(_n), f"{_label}：表單頁 window.ATTEND_SID 等於場次號")
        _pg.fill("#student_id", _stu); _pg.fill("#name", "測試"); _pg.fill("#pin1", "1234"); _pg.fill("#pin2", "1234")
        _ctx.set_offline(True)
        _pg.click("#go")
        _pg.wait_for_function("document.getElementById('err').innerText.length>0", timeout=20000)
        check(_pg.evaluate("(JSON.parse(localStorage.getItem('attendBeaconQ')||'[]')).length") >= 1,
              f"{_label}：離線送出失敗後回報進了 localStorage 佇列（不是 sessionStorage）")
        _ctx.set_offline(False)
        _db = sqlite3.connect(E2E_DB, timeout=10)
        _hit = None
        _deadline = _time.time() + 15
        while _time.time() < _deadline:
            _hit = _db.execute("SELECT reason, session_id FROM events WHERE kind='client' AND session_id=? AND reason LIKE 'neterr:%'", (_n,)).fetchone()
            if _hit: break
            _time.sleep(0.5)
        _db.close()
        check(_hit is not None, f"瀏覽器 {_label}：網路恢復後數秒內 events 有 neterr 回報且 session_id={_n}，實得 {_hit!r}")
        check(_hit is not None and "(+" in _hit[0], f"瀏覽器 {_label}：補送的訊息註明晚了幾秒，實得 {_hit!r}")
        _ctx.close(); _pj.close()
        teacher.request.post(f"{B}/api/t/session/{_n}/close")

    # 離線回報佇列與節流
    _e2edb = E2E_DB

    def _q(sql, *args):
        _c = sqlite3.connect(_e2edb, timeout=10)
        try: return _c.execute(sql, args).fetchall()
        finally: _c.close()

    def _poll(cond, timeout=15.0, step=0.3):
        """輪詢實際條件到逾時為止（不猜固定秒數）。"""
        end = _time.time() + timeout
        while _time.time() < end:
            v = cond()
            if v: return v
            _time.sleep(step)
        return cond()

    def _fill_form(pg, stu, nm, pin):
        pg.fill("#student_id", stu); pg.fill("#name", nm); pg.fill("#pin1", pin); pg.fill("#pin2", pin)

    _browsers = (("Chromium", chrome, dict(p.devices["Pixel 5"])), ("WebKit", webkit, iphone))

    # 伺服器有回應但不是 JSON（502 的 HTML），neterr:http 502 要當場送，不排進這個分頁的佇列
    for _label, _br, _args in _browsers:
        _n, _pj = open_normal()
        _ctx = uctx(_br, **_args); _pg = _ctx.new_page()
        _bq = []
        _pg.on("request", lambda r, _bq=_bq: _bq.append(r.post_data or "") if "/api/beacon" in r.url else None)
        enter(_pg, _pj)
        _fill_form(_pg, "9994A044", "測試44", "4444")
        _pg.route("**/api/checkin", lambda route: route.fulfill(status=502, content_type="text/html", body="<html><body>Bad Gateway</body></html>"))
        _pg.click("#go")
        _pg.wait_for_function("document.getElementById('err').innerText.length>0")
        check(_poll(lambda: any("neterr" in b for b in _bq), timeout=3), f"瀏覽器 {_label}：502 HTML 回應後 3 秒內當場送出 /api/beacon，實得 {_bq}")
        _hit = _poll(lambda: _q("SELECT reason FROM events WHERE kind='client' AND session_id=? AND reason LIKE 'neterr:http 502%'", _n), timeout=5)
        check(bool(_hit), f"瀏覽器 {_label}：伺服器有 client 事件 neterr:http 502，實得 {_hit!r}")
        _ctx.close(); _pj.close()
        teacher.request.post(f"{B}/api/t/session/{_n}/close")

    # 斷網按 8 次 → 恢復後第一次 flush 只送 5 筆、佇列剩 3；61 秒後的下一次 flush 送完。
    # 伺服器每裝置每分鐘只收 6 筆，所以第二輪補送一定要等到第一輪滿 60 秒之後——這是計時行為本身，不是猜時間：
    # 兩個瀏覽器的準備與等待重疊進行，輪詢「佇列清空」這個實際條件，逾時 100 秒。
    _n2, _pj2 = open_normal()
    _runs = []
    for _label, _br, _args in _browsers:
        _ctx = uctx(_br, **_args); _pg = _ctx.new_page()
        enter(_pg, _pj2)
        _fill_form(_pg, "9994A045", "測試45", "4545")
        _ctx.set_offline(True)
        for _i in range(8):
            _pg.click("#go")
            _pg.wait_for_function("document.getElementById('err').innerText.includes('連線中斷') && !document.getElementById('go').disabled")
        _qlen = lambda pg=_pg: pg.evaluate("(JSON.parse(localStorage.getItem('attendBeaconQ')||'[]')).length")
        check(_qlen() == 8, f"瀏覽器 {_label}：斷網按 8 次後 localStorage 佇列有 8 筆，實得 {_qlen()}")
        _ctx.set_offline(False)
        if not _poll(lambda q=_qlen: q() <= 3, timeout=4):
            _pg.evaluate("attendFlush()")  # 這個瀏覽器的 setOffline(false) 沒有觸發 online 事件，手動補一次（仍只算第一次 flush）
        check(_poll(lambda q=_qlen: q() == 3, timeout=6), f"瀏覽器 {_label}：恢復後第一次 flush 只送 5 筆、佇列剩 3，實得 {_qlen()}")
        _runs.append((_label, _qlen, _ctx))
    _t_first = _time.time()
    for _label, _qlen, _ctx in _runs:
        check(_poll(lambda q=_qlen: q() == 0, timeout=100, step=1.0), f"瀏覽器 {_label}：約 61 秒後的下一次 flush 把佇列送完，剩 {_qlen()}")
    _per_dev = _q("SELECT device8, COUNT(*) FROM events WHERE kind='client' AND session_id=? AND reason LIKE 'neterr:%' GROUP BY device8", _n2)
    check(len(_per_dev) == 2 and all(c == 8 for _, c in _per_dev), f"兩個瀏覽器各 8 筆 client 事件，實得 {_per_dev}")
    _drop = _q("SELECT COUNT(*) FROM events WHERE kind='beacon_dropped' AND session_id=?", _n2)[0][0]
    check(_drop == 0, f"沒有 beacon_dropped，實得 {_drop}")
    for _, _, _ctx in _runs: _ctx.close()
    _pj2.close(); teacher.request.post(f"{B}/api/t/session/{_n2}/close")

    # 舊分頁留下的佇列，同一個 context 開新分頁重掃就補送（localStorage 跨分頁）
    for _label, _br, _args in _browsers:
        _n3, _pj3 = open_normal()
        _ctx = uctx(_br, **_args); _old = _ctx.new_page()
        enter(_old, _pj3)
        _fill_form(_old, "9994A046", "測試46", "4646")
        _old.route("**/api/checkin", lambda route: None)  # 攔住不回應，等 10 秒逾時
        _old.click("#go")
        _old.wait_for_function("document.getElementById('err').innerText.includes('連線中斷')", timeout=20000)
        check(_old.evaluate("(JSON.parse(localStorage.getItem('attendBeaconQ')||'[]')).length") >= 1, f"{_label}：前置，逾時後舊分頁的佇列有東西")
        _old.close()
        _new = _ctx.new_page(); enter(_new, _pj3)
        _hit = _poll(lambda: _q("SELECT reason FROM events WHERE kind='client' AND session_id=? AND reason LIKE 'neterr:%'", _n3), timeout=10)
        check(bool(_hit), f"瀏覽器 {_label}：同 context 開新分頁重掃後，伺服器收到舊分頁留下的 neterr，實得 {_hit!r}")
        _ctx.close(); _pj3.close(); teacher.request.post(f"{B}/api/t/session/{_n3}/close")

    # 被回「點名已經結束。」之後，畫面看得到叫人重新掃 QR 的那一句（WebKit）
    _n4, _pj4 = open_normal()
    _ctx = uctx(webkit, **iphone); _pg = _ctx.new_page()
    enter(_pg, _pj4)
    _fill_form(_pg, "9994A046", "測試46", "4646")
    teacher.request.post(f"{B}/api/t/session/{_n4}/close")
    _pg.click("#go")
    _pg.wait_for_function("document.getElementById('err').innerText.includes('點名已經結束')")
    _hint = _pg.locator("#reopenHint")
    check(_hint.count() == 1 and _hint.is_visible() and "老師如果說取消結束了" in _hint.inner_text(),
          f"被回「點名已經結束。」後畫面上看得到「老師如果說取消結束了」，元素數 {_hint.count()}")
    check(_pg.locator("#go").is_disabled() and _pg.locator("#go").inner_text() == "點名已結束", "markClosed(false) 的按鈕停用與文字不變")
    _ctx.close(); _pj4.close()

    # PIN 鎖定之後，前端不能再切成「只輸 PIN」並說輸對就改用這個瀏覽器（WebKit）
    _n5, _pj5 = open_normal()
    _ctx = uctx(webkit, **iphone); _pg = _ctx.new_page()
    enter(_pg, _pj5); _fill_form(_pg, "9994A047", "測試47", "4747"); _pg.click("#go"); _pg.wait_for_selector("#receipt", state="visible")
    _ctx.close(); _pj5.close(); teacher.request.post(f"{B}/api/t/session/{_n5}/close")
    _n6, _pj6 = open_normal()
    _ctx = uctx(webkit, **iphone); _pg = _ctx.new_page()
    for _i, _wrong in enumerate(("1111", "2222", "3333")):
        enter(_pg, _pj6)
        if _i == 0:
            _fill_form(_pg, "9994A047", "測試47", _wrong)
        else:
            # 第一次被擋（已經設過 PIN）之後重新進頁面，伺服器認得這支瀏覽器剛對這個學號打過 PIN，
            # 直接是只輸 PIN 的畫面（姓名欄隱藏、學號已填），所以後兩次只打 PIN；原本每次重填整張表單的前置在 D1 之後不成立
            _pg.wait_for_function("document.getElementById('modeHint').innerText.includes('已經設過 PIN')", timeout=15000)
            _pg.tap("#pin1"); _pg.keyboard.type(_wrong)
        _pg.click("#go")
        _pg.wait_for_function("document.getElementById('err').innerText.length>0")
    check("錯太多次" in _pg.locator("#err").inner_text(), f"前置，第三次的訊息說錯太多次，實得 {_pg.locator('#err').inner_text()!r}")
    check(_pg.locator("#go").is_disabled() and _pg.locator("#go").inner_text() == "這一場請找老師",
          f"鎖定後按鈕停用、文字「這一場請找老師」，實得 {_pg.locator('#go').inner_text()!r} disabled={_pg.locator('#go').is_disabled()}")
    check("輸對就會改用這個瀏覽器簽到" not in _pg.locator("#modeHint").inner_text(), f"畫面沒有「輸對就會改用這個瀏覽器簽到」，實得 {_pg.locator('#modeHint').inner_text()!r}")
    _ctx.close(); _pj6.close(); teacher.request.post(f"{B}/api/t/session/{_n6}/close")

    chrome.close(); webkit.close()

# 學生端前端回歸獨立成子程序跑（同 e2e_teacher_friction_playwright.py
# 呼叫 e2e_out_of_order.py 的手法），細節見 e2e_student_front.py 檔頭 ──
_r = subprocess.run([sys.executable, os.path.join(os.path.dirname(__file__), "e2e_student_front.py")],
                    capture_output=True, text=True)
print(_r.stdout, end="")
if _r.stderr:
    print(_r.stderr, file=sys.stderr, end="")
if _r.returncode != 0:
    FAIL.append("e2e_student_front.py 有項目沒過（見上方輸出）")

# 學生端輸入行為回歸，獨立成子程序（同上一段手法）；
# 輸入一律 tap＋keyboard.type，不用 fill()，細節見 e2e_student_input.py 檔頭 ──
_r = subprocess.run([sys.executable, os.path.join(os.path.dirname(__file__), "e2e_student_input.py")],
                    capture_output=True, text=True)
print(_r.stdout, end="")
if _r.stderr:
    print(_r.stderr, file=sys.stderr, end="")
if _r.returncode != 0:
    FAIL.append("e2e_student_input.py 有項目沒過（見上方輸出）")

# 學生端重新整理、鍵盤與縮放回歸，獨立成子程序（同上一段手法）；
# 輸入一律 tap＋keyboard.type，不用 fill()，細節見 e2e_student_refresh.py 檔頭 ──
_r = subprocess.run([sys.executable, os.path.join(os.path.dirname(__file__), "e2e_student_refresh.py")],
                    capture_output=True, text=True)
print(_r.stdout, end="")
if _r.stderr:
    print(_r.stderr, file=sys.stderr, end="")
if _r.returncode != 0:
    FAIL.append("e2e_student_refresh.py 有項目沒過（見上方輸出）")

print("\n==>", "全部通過" if not FAIL else f"{len(FAIL)} 項失敗：{FAIL}")
sys.exit(1 if FAIL else 0)
