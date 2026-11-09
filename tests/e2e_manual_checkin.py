"""手動簽到失敗清單要跟名單對帳，晚到的成功不可清掉下一位。

用 async API：跟 e2e_out_of_order.py 同樣理由，這裡也要「請求先讓它真的寫進資料庫、回應
本身才延後或遺失」，需要在等待期間讓瀏覽器同時做別的事（打字、下拉選單），sync API 在這個
app 上對這類「先放行、延後回應」的路由攔截有 hang 住的問題（見該檔檔頭說明），改用 async API。
"""
import os, re, sys, asyncio
from playwright.async_api import async_playwright

B = os.environ.get("ATTEND_TEST_BASE", "http://127.0.0.1:64890")
FAIL = []


def check(cond, msg):
    print(("ok  " if cond else "FAIL"), msg)
    if not cond:
        FAIL.append(msg)


async def open_session(ctx, tp):
    html = await (await ctx.request.get(f"{B}/t")).text()
    for s in set(re.findall(r"/t/session/(\d+)", html)):
        await ctx.request.post(f"{B}/api/t/session/{s}/close")
    html = await (await ctx.request.get(f"{B}/t")).text()
    cid = int(re.search(r"course/(\d+)/open", html).group(1))
    r = await ctx.request.post(f"{B}/t/course/{cid}/open", form={"kind": "normal"})
    return int(re.search(r"/t/session/(\d+)", r.url).group(1))


async def scenario_reconcile():
    """點 1＋2：請求寫入成功、回應遺失 → 失敗清單先顯示「沒有送到」；下一次輪詢套用新名單
    （該生狀態已經等於當時想改成的狀態）之後，失敗清單那筆要自動消失。"""
    async with async_playwright() as p:
        chrome = await p.chromium.launch()
        ctx = await chrome.new_context(viewport={"width": 1280, "height": 900})
        tp = await ctx.new_page()
        await tp.goto(f"{B}/t/login"); await tp.fill("input[name=password]", "pw")
        await tp.click("button:has-text('登入')"); await tp.wait_for_url(f"{B}/t")
        sid = await open_session(ctx, tp)
        sess = await ctx.new_page()

        async def lost(route):
            if route.request.method == "POST":
                await route.fetch()
                await route.abort("connectionreset")
            else:
                await route.continue_()
        await sess.route("**/api/t/session/*/status", lost)
        await sess.goto(f"{B}/t/session/{sid}")
        await sess.wait_for_selector("#rows tr")
        await sess.fill("#mid", "A047")
        await sess.wait_for_function("document.getElementById('mcands').innerText.includes('9994A047')")
        # 失敗後 mSet 緊接著 load(true)，那一輪的 reconcile 可能幾十毫秒內就把這筆清掉，
        # 「1 秒內輪詢 #mfails 看得到」會跟它賽跑，機器忙時偶發逾時。
        # 所以點下去之前先掛 MutationObserver，把 renderMFails 加進來、帶這個學號的節點文字記下來，
        # 之後才被清掉也讀得到。
        await sess.evaluate("""() => {
            window.__mfSeen = [];
            new MutationObserver(rs => { for (const r of rs) for (const n of r.addedNodes) {
                const t = n.textContent || ''; if (t.includes('9994A047')) window.__mfSeen.push(t); } })
              .observe(document.getElementById('mfails'), {childList: true, subtree: true});
        }""")
        await sess.locator("#mcands button:has-text('出席')").click()
        mfails_text = await sess.wait_for_function(
            "window.__mfSeen.length ? window.__mfSeen.join('\\n') : false", timeout=6000)
        mfails_text = await mfails_text.json_value()
        check("沒有送到" in mfails_text, f"前提：回應遺失時失敗清單顯示「沒有送到」，實得 {mfails_text!r}")
        row_text = (await sess.locator("#rows tr", has_text="9994A047").inner_text()).replace("\n", " | ")
        check("出席" in row_text, f"前提：名單上這個人其實已經是出席（伺服器有收到），實得 {row_text!r}")
        await sess.unroute_all(behavior="ignoreErrors")
        # 解除攔截後下一次輪詢（最多 3 秒一次）會拿到新名單，reconcile 邏輯應該把失敗清單那筆清掉
        await sess.wait_for_function(
            "!document.getElementById('mfails').innerText.includes('9994A047')", timeout=6000)
        check(True, "點 1：下一輪輪詢套用新名單後，失敗清單裡「狀態已經對上」的那筆自動消失")
        await chrome.close()


