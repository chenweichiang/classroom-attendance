"""老師端與投影頁摩擦的前端回歸。
前提同 e2e_friction_playwright.py（乾淨庫、ATTEND_TEST_BASE 預設埠、NTUB-CD 48 人名冊）；
gate.sh 的 e2e-teacher 層另外把這門課的 qr_interval 設成最低值 5 秒，讓卡格測試不用等 10 秒以上。"""
import json, os, re, subprocess, sys, time
from playwright.sync_api import sync_playwright

# 直接讀寫伺服器資料庫的路徑從 ATTEND_E2E_DB 取（gate.sh 傳 $LOGDIR 底下的唯一路徑，跟伺服器用同一個檔）；
# 沒給就落在 tempfile.mkdtemp()，不寫死 /tmp 底下的固定檔名（兩個閘門同時跑會互踩）
import tempfile
E2E_DB = os.environ.get("ATTEND_E2E_DB") or os.path.join(tempfile.mkdtemp(prefix="attend-e2e-"), "e2e-teacher.db")

B = os.environ.get("ATTEND_TEST_BASE", "http://127.0.0.1:8765"); FAIL = []
def check(c, m):
    print(("ok   " if c else "FAIL ") + m)
    if not c: FAIL.append(m)


def wait_for_phase(teacher_req, sid, phase, timeout_s=90):
    """輪詢真正的伺服器狀態直到進入指定 phase，不是猜一個固定秒數就當作到了。"""
    deadline = time.time() + timeout_s
    last = None
    while time.time() < deadline:
        last = teacher_req.get(f"{B}/api/t/session/{sid}/qr").json()["phase"]
        if last == phase:
            return True
        time.sleep(1)
    print(f"  wait_for_phase 逾時：sid={sid} 要等的 phase={phase!r}，最後看到的是 {last!r}")
    return False


def student_grant(teacher_req, phone_req, sid):
    code = teacher_req.get(f"{B}/api/t/session/{sid}/qr").json()["code"]
    html = phone_req.post(f"{B}/c", form={"code": code}).text()
    return re.search(r'GRANT="([^"]+)"', html).group(1)

