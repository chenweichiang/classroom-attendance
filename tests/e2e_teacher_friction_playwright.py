"""老師端與投影頁摩擦的前端回歸（BUGLOG B014 投影頁那支/B025/B031/B032/B033/B034/B035）。
前提同 e2e_friction_playwright.py（乾淨庫、ATTEND_TEST_BASE 預設埠、NTUB-CD 48 人名冊）；
gate.sh 的 e2e-teacher 層另外把這門課的 qr_interval 設成最低值 5 秒，讓 B025 卡格測試不用等 10 秒以上。"""
import os, re, sys
from playwright.sync_api import sync_playwright

B = os.environ.get("ATTEND_TEST_BASE", "http://127.0.0.1:8765"); FAIL = []
def check(c, m):
    print(("ok   " if c else "FAIL ") + m)
    if not c: FAIL.append(m)

with sync_playwright() as p:
    chrome = p.chromium.launch()
    teacher = chrome.new_context(viewport={"width": 1280, "height": 900})
    tp = teacher.new_page()
    tp.goto(f"{B}/t/login"); tp.fill("input[name=password]", "pw"); tp.click("button:has-text('登入')"); tp.wait_for_url(f"{B}/t")
    tp.locator("form[action$='/open'] button").first.click(); tp.wait_for_url(re.compile(r"/t/session/\d+")); sid = int(tp.url.rsplit("/", 1)[1])

    # ───── B031：手動簽到只用既有 /api/t/session/{sid}/status，輸後 3 碼或姓名片段給候選人 ─────
    sess = tp  # 就是點名頁那分頁
    sess.wait_for_selector("#rows tr")
    posts = []
    sess.on("request", lambda r: posts.append(r.post_data) if "/status" in r.url and r.method == "POST" else None)

    sess.fill("#mid", "測試7")
    sess.wait_for_function("document.getElementById('mcands').innerText.includes('測試7')")
    check("測試7" in sess.locator("#mcands").inner_text(), "B031：姓名片段找到候選人")
    check(sess.locator("#mcands button:has-text('出席')").count() == 1, "B031：候選人旁有「出席」鈕")
    check(sess.locator("#mcands button:has-text('遲到')").count() == 1, "B031：候選人旁有「遲到」鈕")

    sess.fill("#mid", "A047")
    sess.wait_for_function("document.getElementById('mcands').innerText.includes('9994A047')")
    check("9994A047" in sess.locator("#mcands").inner_text(), "B031：學號後 4 碼找到唯一候選人")
    sess.locator("#mcands").get_by_text("遲到").click()
    sess.wait_for_timeout(300)
    check(any(pd and 'name="status"' in pd and "late" in pd for pd in posts), "B031：點「遲到」鈕直接呼叫既有 status API 記遲到")
    check(sess.locator("#mid").input_value() == "", "B031：送出後輸入框清空")

    posts.clear()
    sess.fill("#mid", "A048")
    sess.wait_for_function("document.getElementById('mcands').innerText.includes('9994A048')")
    sess.locator("#mid").press("Enter")
    sess.wait_for_timeout(300)
    check(any(pd and 'name="status"' in pd and "present" in pd for pd in posts), "B031：唯一候選人時按 Enter 直接記出席（不必再點鈕）")
    sess.fill("#mid", "")

    # ───── B034：遮姓名，預設關、狀態存 localStorage、旗標不受影響 ─────
    sess.reload(); sess.wait_for_selector("#rows tr")
    check("遮姓名：已關閉" in sess.locator("#maskBtn").inner_text(), "B034：預設關閉")
    full_name_row = sess.locator("#rows tr").first.inner_text()
    sess.click("#maskBtn")
    sess.wait_for_function("document.getElementById('maskBtn').innerText.includes('開啟中')")
    check("○○" in sess.locator("#rows tr").first.inner_text(), "B034：開啟後姓名遮成「姓○○」")
    sess.reload(); sess.wait_for_selector("#rows tr")
    check("○○" in sess.locator("#rows tr").first.inner_text(), "B034：重新整理後仍記得開啟（localStorage）")
    sess.click("#maskBtn")  # 關掉，不影響後面的測試
    sess.wait_for_function("document.getElementById('maskBtn').innerText.includes('已關閉')")

    # ───── B032/B033：投影頁 QR 尺寸與無捲軸（三個目標視窗）─────
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
        check(box["width"] >= short * 0.85 and box["height"] >= short * 0.85, f"B033：{w}x{h} QR 邊長 ≥ 短邊 85%")
        check(scrollW <= w and scrollH <= h, f"B033：{w}x{h} 沒有捲軸")
        # body 是 overflow:hidden，「沒有捲軸」永遠成立；真正要驗的是 QR 整顆在畫面內、沒被上下資訊列壓到
        top_box = proj.locator(".top").bounding_box(); bot_box = proj.locator(".bot").bounding_box()
        assert box and top_box and bot_box
        check(box["y"] >= top_box["y"] + top_box["height"] - 0.5 and box["y"] + box["height"] <= bot_box["y"] + 0.5
              and box["x"] >= 0 and box["x"] + box["width"] <= w,
              f"B033：{w}x{h} QR 整顆落在上下資訊列之間（QR y={box['y']:.0f}–{box['y']+box['height']:.0f}，"
              f"上列底={top_box['y']+top_box['height']:.0f}，下列頂={bot_box['y']:.0f}）")
        expect_font = min(9 * h / 100, 12 * w / 100, 110)
        check(font_px >= expect_font - 0.5, f"B032/B033：{w}x{h} 六位數字體沒有比既有公式（min(9vh,12vw)）小（實測 {font_px:.1f}px，基準 {expect_font:.1f}px）")
    proj.set_viewport_size({"width": 1280, "height": 900})

    # ───── B025：投影頁本地時鐘自我檢查，QR 卡格會蓋遮罩；網路恢復自動收起 ─────
    proj.route(f"**/api/t/session/{sid}/qr*", lambda route: route.abort())
    proj.wait_for_function("document.body.classList.contains('stale')", timeout=25000)
    check(not proj.locator("#qr").is_visible(), "B025：卡格時 QR 被遮罩蓋住")
    check("畫面沒有更新" in proj.locator("#stalemsg").inner_text(), "B025：卡格提示文字正確")
    proj.unroute(f"**/api/t/session/{sid}/qr*")
    proj.wait_for_function("!document.body.classList.contains('stale')", timeout=15000)
    check(True, "B025：網路恢復後遮罩自動收起")

    # ───── B025：visibilitychange 回到前景要立刻補一次輪詢，不用等下一輪排程 ─────
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
    check(len(reqs) >= 1, "B025：切回前景立刻補打一次 /qr，不等排程")
    ctxV.close()

    # ───── B014：投影頁沒有 AbortSignal.timeout 一樣要能持續輪詢，不是整支卡死 ─────
    ctxN = chrome.new_context(viewport={"width": 1280, "height": 900})
    ctxN.add_init_script("delete AbortSignal.timeout;")
    npg = ctxN.new_page()
    npg.goto(f"{B}/t/login"); npg.fill("input[name=password]", "pw"); npg.click("button:has-text('登入')"); npg.wait_for_url(f"{B}/t")
    npg.goto(f"{B}/t/projector/{sid}")
    npg.wait_for_function("document.querySelector('#qr svg')!==null")
    c0 = npg.locator("#code").inner_text()
    npg.wait_for_function(f"document.getElementById('code').innerText!=={c0!r}", timeout=15000)
    check(True, "B014：拿掉 AbortSignal.timeout，投影頁一樣能持續換碼")
    ctxN.close()

    # ───── B035：老師 token 失效時投影幕給全螢幕文字提示，不是跳成登入表單 ─────
    ctxE = chrome.new_context(viewport={"width": 1280, "height": 900})
    epg = ctxE.new_page()
    epg.goto(f"{B}/t/login"); epg.fill("input[name=password]", "pw"); epg.click("button:has-text('登入')"); epg.wait_for_url(f"{B}/t")
    epg.goto(f"{B}/t/projector/{sid}")
    epg.wait_for_function("document.querySelector('#qr svg')!==null")
    ctxE.clear_cookies()
    epg.wait_for_function("document.body.classList.contains('authexpired')", timeout=15000)
    check("登入已失效" in epg.locator("#authmsg").inner_text(), "B035：401 顯示全螢幕文字提示")
    check(epg.locator("input[type=password]").count() == 0, "B035：畫面上沒有出現密碼輸入框（沒有跳成登入表單）")
    ctxE.close()

    # ───── B028：本場作廢（標籤與確認文案）＋自動缺席整批更正（老師頁新按鈕） ─────
    sess.bring_to_front()
    dialog_msgs = []
    sess.on("dialog", lambda d: (dialog_msgs.append(d.message), d.accept()))
    sess.click("#closeBtn")
    # closeBtn.disabled 在 guard() 一開始（POST 送出之前）就同步設 true，不能拿來判斷「已經結案渲染完」；
    # 要等 #phase 文字真的變成「已結束」（load(true) 拿到新 state 後 render() 才會把 voidBtn/bulkRow 更新）。
    sess.wait_for_function("document.getElementById('phase').innerText==='已結束'", timeout=15000)
    check(sess.locator("#voidBtn").is_visible(), "B028：結案後出現「本場作廢」按鈕")
    check("本場作廢" in sess.locator("#voidBtn").inner_text(), "B028：作廢按鈕預設文字")
    bulk_visible_before = sess.locator("#bulkRow").is_visible()
    check(bulk_visible_before, "B028：有自動缺席筆數時顯示「自動缺席 N 筆」整批更正列")
    if bulk_visible_before:
        check("自動缺席" in sess.locator("#bulkLabel").inner_text(), "B028：整批更正列文字含「自動缺席」")
        sess.select_option("#bulkTo", "excused")
        sess.click("#bulkRow button")
        sess.wait_for_timeout(400)
        check(sess.locator("#bulkRow").is_hidden() or "自動缺席 0" in sess.locator("#bulkLabel").inner_text(),
              "B028：整批更正後自動缺席筆數歸零，整批更正列跟著收起或歸零")
    dialog_msgs.clear()
    sess.click("#voidBtn")
    sess.wait_for_function("document.getElementById('voidBtn').innerText.includes('已作廢')")
    check(True, "B028：按一下「本場作廢」後按鈕文字切成「已作廢（按一下取消作廢）」")
    check(any("不會被撤回" in m for m in dialog_msgs), f"B028：作廢確認對話框有寫明 Discord 不撤回，實得 {dialog_msgs}")

    # ───── C-T2/C-T6：綁定場次老師頁——隱藏狀態下拉／手動簽到、counts 只顯示「已綁定 N 人」、首次綁定鈕鎖定文字 ─────
    r_cid = teacher.request.get(f"{B}/t")
    m_cid = re.search(r"course/(\d+)/open", r_cid.text())
    cid = int(m_cid.group(1))
    r_bind = teacher.request.post(f"{B}/t/course/{cid}/open", form={"kind": "bind"})
    bind_sid = int(re.search(r"/t/session/(\d+)", r_bind.url).group(1))
    bpage = teacher.new_page(); bpage.goto(f"{B}/t/session/{bind_sid}")
    bpage.wait_for_function("state && state.kind==='bind'")
    check("已綁定" in bpage.locator("#counts").inner_text(), "C-T2：綁定場次 counts 只顯示「已綁定 N 人」")
    check(bpage.locator("#manualSection").is_hidden(), "C-T2：綁定場次隱藏「手動簽到」區塊")
    check(bpage.locator("#rows select.mini").count() == 0, "C-T2：綁定場次名單每列沒有改狀態的下拉")
    check("另開一般點名" in bpage.locator("#rows").inner_text(), "C-T2：綁定場次名單改成說明文字")
    check(bpage.locator("#bindBtn").is_disabled(), "C-T6：綁定場次的首次綁定鈕鎖定不可按")
    check("本場一律開放" in bpage.locator("#bindBtn").inner_text(), "C-T6：綁定場次的首次綁定鈕文字改成「本場一律開放」")
    check(bpage.locator("#spotBtn").is_visible(), "C-S2：B030「綁定場次不開放抽點」作廢，抽點按鈕在綁定場次仍要看得到")

    # ───── C-T4：同課已有一場未結案綁定場次時，按「開始點名」（種類不同）要看到錯誤說明，不是沿用也不是新開 ─────
    r_mismatch = teacher.request.post(f"{B}/t/course/{cid}/open", form={"kind": "normal"})
    check(r_mismatch.status == 409, f"C-T4：種類不同時開場應該 409，實得 {r_mismatch.status}")
    check("先結束" in r_mismatch.text() and "綁定場次" in r_mismatch.text(), f"C-T4：錯誤頁說明原因，實得 {r_mismatch.text()[:200]!r}")

    # ───── C-T1：綁定場次與已作廢場次隱藏「發到 Discord」與暫緩提示 ─────
    teacher.request.post(f"{B}/t/course/{cid}/settings", form={
        "open_minutes": "3", "late_minutes": "15", "qr_interval": "10", "qr_grace": "30",
        "spot_n": "3", "bind_minutes": "10", "discord_channel": "123456789012345"})
    teacher.request.post(f"{B}/api/t/session/{bind_sid}/close")
    bpage.reload(); bpage.wait_for_function("state && state.phase==='closed'")
    check(bpage.locator("#notifyBtn").is_hidden(), "C-T1：綁定場次結案後仍不顯示「發到 Discord」（即使課程已設頻道）")

    chrome.close()

print("\n==>", "全部通過" if not FAIL else f"{len(FAIL)} 項失敗：{FAIL}")
sys.exit(1 if FAIL else 0)