async def scenario_dropdown_clears():
    """點 2：手動簽到失敗掛著時，老師改用名單下拉把同一位改掉並成功 → 失敗清單那筆消失
    （不要求新狀態等於當初手動簽到想改成的狀態）。"""
    async with async_playwright() as p:
        chrome = await p.chromium.launch()
        ctx = await chrome.new_context(viewport={"width": 1280, "height": 900})
        tp = await ctx.new_page()
        await tp.goto(f"{B}/t/login"); await tp.fill("input[name=password]", "pw")
        await tp.click("button:has-text('登入')"); await tp.wait_for_url(f"{B}/t")
        sid = await open_session(ctx, tp)
        sess = await ctx.new_page()

        async def lost(route):
            if route.request.method == "POST" and "/status" in route.request.url:
                await route.abort("connectionreset")  # 手動簽到那次直接失敗（不寫入）
            else:
                await route.continue_()
        await sess.route("**/api/t/session/*/status", lost)
        await sess.goto(f"{B}/t/session/{sid}")
        await sess.wait_for_selector("#rows tr")
        await sess.fill("#mid", "A048")
        await sess.wait_for_function("document.getElementById('mcands').innerText.includes('9994A048')")
        await sess.locator("#mcands button:has-text('出席')").click()
        await sess.wait_for_timeout(1500)
        check("9994A048" in (await sess.locator("#mfails").inner_text()), "前提：手動簽到送不出去，失敗清單有這筆")
        await sess.unroute_all(behavior="ignoreErrors")
        sel = sess.locator("#rows tr", has_text="9994A048").locator("select")
        await sel.select_option("late")  # 老師改用下拉把狀態定案成「遲到」——跟原本想改的「出席」不同
        await sess.wait_for_timeout(1500)
        check("9994A048" not in (await sess.locator("#mfails").inner_text()),
              "點 2：改用名單下拉成功後，失敗清單那筆立刻消失（不用等狀態對上）")
        await chrome.close()


async def scenario_slow_success_preserves_mid():
    """點 3：手動簽到成功但回應延後 2.5 秒，期間老師已經在 #mid 打下一位——回應到了之後
    #mid 內容不能被清掉、焦點不能被搶走。"""
    async with async_playwright() as p:
        chrome = await p.chromium.launch()
        ctx = await chrome.new_context(viewport={"width": 1280, "height": 900})
        tp = await ctx.new_page()
        await tp.goto(f"{B}/t/login"); await tp.fill("input[name=password]", "pw")
        await tp.click("button:has-text('登入')"); await tp.wait_for_url(f"{B}/t")
        sid = await open_session(ctx, tp)
        sess = await ctx.new_page()

        async def slow(route):
            if route.request.method == "POST":
                await asyncio.sleep(2.5)
            await route.continue_()
        await sess.route("**/api/t/session/*/status", slow)
        await sess.goto(f"{B}/t/session/{sid}")
        await sess.wait_for_selector("#rows tr")
        await sess.fill("#mid", "A047")
        await sess.wait_for_function("document.getElementById('mcands').innerText.includes('9994A047')")
        await sess.locator("#mcands button:has-text('出席')").click()
        await asyncio.sleep(0.6)
        await sess.click("#mid")
        await sess.keyboard.press("Meta+A" if sys_is_mac() else "Control+A")
        await sess.keyboard.type("A04", delay=120)
        mid_before = await sess.input_value("#mid")
        await asyncio.sleep(2.6)  # 等 2.5 秒的延遲回應真的到了
        mid_after = await sess.input_value("#mid")
        focused = await sess.evaluate("document.activeElement && document.activeElement.id")
        check(mid_before == "A04", f"前提：老師在等待期間已經打了下一位，實得 {mid_before!r}")
        check(mid_after == "A04", f"點 3：慢到的成功回應不能清掉 #mid，實得 {mid_after!r}")
        check(focused == "mid", f"點 3：焦點仍在 #mid，實得 {focused!r}")
        await chrome.close()


def sys_is_mac():
    return sys.platform == "darwin"


asyncio.run(scenario_reconcile())
asyncio.run(scenario_dropdown_clears())
asyncio.run(scenario_slow_success_preserves_mid())
print("\n==>", "全部通過" if not FAIL else f"{len(FAIL)} 項失敗：{FAIL}")
sys.exit(1 if FAIL else 0)