with sync_playwright() as p:
    chrome = p.chromium.launch()
    teacher = chrome.new_context(viewport={"width": 1280, "height": 900})
    tp = teacher.new_page()
    tp.goto(f"{B}/t/login"); tp.fill("input[name=password]", "pw"); tp.click("button:has-text('登入')"); tp.wait_for_url(f"{B}/t")

    # teacher-ip 要補送（先開一場暫時的場次測完就關掉，不佔用下面正式 sid 的名額）
    cid_home = int(re.search(r"course/(\d+)/open", tp.content()).group(1))
    tmp_open = teacher.request.post(f"{B}/t/course/{cid_home}/open", form={"kind": "normal"})
    tmp_sid = int(re.search(r"/t/session/(\d+)", tmp_open.url).group(1))
    ippg = teacher.new_page()
    ippg.route(f"**/api/t/session/{tmp_sid}/teacher-ip", lambda route: route.abort())
    ippg.goto(f"{B}/t/projector/{tmp_sid}")
    ippg.wait_for_function("document.querySelector('#qr svg')!==null")
    st0 = teacher.request.get(f"{B}/api/t/session/{tmp_sid}/state").json()
    check(not st0["teacher_ip"], "第一次 teacher-ip 被掛住時場次還沒記到教室 IP")
    ippg.unroute(f"**/api/t/session/{tmp_sid}/teacher-ip")
    ippg.wait_for_timeout(3000)  # 給下一次取碼成功時補送一次的機會
    st1 = teacher.request.get(f"{B}/api/t/session/{tmp_sid}/state").json()
    check(bool(st1["teacher_ip"]), "解除攔截後數秒內伺服器 teacher_ip 有值")
    ippg.close()

    # teacher-ip 補送要有 ipInFlight 旗標＋逾時，上游卡住時不能越疊越多
    # 上面已經讓 tmp_sid 記過 teacher_ip，在同一場次上驗「解除攔截後有值」恆真（拿掉旗標的錯誤也綠）。
    # 改用一個前面沒記過 teacher_ip 的新場次，並先確認卡住期間確實還沒有值。
    teacher.request.post(f"{B}/api/t/session/{tmp_sid}/close")
    sid116 = int(re.search(r"/t/session/(\d+)", teacher.request.post(f"{B}/t/course/{cid_home}/open", form={"kind": "normal"}).url).group(1))
    check(not teacher.request.get(f"{B}/api/t/session/{sid116}/state").json()["teacher_ip"], "新場次一開始沒有 teacher_ip（前面沒記過）")
    ip116pg = teacher.new_page()
    reqs116 = []
    ip116pg.on("request", lambda r: reqs116.append(r) if f"/api/t/session/{sid116}/teacher-ip" in r.url else None)
    ip116pg.route(f"**/api/t/session/{sid116}/teacher-ip", lambda route: None)  # 掛住不回應，模擬上游卡住
    ip116pg.goto(f"{B}/t/projector/{sid116}")
    ip116pg.wait_for_function("document.querySelector('#qr svg')!==null")
    reqs116.clear()
    ip116pg.wait_for_timeout(10000)
    check(len(reqs116) <= 4, f"10 秒內卡住的 teacher-ip 請求數 ≤ 4（4 秒逾時一輪），實得 {len(reqs116)}")
    check(not teacher.request.get(f"{B}/api/t/session/{sid116}/state").json()["teacher_ip"], "卡住期間伺服器還沒記到 teacher_ip")
    ip116pg.unroute(f"**/api/t/session/{sid116}/teacher-ip")
    # 輪詢伺服器真實狀態，最多等 15 秒；不是猜一個固定秒數
    deadline116 = time.time() + 15
    while time.time() < deadline116 and not teacher.request.get(f"{B}/api/t/session/{sid116}/state").json()["teacher_ip"]:
        time.sleep(0.5)
    st116 = teacher.request.get(f"{B}/api/t/session/{sid116}/state").json()
    check(bool(st116["teacher_ip"]), "解除攔截後 15 秒內，新場次的伺服器 teacher_ip 有值（逾時補送有效）")
    ip116pg.close()
    tmp_sid = sid116  # 下面統一收掉這一場

    teacher.request.post(f"{B}/api/t/session/{tmp_sid}/close")
    tp.goto(f"{B}/t")

    tp.once("dialog", lambda d: d.accept())  # 剛結束的場次，主頁「開始點名」會先跳確認框；這一步本來就要開新的一場，接受它
    tp.locator("form[action$='/open'] button").first.click(); tp.wait_for_url(re.compile(r"/t/session/\d+")); sid = int(tp.url.rsplit("/", 1)[1])

    # 手動簽到只用既有 /api/t/session/{sid}/status，輸後 3 碼或姓名片段給候選人
    sess = tp  # 就是點名頁那分頁
    sess.wait_for_selector("#rows tr")
    posts = []
    sess.on("request", lambda r: posts.append(r.post_data) if "/status" in r.url and r.method == "POST" else None)

    sess.fill("#mid", "測試7")
    sess.wait_for_function("document.getElementById('mcands').innerText.includes('測試7')")
    check("測試7" in sess.locator("#mcands").inner_text(), "姓名片段找到候選人")
    check(sess.locator("#mcands button:has-text('出席')").count() == 1, "候選人旁有「出席」鈕")
    check(sess.locator("#mcands button:has-text('遲到')").count() == 1, "候選人旁有「遲到」鈕")

    sess.fill("#mid", "A047")
    sess.wait_for_function("document.getElementById('mcands').innerText.includes('9994A047')")
    check("9994A047" in sess.locator("#mcands").inner_text(), "學號後 4 碼找到唯一候選人")
    sess.locator("#mcands").get_by_text("遲到").click()
    sess.wait_for_timeout(300)
    check(any(pd and 'name="status"' in pd and "late" in pd for pd in posts), "點「遲到」鈕直接呼叫既有 status API 記遲到")
    check(sess.locator("#mid").input_value() == "", "送出後輸入框清空")

    posts.clear()
    sess.fill("#mid", "A048")
    sess.wait_for_function("document.getElementById('mcands').innerText.includes('9994A048')")
    sess.locator("#mid").press("Enter")
    sess.wait_for_timeout(300)
    check(any(pd and 'name="status"' in pd and "present" in pd for pd in posts), "唯一候選人時按 Enter 直接記出席（不必再點鈕）")
    sess.fill("#mid", "")

    # 手動簽到失敗要說是誰、不要清掉輸入
    sess.route(f"**/api/t/session/{sid}/status", lambda route: route.abort())
    sess.fill("#mid", "A046")
    sess.wait_for_function("document.getElementById('mcands').innerText.includes('9994A046')")
    sess.locator("#mcands").get_by_text("出席").click()
    sess.wait_for_function("document.getElementById('mfails').innerText.includes('9994A046')", timeout=15000)
    check("沒有送到" in sess.locator("#mfails").inner_text(), "失敗訊息含那一筆的學號與說明")
    check(sess.locator("#mid").input_value() == "A046", "失敗時輸入框仍是老師正在打的內容")
    sess.unroute(f"**/api/t/session/{sid}/status")
    sess.locator("#mcands").get_by_text("出席").click()
    sess.wait_for_function("!document.getElementById('mfails').innerText.includes('9994A046')", timeout=15000)
    check(True, "再按一次該筆成功後訊息消失")
    sess.fill("#mid", "")

    # 手動簽到失敗訊息要照「遮蔽姓名」開關顯示；伺服器明確拒絕時要講原因，不是「沒有送到」
    # 這裡點「遲到」而不是「出席」——9994A046 在上面手動簽到那段
    # 已經被成功設成「出席」，如果這裡失敗清單也記「想改成出席」，自動 reconcile會看到
    # 「現在的狀態已經等於當時想改成的狀態」而立刻把這幾筆失敗清單清掉，跟這裡要測的「失敗訊息該
    # 怎麼顯示」互相打架（不是 reconcile 邏輯本身錯，是這裡沿用同一位學生、狀態剛好對上）。改成
    # 「遲到」跟目前的「出席」不同，兩件事就不會混在一起。
    full_name = sess.evaluate("state.rows.find(r=>r.student_id==='9994A046').name")
    # 9994A046 目前是出席，按「遲到」會先跳確認框；這段要測的是送出後的失敗訊息，接受它（段尾移除監聽）
    late_confirm_msgs = []
    def _late_dialog(d):
        late_confirm_msgs.append(d.message); d.accept()
    sess.on("dialog", _late_dialog)
    sess.evaluate("setMaskOn(true); render()")
    sess.route(f"**/api/t/session/{sid}/status", lambda route: route.abort())
    sess.fill("#mid", "A046")
    sess.wait_for_function("document.getElementById('mcands').innerText.includes('9994A046')")
    sess.locator("#mcands").get_by_text("遲到").click()
    sess.wait_for_function("document.getElementById('mfails').innerText.includes('9994A046')", timeout=15000)
    check(full_name not in sess.locator("#mfails").inner_text(), "遮蔽姓名開著時，失敗訊息不顯示完整姓名")
    sess.unroute(f"**/api/t/session/{sid}/status")
    sess.evaluate("setMaskOn(false); render()")
    sess.route(f"**/api/t/session/{sid}/status", lambda route: route.fulfill(
        status=409, content_type="application/json", body='{"ok":false,"error":"測試用：伺服器拒絕這筆"}'))
    sess.locator("#mcands").get_by_text("遲到").click()
    sess.wait_for_function("document.getElementById('mfails').innerText.includes('測試用：伺服器拒絕這筆')", timeout=15000)
    check("沒有送到" not in sess.locator("#mfails").inner_text(), "伺服器明確拒絕時顯示拒絕原因，不說「沒有送到」")
    sess.unroute(f"**/api/t/session/{sid}/status")
    sess.locator("#mcands").get_by_text("遲到").click()
    sess.wait_for_function("!document.getElementById('mfails').innerText.includes('9994A046')", timeout=15000)
    sess.fill("#mid", "")
    sess.remove_listener("dialog", _late_dialog)
    check(len(late_confirm_msgs) >= 1 and all("確定改成遲到" in m for m in late_confirm_msgs), f"對出席的人按遲到先跳確認框，實得 {late_confirm_msgs!r}")

    # 點名頁輪詢壞掉要亮「連線不穩」（state 回應截半連續 3 次）
    sess.route(f"**/api/t/session/{sid}/state", lambda route: route.fulfill(status=200, content_type="application/json", body='{"rows"'))
    sess.wait_for_function("document.getElementById('net').style.display===''", timeout=10000)
    check(sess.locator("#net").is_visible(), "state 回應截半時 #net 顯示「連線不穩」")
    sess.unroute(f"**/api/t/session/{sid}/state")
    sess.wait_for_function("document.getElementById('net').style.display==='none'", timeout=10000)
    check(True, "恢復正常回應後 #net 隱藏")

    # 遮姓名，預設關、狀態存 localStorage、旗標不受影響
    sess.reload(); sess.wait_for_selector("#rows tr")
    check("遮姓名：已關閉" in sess.locator("#maskBtn").inner_text(), "預設關閉")
    full_name_row = sess.locator("#rows tr").first.inner_text()
    sess.click("#maskBtn")
    sess.wait_for_function("document.getElementById('maskBtn').innerText.includes('開啟中')")
    check("○○" in sess.locator("#rows tr").first.inner_text(), "開啟後姓名遮成「姓○○」")
    sess.reload(); sess.wait_for_selector("#rows tr")
    check("○○" in sess.locator("#rows tr").first.inner_text(), "重新整理後仍記得開啟（localStorage）")
    sess.click("#maskBtn")  # 關掉，不影響後面的測試
    sess.wait_for_function("document.getElementById('maskBtn').innerText.includes('已關閉')")

    # 投影頁 QR 尺寸與無捲軸，三個目標視窗
    proj = teacher.new_page(); proj.goto(f"{B}/t/projector/{sid}")
    proj.wait_for_function("document.querySelector('#qr svg')!==null")
    baseline_code_font = None
    for w, h in [(1920, 1080), (1366, 768), (1024, 768)]:
        proj.set_viewport_size({"width": w, "height": h})
        proj.wait_for_timeout(250)
        box = proj.locator("#qr").bounding_box()
        assert box, "#qr 沒有版面（頁面沒渲染出來）"
        short = min(w, h)
        scrollW = proj.evaluate("document.documentElement.scrollWidth")
        scrollH = proj.evaluate("document.documentElement.scrollHeight")
        font_px = float(proj.evaluate("getComputedStyle(document.getElementById('code')).fontSize").replace("px", ""))
        print(f"  量測 {w}x{h}：QR={box['width']:.0f}x{box['height']:.0f}（短邊 {short} 的 {box['width']/short*100:.1f}%），"
              f"scroll={scrollW}x{scrollH}，六位數字體={font_px:.1f}px")
        check(box["width"] >= short * 0.85 and box["height"] >= short * 0.85, f"視窗 {w}x{h} QR 邊長 ≥ 短邊 85%")
        check(scrollW <= w and scrollH <= h, f"視窗 {w}x{h} 沒有捲軸")
        # body 是 overflow:hidden，「沒有捲軸」永遠成立；真正要驗的是 QR 整顆在畫面內、沒被上下資訊列壓到
        top_box = proj.locator(".top").bounding_box(); bot_box = proj.locator(".bot").bounding_box()
        assert box and top_box and bot_box
        check(box["y"] >= top_box["y"] + top_box["height"] - 0.5 and box["y"] + box["height"] <= bot_box["y"] + 0.5
              and box["x"] >= 0 and box["x"] + box["width"] <= w,
              f"視窗 {w}x{h} QR 整顆落在上下資訊列之間（QR y={box['y']:.0f}–{box['y']+box['height']:.0f}，"
              f"上列底={top_box['y']+top_box['height']:.0f}，下列頂={bot_box['y']:.0f}）")
        expect_font = min(9 * h / 100, 12 * w / 100, 110)
        check(font_px >= expect_font - 0.5, f"視窗 {w}x{h} 六位數字體沒有比既有公式（min(9vh,12vw)）小（實測 {font_px:.1f}px，基準 {expect_font:.1f}px）")
    proj.set_viewport_size({"width": 1280, "height": 900})
    proj.close()  # 驗證到此為止；這頁留著不關會一路追著同一堂課（cid_home）之後每一場新開的
    # 投影頁網址，汙染後面用同一堂課測「投影頁沒連上」的情境。

    # 投影頁連不上時要說清楚，而且不要叫人重新整理（全新分頁，模擬「還沒成功過就失敗」）
    ctxNet = chrome.new_context(viewport={"width": 1280, "height": 900})
    netpg = ctxNet.new_page()
    netpg.goto(f"{B}/t/login"); netpg.fill("input[name=password]", "pw"); netpg.click("button:has-text('登入')"); netpg.wait_for_url(f"{B}/t")
    netpg.route(f"**/api/t/session/{sid}/qr*", lambda route: route.abort())
    netpg.route(f"**/api/t/session/{sid}/teacher-ip", lambda route: route.abort())
    netpg.goto(f"{B}/t/projector/{sid}")
    netpg.wait_for_function("document.getElementById('phase').innerText.includes('連線中')", timeout=1500)
    check(True, "還沒成功過就取碼失敗時 #phase 顯示「連線中」")
    netpg.wait_for_function("getComputedStyle(document.getElementById('netmsg')).display==='flex'", timeout=6000)
    msg_text = netpg.locator("#netmsg").inner_text()
    check("連不到伺服器" in msg_text, "遮罩第一行文案「連不到伺服器，可能是網路問題」")
    check("不用重新整理" in msg_text, "遮罩第二行提到不用重新整理")
    check("請重新整理這一頁" not in msg_text, "不再叫人重新整理")
    netpg.unroute(f"**/api/t/session/{sid}/qr*")
    netpg.unroute(f"**/api/t/session/{sid}/teacher-ip")
    netpg.wait_for_function("document.querySelector('#qr svg')!==null", timeout=10000)
    check(netpg.locator("#netmsg").is_hidden(), "解除攔截後 10 秒內 QR 出現、#netmsg 隱藏")
    ctxNet.close()

    # 頁面載入了，取碼請求一直卡著不回（不是立刻失敗）
    # 主腳本已經在跑，看門狗不可蓋上「請重新整理」；要看到整片的「連不到伺服器……不用重新整理」（還沒有 QR，不是邊條）
    ctxHang = chrome.new_context(viewport={"width": 1280, "height": 900})
    hgpg = ctxHang.new_page()
    hgpg.goto(f"{B}/t/login"); hgpg.fill("input[name=password]", "pw"); hgpg.click("button:has-text('登入')"); hgpg.wait_for_url(f"{B}/t")
    hgpg.route(f"**/api/t/session/{sid}/qr*", lambda route: None)  # 不 continue、不 fulfill：請求掛著，直到頁面自己逾時放棄
    hgpg.route(f"**/api/t/session/{sid}/teacher-ip", lambda route: None)
    hgpg.goto(f"{B}/t/projector/{sid}")
    hgpg.wait_for_timeout(8000)  # 看門狗 6 秒；取碼逾時 4 秒，這時至少失敗過一次
    check(hgpg.locator("#__projWatchdog").count() == 0, "主腳本已在跑、只是取碼卡住時，看門狗不出現（不叫人重新整理）")
    check(hgpg.evaluate("getComputedStyle(document.getElementById('netmsg')).display") == "flex", "取碼卡住時顯示網路訊息")
    check(not hgpg.evaluate("document.getElementById('netmsg').classList.contains('edge')"), "還沒出現過 QR 時網路訊息整片顯示，不是邊條")
    check("不用重新整理" in hgpg.locator("#netmsg").inner_text(), "網路訊息說不用重新整理")
    # 標題與說明上下排（flex 容器裡的 <br> 不換行）；「已 N 秒」每秒更新，不是等每次取碼逾時才跳
    check(hgpg.evaluate("getComputedStyle(document.getElementById('netmsg')).flexDirection") == "column", "網路訊息的標題與說明上下排，不是左右兩欄")
    s1 = int(re.search(r"已 (\d+) 秒", hgpg.locator("#netmsg").inner_text()).group(1))
    hgpg.wait_for_timeout(2200)
    s2 = int(re.search(r"已 (\d+) 秒", hgpg.locator("#netmsg").inner_text()).group(1))
    check(1 <= s2 - s1 <= 3, f"已經連不上的秒數每秒更新（2.2 秒內 {s1}→{s2}）")
    hgpg.unroute_all(behavior="ignoreErrors")
    ctxHang.close()

    # 已經顯示過 QR 之後只讓一次取碼失敗，遮罩不出現（維持 3 次門檻）
    ctxThresh = chrome.new_context(viewport={"width": 1280, "height": 900})
    thpg = ctxThresh.new_page()
    thpg.goto(f"{B}/t/login"); thpg.fill("input[name=password]", "pw"); thpg.click("button:has-text('登入')"); thpg.wait_for_url(f"{B}/t")
    thpg.goto(f"{B}/t/projector/{sid}")
    thpg.wait_for_function("document.querySelector('#qr svg')!==null")
    fail_once = {"n": 0}

    def _abort_once(route):
        fail_once["n"] += 1
        route.abort() if fail_once["n"] == 1 else route.continue_()
    thpg.route(f"**/api/t/session/{sid}/qr*", _abort_once)
    thpg.wait_for_timeout(1500)
    check(thpg.locator("#netmsg").is_hidden(), "已經顯示過 QR 之後單次失敗不出現遮罩（仍要 3 次門檻）")
    thpg.unroute(f"**/api/t/session/{sid}/qr*")
    ctxThresh.close()

    # 投影頁截斷在主腳本之前，看門狗要蓋一個提示；正常載入時不出現
    raw_html = teacher.request.get(f"{B}/t/projector/{sid}").text()
    cut_at = raw_html.index("const sid=")  # 在主腳本第一行之前截斷，看門狗腳本（在 head）已經送到
    truncated = raw_html[:cut_at]
    ctxTrunc = chrome.new_context(viewport={"width": 1280, "height": 900})
    trpage = ctxTrunc.new_page()
    trpage.goto(f"{B}/t/login"); trpage.fill("input[name=password]", "pw"); trpage.click("button:has-text('登入')"); trpage.wait_for_url(f"{B}/t")
    trpage.route(f"{B}/t/projector/{sid}", lambda route: route.fulfill(status=200, content_type="text/html; charset=utf-8", body=truncated))
    trpage.goto(f"{B}/t/projector/{sid}")
    trpage.wait_for_function("document.getElementById('__projWatchdog')!==null", timeout=7000)
    check("沒有完整載入" in trpage.locator("#__projWatchdog").inner_text(), "截斷時看門狗顯示「沒有完整載入」")
    trpage.unroute(f"{B}/t/projector/{sid}")
    ctxTrunc.close()

    ctxNormal = chrome.new_context(viewport={"width": 1280, "height": 900})
    nmlpage = ctxNormal.new_page()
    nmlpage.goto(f"{B}/t/login"); nmlpage.fill("input[name=password]", "pw"); nmlpage.click("button:has-text('登入')"); nmlpage.wait_for_url(f"{B}/t")
    nmlpage.goto(f"{B}/t/projector/{sid}")
    nmlpage.wait_for_timeout(6500)  # 看門狗的 setTimeout 是 6 秒，過了這個時間點才有意義判斷「有沒有出現」
    check(nmlpage.locator("#__projWatchdog").count() == 0, "正常載入時看門狗逾時檢查後仍不出現")
    ctxNormal.close()

    # staleAt 改算「換碼時間點＋grace」，QR 仍有效時網路訊息只是邊條，過了才整片覆蓋
    teacher.request.post(f"{B}/t/course/{cid_home}/settings", form={
        "open_minutes": "3", "late_minutes": "15", "qr_interval": "5", "qr_grace": "8",
        "spot_n": "3", "bind_minutes": "10"})
    ctxGrace = chrome.new_context(viewport={"width": 1280, "height": 900})
    gpg = ctxGrace.new_page()
    gpg.goto(f"{B}/t/login"); gpg.fill("input[name=password]", "pw"); gpg.click("button:has-text('登入')"); gpg.wait_for_url(f"{B}/t")
    gpg.goto(f"{B}/t/projector/{sid}")
    gpg.wait_for_function("document.querySelector('#qr svg')!==null")
    gpg.route(f"**/api/t/session/{sid}/qr*", lambda route: route.abort())
    gpg.wait_for_function("getComputedStyle(document.getElementById('netmsg')).display==='flex'", timeout=8000)
    check(gpg.evaluate("getComputedStyle(document.getElementById('qr')).visibility") != "hidden",
          "換碼時間點+grace 之前 QR 仍可見（visibility 不是 hidden）")
    check("edge" in (gpg.locator("#netmsg").get_attribute("class") or ""), "QR 仍有效時網路訊息是不擋 QR 的邊條")
    gpg.wait_for_function(
        "(()=>{const el=document.getElementById('netmsg');return el.style.display==='flex' && !el.classList.contains('edge');})()",
        timeout=30000)
    check(True, "過了 staleAt 之後網路訊息從邊條升級成整片覆蓋")
    ctxGrace.close()

    # 計時器停擺（非網路問題）時 #stalemsg 仍會出現，不受上面的網路判定影響
    ctxFreeze = chrome.new_context(viewport={"width": 1280, "height": 900})
    ctxFreeze.add_init_script("""
      const _orig = performance.now.bind(performance);
      let _off = 0;
      performance.now = () => _orig() + _off;
      window.__bumpClock = (ms) => { _off += ms; };
    """)
    fzpg = ctxFreeze.new_page()
    fzpg.goto(f"{B}/t/login"); fzpg.fill("input[name=password]", "pw"); fzpg.click("button:has-text('登入')"); fzpg.wait_for_url(f"{B}/t")
    fzpg.goto(f"{B}/t/projector/{sid}")
    fzpg.wait_for_function("document.querySelector('#qr svg')!==null")
    fzpg.evaluate("window.inFlight = true")  # 讓之後的 tick() 全部 no-op，模擬「計時器沒有真的執行」
    fzpg.evaluate("window.__bumpClock(120000)")
    fzpg.wait_for_function("document.body.classList.contains('stale')", timeout=5000)
    check("畫面沒有更新" in fzpg.locator("#stalemsg").inner_text(), "計時器停擺時 #stalemsg 仍會出現")
    check(fzpg.evaluate("document.getElementById('netmsg').style.display") != "flex",
          "計時器停擺不是網路問題，不該同時顯示網路遮罩")
    ctxFreeze.close()

    # 計時器恢復＋取碼真的失敗（不只是凍結）時，收掉「畫面沒有更新」改講網路訊息
    ctxFreeze2 = chrome.new_context(viewport={"width": 1280, "height": 900})
    ctxFreeze2.add_init_script("""
      const _orig = performance.now.bind(performance);
      let _off = 0;
      performance.now = () => _orig() + _off;
      window.__bumpClock = (ms) => { _off += ms; };
    """)
    fz2pg = ctxFreeze2.new_page()
    fz2pg.goto(f"{B}/t/login"); fz2pg.fill("input[name=password]", "pw"); fz2pg.click("button:has-text('登入')"); fz2pg.wait_for_url(f"{B}/t")
    fz2pg.goto(f"{B}/t/projector/{sid}")
    fz2pg.wait_for_function("document.querySelector('#qr svg')!==null")
    fz2pg.evaluate("window.inFlight = true")
    fz2pg.evaluate("window.__bumpClock(40000)")
    fz2pg.wait_for_function("document.body.classList.contains('stale')", timeout=5000)
    fz2pg.route(f"**/api/t/session/{sid}/qr*", lambda route: route.abort("connectionreset"))
    fz2pg.evaluate("window.inFlight = false")  # 恢復輪詢，同時 qr 真的連不上
    fz2pg.wait_for_timeout(5000)
    check(not fz2pg.evaluate("document.body.classList.contains('stale')"),
          "恢復輪詢＋取碼真的失敗後，畫面正中最上層不再是「畫面沒有更新」")
    check(fz2pg.evaluate("getComputedStyle(document.getElementById('netmsg')).display") == "flex",
          "改講網路訊息")
    ctxFreeze2.close()

    # visibilitychange 回到前景要立刻補一次輪詢，不用等下一輪排程
    ctxV = chrome.new_context(viewport={"width": 1280, "height": 900})
    ctxV.add_init_script("""
      Object.defineProperty(document,'hidden',{get:()=>window.__forceHidden===true,configurable:true});
    """)
    vp = ctxV.new_page()
    vp.goto(f"{B}/t/login"); vp.fill("input[name=password]", "pw"); vp.click("button:has-text('登入')"); vp.wait_for_url(f"{B}/t")
    vp.goto(f"{B}/t/projector/{sid}")
    vp.wait_for_function("document.querySelector('#qr svg')!==null")
    reqs = []
    vp.on("request", lambda r: reqs.append(r.url) if f"/api/t/session/{sid}/qr" in r.url else None)
    vp.evaluate("window.__forceHidden=true")
    reqs.clear()
    vp.evaluate("window.__forceHidden=false; document.dispatchEvent(new Event('visibilitychange'))")
    vp.wait_for_timeout(300)
    check(len(reqs) >= 1, "切回前景立刻補打一次 /qr，不等排程")
    ctxV.close()

    # 投影頁沒有 AbortSignal.timeout 一樣要能持續輪詢，不是整支卡死
    ctxN = chrome.new_context(viewport={"width": 1280, "height": 900})
    ctxN.add_init_script("delete AbortSignal.timeout;")
    npg = ctxN.new_page()
    npg.goto(f"{B}/t/login"); npg.fill("input[name=password]", "pw"); npg.click("button:has-text('登入')"); npg.wait_for_url(f"{B}/t")
    npg.goto(f"{B}/t/projector/{sid}")
    npg.wait_for_function("document.querySelector('#qr svg')!==null")
    c0 = npg.locator("#code").inner_text()
    npg.wait_for_function(f"document.getElementById('code').innerText!=={c0!r}", timeout=15000)
    check(True, "拿掉 AbortSignal.timeout，投影頁一樣能持續換碼")
    ctxN.close()

    # 老師 token 失效時投影幕給全螢幕文字提示，不是跳成登入表單
    ctxE = chrome.new_context(viewport={"width": 1280, "height": 900})
    epg = ctxE.new_page()
    epg.goto(f"{B}/t/login"); epg.fill("input[name=password]", "pw"); epg.click("button:has-text('登入')"); epg.wait_for_url(f"{B}/t")
    epg.goto(f"{B}/t/projector/{sid}")
    epg.wait_for_function("document.querySelector('#qr svg')!==null")
    ctxE.clear_cookies()
    epg.wait_for_function("document.body.classList.contains('authexpired')", timeout=15000)
    check("登入已失效" in epg.locator("#authmsg").inner_text(), "401 顯示全螢幕文字提示")
    check(epg.locator("input[type=password]").count() == 0, "畫面上沒有出現密碼輸入框（沒有跳成登入表單）")
    ctxE.close()

    # 本場作廢（標籤與確認文案）＋自動缺席整批更正（老師頁新按鈕）
    sess.bring_to_front()
    dialog_msgs = []
    sess.on("dialog", lambda d: (dialog_msgs.append(d.message), d.accept()))
    sess.click("#closeBtn")
    # closeBtn.disabled 在 guard() 一開始（POST 送出之前）就同步設 true，不能拿來判斷「已經結案渲染完」；
    # 要等 #phase 文字真的變成「已結束」（load(true) 拿到新 state 後 render() 才會把 voidBtn/bulkRow 更新）。
    sess.wait_for_function("document.getElementById('phase').innerText==='已結束'", timeout=15000)
    check(sess.locator("#voidBtn").is_visible(), "結案後出現「本場作廢」按鈕")
    check("本場作廢" in sess.locator("#voidBtn").inner_text(), "作廢按鈕預設文字")
    bulk_visible_before = sess.locator("#bulkRow").is_visible()
    check(bulk_visible_before, "有自動缺席筆數時顯示「自動缺席 N 筆」整批更正列")
    if bulk_visible_before:
        check("自動缺席" in sess.locator("#bulkLabel").inner_text(), "整批更正列文字含「自動缺席」")
        sess.select_option("#bulkTo", "excused")
        sess.click("#bulkRow button")
        sess.wait_for_timeout(400)
        check(sess.locator("#bulkRow").is_hidden() or "自動缺席 0" in sess.locator("#bulkLabel").inner_text(),
              "整批更正後自動缺席筆數歸零，整批更正列跟著收起或歸零")
    dialog_msgs.clear()
    sess.click("#voidBtn")
    sess.wait_for_function("document.getElementById('voidBtn').innerText.includes('已作廢')")
    check(True, "按一下「本場作廢」後按鈕文字切成「已作廢（按一下取消作廢）」")
    check(any("不會被撤回" in m for m in dialog_msgs), f"作廢確認對話框有寫明 Discord 不撤回，實得 {dialog_msgs}")

    # 綁定場次老師頁：隱藏狀態下拉／手動簽到、counts 只顯示「已綁定 N 人」、首次綁定鈕鎖定文字
    r_cid = teacher.request.get(f"{B}/t")
    m_cid = re.search(r"course/(\d+)/open", r_cid.text())
    cid = int(m_cid.group(1))
    r_bind = teacher.request.post(f"{B}/t/course/{cid}/open", form={"kind": "bind"})
    bind_sid = int(re.search(r"/t/session/(\d+)", r_bind.url).group(1))
    bpage = teacher.new_page(); bpage.goto(f"{B}/t/session/{bind_sid}")
    bpage.wait_for_function("state && state.kind==='bind'")
    check("已綁定" in bpage.locator("#counts").inner_text(), "綁定場次： counts 只顯示「已綁定 N 人」")
    check(bpage.locator("#manualSection").is_hidden(), "綁定場次：隱藏「手動簽到」區塊")
    check(bpage.locator("#rows select.mini").count() == 0, "綁定場次：名單每列沒有改狀態的下拉")
    check("另開一般點名" in bpage.locator("#rows").inner_text(), "綁定場次：名單改成說明文字")
    check(bpage.locator("#bindBtn").is_disabled(), "綁定場次：的首次綁定鈕鎖定不可按")
    check("本場一律開放" in bpage.locator("#bindBtn").inner_text(), "綁定場次：的首次綁定鈕文字改成「本場一律開放」")
    check(bpage.locator("#spotBtn").is_visible(), "綁定場次：抽點按鈕在綁定場次仍要看得到")

    # 同課已有一場未結案綁定場次時，按「開始點名」（種類不同）要看到錯誤說明，不是沿用也不是新開
    r_mismatch = teacher.request.post(f"{B}/t/course/{cid}/open", form={"kind": "normal"})
    check(r_mismatch.status == 409, f"綁定場次：種類不同時開場應該 409，實得 {r_mismatch.status}")
    # 這句訊息依方向給不同建議動作——綁定場次還在進行、想開一般點名時，
    # 叫老師去那一場按「結束點名」；不用含糊的「先結束它」，也不用「綁定場次」這個內部旗標名，
    # 用畫面上那顆按鈕真正寫的「只做綁定」。
    check("只做綁定" in r_mismatch.text() and "結束點名" in r_mismatch.text(), f"綁定場次：錯誤頁說明原因，實得 {r_mismatch.text()[:200]!r}")

    # 綁定場次與已作廢場次隱藏「發到 Discord」與暫緩提示
    teacher.request.post(f"{B}/t/course/{cid}/settings", form={
        "open_minutes": "3", "late_minutes": "15", "qr_interval": "10", "qr_grace": "30",
        "spot_n": "3", "bind_minutes": "10", "discord_channel": "123456789012345"})
    teacher.request.post(f"{B}/api/t/session/{bind_sid}/close")
    bpage.reload(); bpage.wait_for_function("state && state.phase==='closed'")
    check(bpage.locator("#notifyBtn").is_hidden(), "綁定場次：結案後仍不顯示「發到 Discord」（即使課程已設頻道）")

    # 投影頁換場、名單忙碌判斷與遲到區確認

    # 名單下拉開了再按 Esc，焦點留在 #rows，輪詢不該無限期暫停
    teacher.request.post(f"{B}/t/course/{cid_home}/settings", form={
        "open_minutes": "1", "late_minutes": "2", "qr_interval": "5", "qr_grace": "8",
        "spot_n": "3", "bind_minutes": "10", "discord_channel": "123456789012345"})
    r_busy_open = teacher.request.post(f"{B}/t/course/{cid_home}/open", form={"kind": "normal"})
    busy_sid = int(re.search(r"/t/session/(\d+)", r_busy_open.url).group(1))
    b090pg = teacher.new_page(); b090pg.goto(f"{B}/t/session/{busy_sid}")
    b090pg.wait_for_selector("#rows select")
    b090pg.click("#rows select >> nth=0")
    b090pg.keyboard.press("Escape")  # 下拉關掉，但焦點還留在 #rows 裡（select 本身）
    b090pg.wait_for_timeout(11000)  # busy() 現在只認「最後一次在 #rows 裡的操作」10 秒內才算忙碌，過了就該恢復輪詢
    b090pg.wait_for_function("document.getElementById('phase').innerText==='遲到區'", timeout=90000)
    check(b090pg.locator("#pause").is_hidden(), "超過 10 秒沒再摸名單後輪詢恢復，畫面能更新到遲到區、#pause 不再顯示")
    teacher.request.post(f"{B}/api/t/session/{busy_sid}/close")
    teacher.request.post(f"{B}/t/course/{cid_home}/settings", form={
        "open_minutes": "3", "late_minutes": "15", "qr_interval": "5", "qr_grace": "8",
        "spot_n": "3", "bind_minutes": "10", "discord_channel": "123456789012345"})

    # 投影頁換場後自動導去新場次，不用老師手動改網址
    r_first_open = teacher.request.post(f"{B}/t/course/{cid_home}/open", form={"kind": "normal"})
    old_sid = int(re.search(r"/t/session/(\d+)", r_first_open.url).group(1))
    b091pg = teacher.new_page(); b091pg.goto(f"{B}/t/projector/{old_sid}")
    b091pg.wait_for_function("document.querySelector('#qr svg')!==null")
    teacher.request.post(f"{B}/api/t/session/{old_sid}/close")
    r_new = teacher.request.post(f"{B}/t/course/{cid_home}/open", form={"kind": "normal"})
    second_open_sid = int(re.search(r"/t/session/(\d+)", r_new.url).group(1))
    b091pg.wait_for_function("document.getElementById('msg').innerText.includes('已經開了新的一場')", timeout=15000)
    b091pg.wait_for_url(f"{B}/t/projector/{second_open_sid}", timeout=8000)
    check(True, "舊投影頁在老師關掉這場並開新場次後，幾秒內自動換到新場次網址")
    b091pg.close()  # 驗證到此為止；這頁留著不關會一路追著 cid_home 之後每一場新開的投影頁網址，
    # 汙染後面用同一堂課測「投影頁沒連上」的情境——殘留的這頁會讓後面那段失敗。
    teacher.request.post(f"{B}/api/t/session/{second_open_sid}/close")

    # 遲到區簽到、還待確認的人，名單標「遲到區簽到」；下拉選「出席」要先確認一次
    teacher.request.post(f"{B}/t/course/{cid_home}/settings", form={
        "open_minutes": "1", "late_minutes": "15", "qr_interval": "5", "qr_grace": "8",
        "spot_n": "3", "bind_minutes": "10", "discord_channel": "123456789012345"})
    r_late_open = teacher.request.post(f"{B}/t/course/{cid_home}/open", form={"kind": "normal"})
    late_sid = int(re.search(r"/t/session/(\d+)", r_late_open.url).group(1))
    shared_phone = p.request.new_context(base_url=B)  # 同一支「手機」：兩個學生共用它 → 既有代簽偵測讓兩人都變待確認
    check(wait_for_phase(teacher.request, late_sid, "late", timeout_s=75), "等出席窗關、進入遲到區才簽到（開放窗設 1 分鐘）")
    g1 = student_grant(teacher.request, shared_phone, late_sid)
    r1 = shared_phone.post(f"{B}/api/checkin", form={"grant": g1, "student_id": "9994A045", "name": "測試45",
                                                    "pin": "2468", "pin2": "2468", "hints": "h"})
    g2 = student_grant(teacher.request, shared_phone, late_sid)
    r2 = shared_phone.post(f"{B}/api/checkin", form={"grant": g2, "student_id": "9994A046", "name": "測試46", "pin": "1357", "pin2": "1357", "hints": "h"})
    check(True, f"兩個待確認的簽到已送出（{r1.status} /{r2.status}）")
    b095pg = teacher.new_page(); b095pg.goto(f"{B}/t/session/{late_sid}")
    b095pg.wait_for_selector("#rows tr")
    row = b095pg.locator("#rows tr", has_text="9994A045")
    check("遲到區簽到" in row.inner_text(), "待確認且是遲到區簽到的人，名單上顯示「遲到區簽到」標記")
    confirm_msgs = []
    b095pg.once("dialog", lambda d: (confirm_msgs.append(d.message), d.dismiss()))  # 按「取消」
    posts_095 = []
    b095pg.on("request", lambda r: posts_095.append(r.post_data) if "/status" in r.url and r.method == "POST" else None)
    row.locator("select").select_option("present")
    b095pg.wait_for_timeout(500)
    check(any("要記「出席」嗎" in m for m in confirm_msgs), f"選「出席」前先跳確認對話框，實得 {confirm_msgs}")
    check(any(pd and 'name="status"' in pd and "late" in pd for pd in posts_095), "對話框按「取消」後改記「遲到」而不是「出席」")
    teacher.request.post(f"{B}/api/t/session/{late_sid}/close")
    shared_phone.dispose()
    teacher.request.post(f"{B}/t/course/{cid_home}/settings", form={
        "open_minutes": "3", "late_minutes": "15", "qr_interval": "5", "qr_grace": "8",
        "spot_n": "3", "bind_minutes": "10", "discord_channel": "123456789012345"})

    # 場次結束後，抽點的「在／不在」按鈕要鎖起來，不能再誤按
    real_state = json.loads(sess.evaluate("JSON.stringify(state)"))
    real_state["phase"] = "closed"
    real_state["spots"] = [{"id": 999999, "student_id": "9999999", "name": "測試學生", "result": None}]
    sess.route(f"**/api/t/session/{sid}/state", lambda route: route.fulfill(
        status=200, content_type="application/json", body=json.dumps(real_state)))
    sess.wait_for_timeout(3500)  # 等下一輪輪詢吃到這份假資料
    check(sess.locator("#spotClosedHint").is_visible(), "結束後抽點區顯示「點名已結束，要改請在名單上改狀態」")
    check(sess.locator("#spot button:has-text('在')").first.is_disabled(), "結束後「在」鈕鎖住")
    check(sess.locator("#spot button:has-text('不在')").first.is_disabled(), "結束後「不在」鈕鎖住")
    sess.unroute(f"**/api/t/session/{sid}/state")

    # 延長／抽點是非冪等操作，回應遺失時訊息改中性、並重新讀取狀態（不是「再按一次」）
    r_ext = teacher.request.post(f"{B}/t/course/{cid_home}/open", form={"kind": "normal"})
    ext_sid = int(re.search(r"/t/session/(\d+)", r_ext.url).group(1))
    extpg = teacher.new_page(); extpg.goto(f"{B}/t/session/{ext_sid}")
    extpg.wait_for_function("state && state.phase")
    before_timer = extpg.locator("#timer").inner_text()

    def _drop_after_processed(route):
        # 規格明寫「回應在送到伺服器後 abort」：先讓請求真的處理完成（真的延長了），
        # 只是瀏覽器收不到那個回應，跟請求根本沒送到是兩回事。
        route.fetch()
        route.abort()

    ext_msgs = []
    _on_ext_dialog = lambda d: (ext_msgs.append(d.message), d.accept())
    extpg.on("dialog", _on_ext_dialog)
    extpg.route(f"**/api/t/session/{ext_sid}/extend", _drop_after_processed)
    extpg.click("#extendBtn")
    extpg.wait_for_function("!document.getElementById('extendBtn').disabled", timeout=15000)
    extpg.unroute(f"**/api/t/session/{ext_sid}/extend")
    check(any("不確定有沒有送到" in m for m in ext_msgs), f"延長失敗訊息含「不確定有沒有送到」，實得 {ext_msgs}")
    check(not any("再按一次" in m for m in ext_msgs), f"延長失敗訊息不再講「再按一次」，實得 {ext_msgs}")
    after_timer = extpg.locator("#timer").inner_text()
    check(before_timer != after_timer, f"畫面倒數已經反映延長後的時間（load(true) 有跑；{before_timer!r}→{after_timer!r}）")

    # 抽點失敗後一樣重新讀取並捲到抽點名單，跟成功時一樣
    extpg.evaluate("""() => {
        window.__spotScrolled = false;
        const el = document.getElementById('spot');
        const orig = el.scrollIntoView.bind(el);
        el.scrollIntoView = (...args) => { window.__spotScrolled = true; return orig(...args); };
    }""")

    def _drop_spot(route):
        route.fetch()
        route.abort()

    ext_msgs.clear()
    extpg.route(f"**/api/t/session/{ext_sid}/spotcheck", _drop_spot)
    extpg.click("#spotBtn")
    extpg.wait_for_function("!document.getElementById('spotBtn').disabled", timeout=15000)
    extpg.unroute(f"**/api/t/session/{ext_sid}/spotcheck")
    check(any("不確定有沒有送到" in m for m in ext_msgs), f"抽點失敗也是同一句中性訊息，實得 {ext_msgs}")
    extpg.wait_for_function("window.__spotScrolled===true", timeout=3000)
    check(True, "抽點失敗後仍捲到抽點名單（跟成功時一樣）")

    # 投影頁顯示「登入已失效」遮罩後，下一次取碼回 200 就要把遮罩拿掉
    projpg2 = teacher.new_page(); projpg2.goto(f"{B}/t/projector/{ext_sid}")
    projpg2.wait_for_function("document.querySelector('#qr svg')!==null")
    projpg2.route(f"**/api/t/session/{ext_sid}/qr*", lambda route: route.fulfill(
        status=401, content_type="application/json", body='{"ok":false,"error":"請先登入"}'))
    projpg2.wait_for_function("document.body.classList.contains('authexpired')", timeout=8000)
    check(True, "qr 回 401 後投影頁顯示「登入已失效」遮罩")
    projpg2.unroute(f"**/api/t/session/{ext_sid}/qr*")
    projpg2.wait_for_function("!document.body.classList.contains('authexpired')", timeout=8000)
    check(True, "解除攔截、下一次取碼回 200 後遮罩自動消失")
    projpg2.close()

    # 先斷線 20 秒（網路訊息出現）再回 401——網路訊息要收掉、登入遮罩蓋在最上層
    route_mode = {"m": "abort"}
    def _handler(route):
        if route_mode["m"] == "abort":
            route.abort("connectionreset")
        else:
            route.fulfill(status=401, content_type="application/json", body='{"ok":false,"error":"請先登入"}')
    b123pg = teacher.new_page()
    b123pg.route(f"**/api/t/session/{ext_sid}/qr*", _handler)
    b123pg.goto(f"{B}/t/projector/{ext_sid}")
    b123pg.wait_for_function("getComputedStyle(document.getElementById('netmsg')).display==='flex'", timeout=10000)
    check(True, "斷線時網路訊息先出現（前提成立）")
    route_mode["m"] = "401"
    b123pg.wait_for_function("getComputedStyle(document.getElementById('netmsg')).display!=='flex'", timeout=3000)
    check(True, "改回 401 後 3 秒內網路訊息隱藏")
    check(b123pg.evaluate("document.body.classList.contains('authexpired')"), "401 後登入遮罩顯示")
    net_z = int(b123pg.evaluate("getComputedStyle(document.getElementById('netmsg')).zIndex"))
    auth_z = int(b123pg.evaluate("getComputedStyle(document.getElementById('authmsg')).zIndex"))
    check(auth_z > net_z, f"登入遮罩 z-index（{auth_z}）要蓋過網路訊息（{net_z}）")
    b123pg.unroute_all(behavior="ignoreErrors")
    b123pg.close()
    projpg2.close()

    # session.html 刪除場次：只有伺服器回成功才跳回 /t，失敗留在原頁並顯示原因
    # 先移除上面抽點那段的 dialog 監聽器（兩個 .on("dialog", ...) 疊在同一個
    # extpg 上沒清掉，同一顆 dialog 會被兩個 handler 搶著 accept，第二個必噴
    # 「Cannot accept dialog which is already handled」；不移除會讓整支腳本在這裡意外中止）。
    extpg.remove_listener("dialog", _on_ext_dialog)
    del_msgs = []
    extpg.on("dialog", lambda d: (del_msgs.append(d.message), d.accept()))
    extpg.route(f"**/api/t/session/{ext_sid}/delete", lambda route: route.fulfill(
        status=500, content_type="application/json", body='{"ok":false,"error":"測試用：伺服器拒絕刪除"}'))
    extpg.click("button.ghost.mini:has-text('刪除此次')")
    extpg.wait_for_timeout(500)
    check(extpg.url.endswith(f"/t/session/{ext_sid}"), f"刪除回 500 時仍留在原頁，實得 {extpg.url}")
    check(any("測試用：伺服器拒絕刪除" in m for m in del_msgs), f"刪除失敗顯示伺服器給的原因，實得 {del_msgs}")
    extpg.unroute(f"**/api/t/session/{ext_sid}/delete")

    # 發 Discord 的前端：45 秒逾時、逾時／網路失敗訊息中性、again 顯示「兩分鐘內已經發過」
    notpg = teacher.new_page(); notpg.goto(f"{B}/t/session/{ext_sid}")
    notpg.wait_for_function("state && state.phase")
    notpg.once("dialog", lambda d: d.accept())  # closeBtn 的 confirm
    notpg.click("#closeBtn")
    notpg.wait_for_function("document.getElementById('phase').innerText==='已結束'", timeout=15000)
    notpg.wait_for_function(
        "document.getElementById('notifyBtn') && getComputedStyle(document.getElementById('notifyBtn')).display!=='none'",
        timeout=10000)

    notify_msgs = []
    notpg.on("dialog", lambda d: (notify_msgs.append(d.message), d.accept()))

    def _drop_notify(route):
        route.fetch()
        route.abort()

    notpg.route(f"**/api/t/session/{ext_sid}/notify", _drop_notify)
    notpg.click("#notifyBtn")
    notpg.wait_for_function("!document.getElementById('notifyBtn').disabled", timeout=50000)
    notpg.unroute(f"**/api/t/session/{ext_sid}/notify")
    check(any("不確定有沒有發出" in m for m in notify_msgs), f"notify 逾時／網路失敗訊息中性，實得 {notify_msgs}")
    check(not any("再按一次" in m for m in notify_msgs), f"notify 失敗訊息不再講「再按一次」，實得 {notify_msgs}")

    notify_msgs.clear()
    notpg.route(f"**/api/t/session/{ext_sid}/notify", lambda route: route.fulfill(
        status=200, content_type="application/json", body='{"ok":true,"again":true}'))
    notpg.click("#notifyBtn")
    notpg.wait_for_function("!document.getElementById('notifyBtn').disabled", timeout=10000)
    notpg.unroute(f"**/api/t/session/{ext_sid}/notify")
    check(any("兩分鐘內已經發過" in m for m in notify_msgs), f"again 顯示「兩分鐘內已經發過」，實得 {notify_msgs}")

    # 點名頁「這場先不要自動發 Discord」：進行中的暫緩開關，跟出席率暫緩分開
    teacher.request.post(f"{B}/t/course/{cid_home}/settings", form={
        "open_minutes": "3", "late_minutes": "15", "qr_interval": "5", "qr_grace": "30",
        "spot_n": "3", "bind_minutes": "10", "discord_channel": "123456789012345"})
    r_hold = teacher.request.post(f"{B}/t/course/{cid_home}/open", form={"kind": "normal"})
    hold_sid = int(re.search(r"/t/session/(\d+)", r_hold.url).group(1))
    holdpg = teacher.new_page(); holdpg.goto(f"{B}/t/session/{hold_sid}")
    holdpg.wait_for_function("state && state.discord===true")
    check(holdpg.locator("#holdBtn").is_visible(), "設了頻道的課開場後，暫緩按鈕可見")
    check(holdpg.locator("#holdBtn").inner_text() == "這場先不要自動發 Discord",
          f"初始按鈕文字，實得 {holdpg.locator('#holdBtn').inner_text()!r}")
    holdpg.click("#holdBtn")
    holdpg.wait_for_function("state && state.discord_held_manual===true")
    check(holdpg.locator("#holdBtn").inner_text() == "取消暫緩（結束時照常發 Discord）",
          f"按下後文字變成「取消暫緩…」，實得 {holdpg.locator('#holdBtn').inner_text()!r}")
    check(holdpg.locator("#holdHint").is_visible(), "暫緩中灰字出現")
    holdpg.reload()
    holdpg.wait_for_function("state && state.discord_held_manual===true")
    check(holdpg.locator("#holdBtn").inner_text() == "取消暫緩（結束時照常發 Discord）", "重新整理後暫緩狀態保留")
    holdpg.once("dialog", lambda d: d.accept())
    holdpg.click("#closeBtn")
    holdpg.wait_for_function("document.getElementById('phase').innerText==='已結束'", timeout=15000)
    check(not holdpg.locator("#holdBtn").is_visible(), "結束後暫緩按鈕隱藏")
    check(holdpg.locator("#heldMsg").is_visible(), "結束後 #heldMsg 可見")
    check("先不要自動發" in holdpg.locator("#heldMsg").inner_text(),
          f"#heldMsg 是手動暫緩那句，實得 {holdpg.locator('#heldMsg').inner_text()!r}")

    # 背景發 Discord 失敗要留下痕跡
    # 這個 e2e 環境沒有設 DISCORD_TOKEN，出席率過半、不被暫緩時，背景發送一定會失敗——用這點驗證失敗有沒有被看見，不用另外 mock
    r_discord_open = teacher.request.post(f"{B}/t/course/{cid_home}/open", form={"kind": "normal"})
    discord_sid = int(re.search(r"/t/session/(\d+)", r_discord_open.url).group(1))
    # NTUB-CD 名冊 47 人（9994A001~048，缺 018）；24 人簽到已過半（24/47），should_hold_discord
    # 才不會擋住背景發送，才吃得到「背景送出但失敗」這個情境。每支手機一分鐘只能簽到 12 次
    # （_throttle 限流），24 人不能共用同一個 request context，每人各開一個當一支手機。
    # 學號 5 號以後名冊上的姓名都是「測試N」（gate.sh 合成名冊），跟這裡送出的姓名對得起來；
    # 1~4 號的姓名不是「測試N」格式，姓名對不上會被判 not_match，故從 5 號開始取，避開那四人。
    phones = []
    for n in range(5, 30):
        if n == 18:
            continue
        stu = f"9994A{n:03d}"
        phone = p.request.new_context(base_url=B)
        phones.append(phone)
        g = student_grant(teacher.request, phone, discord_sid)
        phone.post(f"{B}/api/checkin", form={"grant": g, "student_id": stu, "name": f"測試{n}", "pin": "1234", "pin2": "1234", "hints": "h"})
    for phone in phones:
        phone.dispose()
    b105pg = teacher.new_page(); b105pg.goto(f"{B}/t/session/{discord_sid}")
    b105pg.once("dialog", lambda d: d.accept())
    b105pg.click("#closeBtn")
    b105pg.wait_for_function("document.getElementById('phase').innerText==='已結束'", timeout=15000)
    b105pg.wait_for_function("state && state.discord_failed===true", timeout=10000)
    check(b105pg.locator("#discordFailedMsg").is_visible(), "結束後背景發送失敗，紅字要出現")
    check("沒有成功" in b105pg.locator("#discordFailedMsg").inner_text(),
          f"紅字文案，實得 {b105pg.locator('#discordFailedMsg').inner_text()!r}")

    # 主腳本一開始跑就移除已經插入的看門狗；補完主腳本時 qr 掛住
    r_watchdog_open = teacher.request.post(f"{B}/t/course/{cid_home}/open", form={"kind": "normal"})
    watchdog_sid = int(re.search(r"/t/session/(\d+)", r_watchdog_open.url).group(1))
    raw_html_119 = teacher.request.get(f"{B}/t/projector/{watchdog_sid}").text()
    cut_at_119 = raw_html_119.index("const sid=")
    end_at_119 = raw_html_119.index("</script>", cut_at_119)
    truncated_119 = raw_html_119[:cut_at_119]
    script_body_119 = raw_html_119[cut_at_119:end_at_119]
    b119pg = teacher.new_page()
    b119pg.route(f"{B}/t/projector/{watchdog_sid}", lambda route: route.fulfill(status=200, content_type="text/html; charset=utf-8", body=truncated_119))
    b119pg.route(f"**/api/t/session/{watchdog_sid}/qr*", lambda route: None)  # 掛住不回應，補完主腳本後取碼卡住
    b119pg.goto(f"{B}/t/projector/{watchdog_sid}")
    b119pg.wait_for_function("document.getElementById('__projWatchdog')!==null", timeout=7000)
    check(True, "截斷重現＋看門狗先出現（前提成立）")
    b119pg.add_script_tag(content=script_body_119)
    b119pg.wait_for_timeout(1000)
    check(b119pg.evaluate("document.getElementById('__projWatchdog')===null"),
          "主腳本補上後 1 秒內看門狗被移除，不用等取碼成功")
    b119pg.wait_for_function("getComputedStyle(document.getElementById('netmsg')).display==='flex'", timeout=8000)
    check(True, "網路訊息照常出現（取碼卡住走的是網路訊息那條路，不是看門狗）")
    b119pg.unroute_all(behavior="ignoreErrors")
    b119pg.close()
    teacher.request.post(f"{B}/api/t/session/{watchdog_sid}/close")

    # 「投影頁連線正常」只在投影頁真的收到回應時才顯示；#projMsg 狀態表
    teacher.request.post(f"{B}/t/course/{cid_home}/settings", form={
        "open_minutes": "3", "late_minutes": "15", "qr_interval": "5", "qr_grace": "30",
        "spot_n": "3", "bind_minutes": "10", "discord_channel": ""})
    r_stale_open = teacher.request.post(f"{B}/t/course/{cid_home}/open", form={"kind": "normal"})
    stale_sid = int(re.search(r"/t/session/(\d+)", r_stale_open.url).group(1))
    b121pg = teacher.new_page(); b121pg.goto(f"{B}/t/session/{stale_sid}")
    b121pg.wait_for_function("state && state.phase")
    # 剛開場、還沒開投影頁：projector_age 是 None、opened_age 還很小 → 灰字
    check(b121pg.locator("#projMsg").get_attribute("class") == "sm muted",
          f"剛開場沒開投影頁是灰字，實得 class={b121pg.locator('#projMsg').get_attribute('class')!r}")
    check("等投影頁出現 QR" in b121pg.locator("#projMsg").inner_text(),
          f"剛開場文案，實得 {b121pg.locator('#projMsg').inner_text()!r}")

    # 情境 A：投影頁 qr 回應在回程遺失（請求已到伺服器、回應沒回到瀏覽器）20 秒 →
    # 點名頁不該再說「投影頁連線正常」。瀏覽器端的請求一律直接 abort（不在頁面層做
    # route.fetch()+abort：整支測試檔開了幾十個分頁，系統負載大時那個組合會跟瀏覽器自己的
    # 4 秒逾時搶時間出現競態、偶爾讓真回應意外落地，是測試本身不穩，不是被測程式碼的問題）；
    # 「請求真的到了伺服器」改用 teacher.request（跟頁面分開的 API context）直接送，
    # have 帶投影頁看得到的空字串，驗證伺服器處理過請求也不會誤更新 PROJECTOR_SEEN。
    b121proj = teacher.new_page()
    b121proj.route(f"**/api/t/session/{stale_sid}/qr*", lambda route: route.abort("connectionreset"))
    b121proj.goto(f"{B}/t/projector/{stale_sid}")
    # 間隔 2.5 秒×8 次（共 20 秒），不用 5 秒×4：uvicorn 的 keep-alive 閒置逾時預設剛好 5 秒
    # （`uvicorn --help`，0.37.0），每 5 秒重用同一條連線會跟伺服器關連線撞在同一刻，偶發 ECONNRESET
    # （伺服器日誌只有第一次請求）。
    for _ in range(8):
        teacher.request.get(f"{B}/api/t/session/{stale_sid}/qr?have=")
        b121proj.wait_for_timeout(2500)
    check("連線正常" not in b121pg.locator("#projMsg").inner_text(),
          f"投影頁回應在回程遺失，點名頁不該說連線正常，實得 {b121pg.locator('#projMsg').inner_text()!r}")
    b121proj.unroute_all(behavior="ignoreErrors")
    b121proj.close()

    # 情境 B：開場超過 60 秒、投影頁仍然沒連上（projector_age 一直是 None）→ 紅字，且含補登說明
    b121pg.wait_for_function("state.opened_age>=60 && state.projector_age===null", timeout=70000)
    check(b121pg.locator("#projMsg").get_attribute("class") == "sm err",
          f"開場超過一分鐘沒收到投影頁消息是紅字，實得 class={b121pg.locator('#projMsg').get_attribute('class')!r}")
    txt = b121pg.locator("#projMsg").inner_text()
    check("開始點名一分鐘了" in txt, f"紅字文案，實得 {txt!r}")
    check("遲到上限之前" in txt and "不要按結束" in txt, f"紅字含補登說明，實得 {txt!r}")

    # 情境 C：點名頁自己的輪詢失敗（#net 顯示中）→ 灰字「狀態暫時不明」
    b121pg.route(f"**/api/t/session/{stale_sid}/state", lambda route: route.abort("timedout"))
    b121pg.wait_for_function("document.getElementById('net').style.display!=='none'", timeout=15000)
    b121pg.wait_for_function("document.getElementById('projMsg').innerText.includes('狀態暫時不明')", timeout=8000)
    check(True, "點名頁自己斷線時 #projMsg 顯示「狀態暫時不明」")
    b121pg.unroute_all(behavior="ignoreErrors")
    b121pg.close()

    # 名冊頁解除綁定、綁定場次篩選與抽點確認
    teacher.request.post(f"{B}/api/t/session/{stale_sid}/close")

    def soft_wait(pg, expr, timeout=10000):
        """等條件成立；逾時回 False 讓後面的 check 記成失敗，不要整支腳本崩掉。"""
        try:
            pg.wait_for_function(expr, timeout=timeout); return True
        except Exception:
            return False

    def api_checkin(ctx, sid, stu, name, pin="1234"):
        g = student_grant(teacher.request, ctx.request, sid)
        return ctx.request.post(f"{B}/api/checkin", form={"grant": g, "student_id": stu, "name": name, "pin": pin,
                                                          "pin2": pin, "hints": "h"}).json()

    # 名冊頁解除綁定要看回應：失敗 alert、不重新載入；成功 alert「不受首次綁定開關限制」那句
    s9a = int(re.search(r"/t/session/(\d+)", teacher.request.post(f"{B}/t/course/{cid_home}/open", form={"kind": "normal"}).url).group(1))
    ph_a = chrome.new_context()
    check(api_checkin(ph_a, s9a, "9994A030", "測試30")["ok"], "前置，9994A030 簽到並綁定")
    teacher.request.post(f"{B}/api/t/session/{s9a}/close")
    stp = teacher.new_page(); stp_msgs = []
    stp.on("dialog", lambda d: (stp_msgs.append(d.message), d.accept()))
    stp.goto(f"{B}/t/course/{cid_home}/students")
    stp.evaluate("window.__marker=1")
    row_a1 = stp.locator("tr:has-text('9994A030')")
    check("已綁" in row_a1.inner_text(), "前置，名冊上 9994A030 顯示已綁")
    stp.route("**/api/t/student/*/unbind", lambda route: route.fulfill(status=401, content_type="application/json",
                                                                     body=json.dumps({"ok": False, "error": "請先登入"})))
    row_a1.locator("button:has-text('解除綁定')").click()
    stp.wait_for_timeout(800)
    check(any("請先登入" in m for m in stp_msgs), f"解除綁定失敗（401）要 alert 出伺服器的訊息，實得 {stp_msgs}")
    check(stp.evaluate("window.__marker") == 1, "失敗時頁面不重新載入")
    check("已綁" in stp.locator("tr:has-text('9994A030')").inner_text(), "失敗後 9994A030 仍是已綁")
    stp_msgs.clear()
    stp.unroute_all(behavior="ignoreErrors")
    stp.route("**/api/t/student/*/unbind", lambda route: route.abort("connectionreset"))
    stp.locator("tr:has-text('9994A030')").locator("button:has-text('解除綁定')").click()
    stp.wait_for_timeout(800)
    check(any("連線中斷，沒有成功，請再試一次" in m for m in stp_msgs), f"網路錯誤要 alert 連線中斷，實得 {stp_msgs}")
    check(stp.evaluate("window.__marker") == 1, "網路錯誤時頁面不重新載入")
    stp_msgs.clear()
    stp.unroute_all(behavior="ignoreErrors")
    stp.locator("tr:has-text('9994A030')").locator("button:has-text('解除綁定')").click()
    stp.wait_for_function("window.__marker===undefined", timeout=10000)  # 成功後 reload，標記消失
    check(any("不受首次綁定開關限制" in m for m in stp_msgs), f"解除成功要 alert 不受首次綁定開關限制，實得 {stp_msgs}")
    check("已綁" not in stp.locator("tr:has-text('9994A030')").inner_text(), "成功後 9994A030 不再是已綁")
    stp.close()

    # 老師點名頁
    s9b = int(re.search(r"/t/session/(\d+)", teacher.request.post(f"{B}/t/course/{cid_home}/open", form={"kind": "normal"}).url).group(1))
    check(api_checkin(chrome.new_context(), s9b, "9994A031", "測試31")["ok"], "前置，9994A031 簽到成功")
    rej = api_checkin(chrome.new_context(), s9b, "9994A032", "測試32", pin="12")  # 姓名不符不歸戶，所以改用 PIN 格式被擋
    check(not rej["ok"], f"前置，9994A032 PIN 格式不對被擋，實得 {rej}")
    mock = {"na_pk": None, "late_delta": None}
    def _mock_state(route):
        r = route.fetch(); j = r.json()
        if mock["na_pk"]:
            for row in j["rows"]:
                if row["pk"] == mock["na_pk"]:
                    row["na"] = True; row["status"] = ""; row["tried"] = ""
        if mock["late_delta"] is not None:
            j["late_until"] = j["now"] + mock["late_delta"]
        route.fulfill(response=r, json=j)
    p9 = teacher.new_page()
    p9.route(f"**/api/t/session/{s9b}/state", _mock_state)
    p9.goto(f"{B}/t/session/{s9b}")
    soft_wait(p9, "state && state.rows && state.rows.some(r=>r.tried)", 15000)
    row3 = p9.locator("#rows tr:has-text('9994A032')")
    check("掃碼被擋：PIN 格式" in row3.inner_text(), f"被擋過還沒簽到的人，名單標記欄顯示「掃碼被擋：原因」，實得 {row3.inner_text()!r}")
    check("掃碼被擋" not in p9.locator("#rows tr:has-text('9994A031')").inner_text(), "已簽到的人不標")
    p9.select_option("#filter", "flag")
    check(p9.locator("#rows tr:has-text('9994A032')").count() == 1, "有 tried 的人切「有標記」要出現")
    check(p9.locator("#rows tr:has-text('9994A031')").count() == 0, "沒有標記的已簽到者切「有標記」不出現")
    # 把 9994A033 這一列改成「開場後加選」（真正加選要寫庫，這裡攔截 state 回應只改那一列，驗前端顯示與篩選）
    pk4 = next(r["pk"] for r in teacher.request.get(f"{B}/api/t/session/{s9b}/state").json()["rows"] if r["student_id"] == "9994A033")
    mock["na_pk"] = pk4
    p9.select_option("#filter", "")
    soft_wait(p9, "state.rows.some(r=>r.na)")
    check("開場後加選" in p9.locator("#rows tr:has-text('9994A033')").inner_text(), "開場後加選的人狀態欄顯示「開場後加選」")
    p9.select_option("#filter", "none")
    check(p9.locator("#rows tr:has-text('9994A033')").count() == 0, "切「未簽到」不出現加選的人")
    check(p9.locator("#rows tr:has-text('9994A034')").count() == 1, "切「未簽到」仍列出真的沒簽到的人")
    p9.select_option("#filter", "")
    # 一般點名剩 50 秒要提示延長，剩 5 分鐘不提示
    mock["late_delta"] = 300
    soft_wait(p9, "state.late_until-state.now>200")
    check(p9.locator("#extendHint").is_hidden(), "一般點名遲到區還剩 5 分鐘，不提示延長")
    mock["late_delta"] = 50
    soft_wait(p9, "state.late_until-state.now<60")
    soft_wait(p9, "document.getElementById('extendHint').style.display===''", 5000)
    check(p9.locator("#extendHint").is_visible(), "一般點名遲到區剩 50 秒，提示延長")
    p9.unroute_all(behavior="ignoreErrors"); p9.close()
    teacher.request.post(f"{B}/api/t/session/{s9b}/close")

    # 綁定場次每個綁好的人都有 late_bind，切「有標記」要 0 列；有人被擋就出現
    s9c = int(re.search(r"/t/session/(\d+)", teacher.request.post(f"{B}/t/course/{cid_home}/open", form={"kind": "bind"}).url).group(1))
    check(api_checkin(chrome.new_context(), s9c, "9994A033", "測試33")["ok"], "前置，綁定場次 9994A033 綁定")
    p9c = teacher.new_page(); p9c.goto(f"{B}/t/session/{s9c}")
    p9c.wait_for_function("state && state.rows && state.rows.some(r=>r.bound && r.status)", timeout=15000)
    check(any("late_bind" in r["flags"] for r in teacher.request.get(f"{B}/api/t/session/{s9c}/state").json()["rows"]),
          "前置，綁定場次的簽到列帶 late_bind 旗標")
    p9c.select_option("#filter", "flag")
    p9c.wait_for_timeout(500)
    check(p9c.locator("#rows tr").count() == 0, f"綁定場次切「有標記」只有 late_bind 時 0 列，實得 {p9c.locator('#rows tr').count()}")
    api_checkin(chrome.new_context(), s9c, "9994A035", "測試35", pin="12")  # 姓名不符不歸戶，所以改用 PIN 格式被擋
    soft_wait(p9c, "state.rows.some(r=>r.tried)")
    check(p9c.locator("#rows tr:has-text('9994A035')").count() == 1 and p9c.locator("#rows tr").count() == 1,
          "有人被擋後切「有標記」只出現被擋的那一列")
    p9c.close()
    teacher.request.post(f"{B}/api/t/session/{s9c}/close")

    # 提早按「結束點名」，確認框要說還有幾支手機在填表、出席窗還沒到
    # 學生端落地事件（land_ok）只有掃 /s/{sid}/{slot}/{sig} 才會記，e2e 沒辦法解 QR，直接在這層的暫存庫補一筆。
    import sqlite3, time as _time
    s10 = int(re.search(r"/t/session/(\d+)", teacher.request.post(f"{B}/t/course/{cid_home}/open", form={"kind": "normal"}).url).group(1))
    _db = sqlite3.connect(E2E_DB, timeout=10)
    _db.execute("INSERT INTO events(ts,session_id,kind,device8,ua_class) VALUES(?,?,?,?,?)", (_time.time(), s10, "land_ok", "dev1dev0", "iphone"))
    _db.commit(); _db.close()
    p10 = teacher.new_page(); forming_msgs = []
    p10.on("dialog", lambda d: (forming_msgs.append(d.message), d.dismiss()))  # 按「取消」，場次留著
    p10.goto(f"{B}/t/session/{s10}")
    check(soft_wait(p10, "state && state.filling>=1"), "前置，state.filling 算到還在填表的手機")
    p10.click("#closeBtn"); p10.wait_for_timeout(400)
    check(any("1 支手機在填表" in m for m in forming_msgs), f"確認框說還有手機在填表，實得 {forming_msgs}")
    check(any("出席窗還要" in m and "分鐘才關" in m for m in forming_msgs), f"出席窗還沒到時確認框也說還要幾分鐘才關，實得 {forming_msgs}")
    p10.close()
    teacher.request.post(f"{B}/api/t/session/{s10}/close")

    # 抽點、取消結束、名單版面
    _dbp = E2E_DB

    # 已簽到、又有陌生新環境打對 PIN 被擋（already_recorded）→ 灰字 tag（沒有 err 類別）
    s11 = int(re.search(r"/t/session/(\d+)", teacher.request.post(f"{B}/t/course/{cid_home}/open", form={"kind": "normal"}).url).group(1))
    check(api_checkin(chrome.new_context(), s11, "9994A036", "測試36")["ok"], "前置，9994A036 簽到")
    _db = sqlite3.connect(_dbp, timeout=10)
    _pk36 = _db.execute("SELECT id FROM students WHERE student_id='9994A036'").fetchone()[0]
    _db.execute("INSERT INTO events(ts,session_id,kind,student_pk,reason,device8,ua_class) VALUES(?,?,?,?,?,?,?)",
                (_time.time() + 1, s11, "checkin_reject", _pk36, "already_recorded", "stranger0", "iphone"))
    _db.commit(); _db.close()
    p11 = teacher.new_page(); p11_msgs = []
    p11.on("dialog", lambda d: (p11_msgs.append(d.message), d.dismiss()))
    p11.goto(f"{B}/t/session/{s11}")
    check(soft_wait(p11, "state && state.stuck && state.stuck.some(x=>x.info)"), "前置，state.stuck 有 info 項")
    _tag = p11.locator("#stuck .tag:has-text('9994A036')")
    check(_tag.count() == 1 and "err" not in (_tag.first.get_attribute("class") or ""), "#stuck 裡該人的 tag 沒有 err 類別（灰字）")
    check("不用處理" in p11.locator("#stuck").inner_text(), "灰字區有「不用處理」說明")

    # 抽點「不在」要先確認；取消不送出，確定才送出
    teacher.request.post(f"{B}/api/t/session/{s11}/spotcheck")
    p11.close()
    p11c = teacher.new_page(); p11c_msgs = []; p11c_mode = {"accept": False}; p11c_posts = []
    p11c.on("dialog", lambda d: (p11c_msgs.append(d.message), d.accept() if p11c_mode["accept"] else d.dismiss()))
    p11c.on("request", lambda r: p11c_posts.append(r.url) if "/api/t/spotcheck/" in r.url and r.method == "POST" else None)
    p11c.goto(f"{B}/t/session/{s11}"); p11c.wait_for_selector("#picks .pick button.danger")
    p11c.locator("#picks .pick button.danger").first.click(); p11c.wait_for_timeout(500)
    check(any("不在？" in m and "記缺席" in m for m in p11c_msgs), f"一般場次「不在」確認框說明記缺席，實得 {p11c_msgs}")
    check(not p11c_posts, f"按取消不送出，實得 {p11c_posts}")
    p11c_mode["accept"] = True
    p11c.locator("#picks .pick button.danger").first.click(); p11c.wait_for_timeout(800)
    check(len(p11c_posts) == 1, f"按確定才送出一次，實得 {p11c_posts}")
    p11c.close()
    # 結束後出現「取消結束」，按下去（接受確認框）後 #closeBtn 重新可按
    p11b = teacher.new_page(); p11b_msgs = []
    p11b.on("dialog", lambda d: (p11b_msgs.append(d.message), d.accept()))
    p11b.goto(f"{B}/t/session/{s11}")
    p11b.wait_for_selector("#closeBtn")
    p11b.click("#closeBtn")
    check(soft_wait(p11b, "state && state.phase==='closed'"), "前置，按結束後 phase=closed")
    check(soft_wait(p11b, "document.getElementById('reopenBtn').style.display===''"), "結束後「取消結束」按鈕出現")
    check("分鐘內可按" in p11b.locator("#reopenBtn").inner_text(), f"取消結束按鈕文字含「分鐘內可按」，實得 {p11b.locator('#reopenBtn').inner_text()!r}")
    p11b.click("#reopenBtn")
    check(soft_wait(p11b, "state && state.phase!=='closed'"), "取消結束後 phase 不再是 closed")
    check(p11b.locator("#closeBtn").is_enabled() and p11b.locator("#closeBtn").is_visible(), "#closeBtn 重新可按")
    check(any("取消結束？" in m for m in p11b_msgs), f"有跳確認框，實得 {p11b_msgs}")
    check(any("請他們重新掃 QR" in m and "至少再開 2 分鐘" in m for m in p11b_msgs), f"取消結束成功後的 alert 叫被擋的同學重新掃 QR，實得 {p11b_msgs}")

    # api_qr 回 opened_at
    _q = teacher.request.get(f"{B}/api/t/session/{s11}/qr").json()
    check(isinstance(_q.get("opened_at"), (int, float)) and _q["opened_at"] > 0, f"api_qr 帶 opened_at，實得 {_q.get('opened_at')!r}")

    # 390x844 名單不用左右捲，下拉與標記都在視窗內
    # 先造出一筆紅字（9994A038 已簽到後另一支手機打錯 PIN）與一筆灰字（9994A039 已簽到後新環境打對 PIN）；
    # 抽點那段的灰字（9994A036）已經被前面的抽點「不在」改成缺席，不再算已簽到
    for _stu, _nm in (("9994A038", "測試38"), ("9994A039", "測試39")):
        check(api_checkin(chrome.new_context(), s11, _stu, _nm)["ok"], f"前置，{_stu} 簽到")
    _db = sqlite3.connect(_dbp, timeout=10)
    for _stu, _reason, _dev in (("9994A038", "pin_wrong", "stranger1"), ("9994A039", "already_recorded", "stranger2")):
        _pk = _db.execute("SELECT id FROM students WHERE student_id=?", (_stu,)).fetchone()[0]
        _db.execute("INSERT INTO events(ts,session_id,kind,student_pk,reason,device8,ua_class) VALUES(?,?,?,?,?,?,?)",
                    (_time.time() + 1, s11, "checkin_reject", _pk, _reason, _dev, "iphone"))
    _db.commit(); _db.close()
    p11b.set_viewport_size({"width": 390, "height": 844})
    p11b.reload(); p11b.wait_for_selector("#rows tr")
    check(soft_wait(p11b, "state && state.stuck && state.stuck.some(x=>x.conflict) && state.stuck.some(x=>x.info)"), "前置，#stuck 同時有紅字與灰字")
    _stuck_over = p11b.evaluate("""(()=>{const vw=document.documentElement.clientWidth;return [...document.querySelectorAll('#stuck .tag,#picks .tag')].filter(e=>e.getBoundingClientRect().right>vw).length})()""")
    check(_stuck_over == 0, f"390 寬 #stuck .tag 與 #picks .tag 右緣都在視窗內，超出 {_stuck_over} 個")
    check(p11b.locator("#stuck .tag").count() >= 2, f"#stuck 至少兩個標籤，實得 {p11b.locator('#stuck .tag').count()}")
    _th_vis = p11b.evaluate("[...document.querySelectorAll('#rowsTable th')].filter(e=>e.offsetParent!==null).length")
    check(_th_vis == 0, f"390 寬名單表頭（th）都不可見，可見 {_th_vis} 個")
    _sw = p11b.evaluate("document.documentElement.scrollWidth")
    check(_sw <= 390, f"390 寬 scrollWidth ≤ 390，實得 {_sw}")
    _tw = p11b.evaluate("(()=>{const w=document.querySelector('.tablewrap');return w.scrollWidth-w.clientWidth})()")
    check(_tw <= 0, f"名單容器不需要橫向捲動，超出 {_tw}")
    _bad = p11b.evaluate("""(()=>{let n=0;document.querySelectorAll('#rows select.mini,#rows .tag').forEach(e=>{const b=e.getBoundingClientRect();if(b.left<0||b.right>390)n++});return n})()""")
    check(_bad == 0, f"下拉與標記都在 0–390 之內，超出 {_bad} 個")
    p11b.set_viewport_size({"width": 1280, "height": 900}); p11b.reload(); p11b.wait_for_selector("#rows tr")
    _th_vis2 = p11b.evaluate("[...document.querySelectorAll('#rowsTable th')].filter(e=>e.offsetParent!==null).length")
    check(_th_vis2 >= 5, f"1280 寬名單表頭可見，可見 {_th_vis2} 個")
    p11b.close()
    teacher.request.post(f"{B}/api/t/session/{s11}/close")

    # 綁定場次的確認框要講「撤回綁定、救不回來」
    s11b = int(re.search(r"/t/session/(\d+)", teacher.request.post(f"{B}/t/course/{cid_home}/open", form={"kind": "bind"}).url).group(1))
    check(api_checkin(chrome.new_context(), s11b, "9994A037", "測試37")["ok"], "前置，綁定場次 9994A037 綁定")
    teacher.request.post(f"{B}/api/t/session/{s11b}/spotcheck")
    p11d = teacher.new_page(); p11d_msgs = []
    p11d.on("dialog", lambda d: (p11d_msgs.append(d.message), d.dismiss()))
    p11d.goto(f"{B}/t/session/{s11b}"); p11d.wait_for_selector("#picks .pick button.danger")
    p11d.locator("#picks .pick button.danger").first.click(); p11d.wait_for_timeout(500)
    check(any("撤回這個學號的綁定" in m and "救不回來" in m for m in p11d_msgs), f"綁定場次確認框說明撤回綁定，實得 {p11d_msgs}")
    p11d.close()
    # 390 寬、綁定場次抽點判「不在」之後，那條長標籤不能超出畫面右緣
    p11e = teacher.new_page(); p11e_msgs = []
    p11e.on("dialog", lambda d: (p11e_msgs.append(d.message), d.accept()))
    p11e.set_viewport_size({"width": 390, "height": 844})
    p11e.goto(f"{B}/t/session/{s11b}"); p11e.wait_for_selector("#picks .pick button.danger")
    p11e.locator("#picks .pick button.danger").first.click()
    check(soft_wait(p11e, "state && state.spots && state.spots.some(x=>x.result==='absent')"), "前置，綁定場次抽點已判不在")
    p11e.wait_for_selector("#picks .tag.flag")
    _pick_over = p11e.evaluate("""(()=>{const vw=document.documentElement.clientWidth;return [...document.querySelectorAll('#picks .tag')].filter(e=>e.getBoundingClientRect().right>vw).length})()""")
    check(_pick_over == 0, f"390 寬綁定場次抽點標籤右緣都在視窗內，超出 {_pick_over} 個")
    _pick_sw = p11e.evaluate("document.documentElement.scrollWidth")
    check(_pick_sw <= 390, f"綁定場次頁 scrollWidth ≤ 390，實得 {_pick_sw}")
    check("首次綁定要開著" in p11e.locator("#picks .tag.flag").first.inner_text(), "綁定場次抽點「不在場」標籤加註首次綁定要開著")
    p11e.close()
    teacher.request.post(f"{B}/api/t/session/{s11b}/close")

    # 名冊頁確認框講是誰、成功提示依旗標
    p12 = teacher.new_page(); p12_msgs = []
    p12.on("dialog", lambda d: (p12_msgs.append(d.message), d.accept()))
    p12.goto(f"{B}/t/course/{cid_home}/students")
    p12.locator("tr:has-text('9994A036') button:has-text('解除綁定')").click(); p12.wait_for_timeout(800)
    check(any("解除 9994A036 測試36 的手機綁定？" in m for m in p12_msgs), f"解除綁定確認框帶學號姓名，實得 {p12_msgs}")
    check(any(m.startswith("已解除。") for m in p12_msgs), f"成功提示以「已解除。」開頭，實得 {p12_msgs}")
    p12_msgs.clear()
    p12.goto(f"{B}/t/course/{cid_home}/students")
    p12.locator("tr:has-text('9994A036') button:has-text('重設')").click(); p12.wait_for_timeout(800)
    check(any("重設 9994A036 測試36？" in m for m in p12_msgs), f"重設確認框帶學號姓名，實得 {p12_msgs}")
    check(any(m.startswith("已重設。") for m in p12_msgs), f"重設成功提示以「已重設。」開頭，實得 {p12_msgs}")
    p12.close()

    # 誤按結束與重開確認
    # 誤按結束後回主頁按「開始點名」，先跳確認框；按取消就沒有開新的一場
    s12 = int(re.search(r"/t/session/(\d+)", teacher.request.post(f"{B}/t/course/{cid_home}/open", form={"kind": "normal"}).url).group(1))
    teacher.request.post(f"{B}/api/t/session/{s12}/close")
    _db = sqlite3.connect(_dbp, timeout=10); _n_before = _db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]; _db.close()
    p13 = teacher.new_page(); p13_msgs = []
    p13.on("dialog", lambda d: (p13_msgs.append(d.message), d.dismiss()))
    p13.goto(f"{B}/t")
    p13.locator(f"form[action='/t/course/{cid_home}/open'] button[value=normal]").click(); p13.wait_for_timeout(800)
    check(any("上一場 " in m and "分鐘前才結束" in m and "取消結束" in m for m in p13_msgs), f"回主頁按「開始點名」先跳確認框，實得 {p13_msgs}")
    _db = sqlite3.connect(_dbp, timeout=10); _n_after = _db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]; _db.close()
    check(_n_after == _n_before, f"按取消後沒有開新場次（sessions {_n_before} →{_n_after}）")
    check(p13.url.rstrip("/").endswith("/t"), f"按取消後留在主頁，實得 {p13.url}")
    p13.close()

    # 按住「不在」跨過一次輪詢重繪再放開，click 不能被吃掉（Chromium 與 WebKit 各跑一次）
    _wk = p.webkit.launch()
    for _label, _br, _stus in (("Chromium", chrome, ("9994A040", "9994A041")), ("WebKit", _wk, ("9994A042", "9994A043"))):
        _tc = _br.new_context(viewport={"width": 1280, "height": 800}); _tpg = _tc.new_page()
        _tpg.goto(f"{B}/t/login"); _tpg.fill("input[name=password]", "pw"); _tpg.click("button:has-text('登入')"); _tpg.wait_for_url(f"{B}/t")
        s13 = int(re.search(r"/t/session/(\d+)", _tc.request.post(f"{B}/t/course/{cid_home}/open", form={"kind": "normal"}).url).group(1))
        for _i, _stu in enumerate(_stus):
            check(api_checkin(chrome.new_context(), s13, _stu, "測試" + _stu[-2:])["ok"], f"瀏覽器 {_label}：前置，{_stu} 簽到")
        _tc.request.post(f"{B}/api/t/session/{s13}/spotcheck")
        _tpg.goto(f"{B}/t/session/{s13}"); _tpg.wait_for_selector("#picks .pick button.danger")
        _dlg = []; _tpg.on("dialog", lambda d: (_dlg.append(d.message), d.dismiss()))
        _bb = _tpg.locator("#picks button.danger").first.bounding_box()
        _tpg.mouse.move(_bb["x"] + _bb["width"] / 2, _bb["y"] + _bb["height"] / 2)
        _tpg.mouse.down()
        _seq0 = _tpg.evaluate("loadSeq")
        _tpg.wait_for_function(f"loadSeq>{_seq0} && !inflight", timeout=15000)  # 等到按住期間至少完成了一輪輪詢（條件，不是固定秒數）
        _tpg.mouse.up(); _tpg.wait_for_timeout(500)
        check(len(_dlg) == 1 and "不在" in _dlg[0], f"瀏覽器 {_label}：按住跨過一次輪詢重繪後放開，仍跳出確認框，實得 {_dlg}")
        _tc.request.post(f"{B}/api/t/session/{s13}/close"); _tc.close()
    _wk.close()

    chrome.close()

# 獨立成子程序跑的前端回歸（見各檔檔頭說明：sync API 對「先放行、延後回應」這類重複
# 攔截手法在這個 app 上會讓瀏覽器 fetch() 卡住不 resolve，這兩支非本檔的問題，改用 async API）；
# 手機版面那支純量測、不牽涉延後回應，但需要另開 WebKit engine，獨立成子程序不跟本檔的 chromium 搶資源 ──
for _name in ("e2e_out_of_order.py", "e2e_manual_checkin.py", "e2e_phone_layout.py", "e2e_teacher_roster.py", "e2e_teacher_stuck.py"):  # b14：名單列位移、遲到確認、連按、重開重載；b15：重設鈕、IP 不顯示、卡住名單版面、名單輪詢
    _r = subprocess.run([sys.executable, os.path.join(os.path.dirname(__file__), _name)],
                        capture_output=True, text=True)
    print(_r.stdout, end="")
    if _r.stderr:
        print(_r.stderr, file=sys.stderr, end="")
    if _r.returncode != 0:
        FAIL.append(f"{_name} 有項目沒過（見上方輸出）")

print("\n==>", "全部通過" if not FAIL else f"{len(FAIL)} 項失敗：{FAIL}")
sys.exit(1 if FAIL else 0)
