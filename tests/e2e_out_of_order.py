"""點名頁 load() 要用遞增序號擋掉過期回應。

用 async API 而不是本檔案家族其餘測試慣用的 sync API：這個情境需要「一般輪詢的回應已經由
伺服器算完、只是送達瀏覽器的時間被刻意延後」同時又要在等待期間讓另一支請求（延長／結束觸發的
load(true)）正常先送達，兩支請求必須真的並行掛著。sync API 對這個 app 的
/state 輪詢用 route.continue_()／route.fulfill(response=...) delay 手法會讓瀏覽器那個 fetch()
永遠不 resolve（Network 面板看得到 requestfinished 200，但 JS 這邊的 promise 卡住、inflight 永遠
不會變回 false）——獨立重現三次、去掉業務邏輯只留最小 route+continue 仍會卡，判斷是 sync API
這個版本對「先攔截、之後再放行」重複輪詢有 bug，不是被測程式碼的問題；async API 用同樣手法完全正常，故獨立成這支用 async API。
"""
import os, re, sys, asyncio
from playwright.async_api import async_playwright

B = os.environ.get("ATTEND_TEST_BASE", "http://127.0.0.1:64890")
FAIL = []


def check(cond, msg):
    print(("ok  " if cond else "FAIL"), msg)
    if not cond:
        FAIL.append(msg)


REC = """window.__rec=[];const t0=performance.now();
new MutationObserver(()=>{window.__rec.push([Math.round(performance.now()-t0), document.getElementById('phase').textContent, document.getElementById('timer').textContent, document.getElementById('closeBtn').disabled])})
 .observe(document.getElementById('timer'),{childList:true,characterData:true,subtree:true});
new MutationObserver(()=>{window.__rec.push([Math.round(performance.now()-t0), document.getElementById('phase').textContent, document.getElementById('timer').textContent, document.getElementById('closeBtn').disabled])})
 .observe(document.getElementById('phase'),{childList:true,characterData:true,subtree:true});"""


async def run(action):
    async with async_playwright() as p:
        chrome = await p.chromium.launch()
        ctx = await chrome.new_context(viewport={"width": 1280, "height": 900})
        tp = await ctx.new_page()
        await tp.goto(f"{B}/t/login")
        await tp.fill("input[name=password]", "pw")
        await tp.click("button:has-text('登入')")
        await tp.wait_for_url(f"{B}/t")
        html = await (await ctx.request.get(f"{B}/t")).text()
        for s in set(re.findall(r"/t/session/(\d+)", html)):
            await ctx.request.post(f"{B}/api/t/session/{s}/close")
        html = await (await ctx.request.get(f"{B}/t")).text()
        m = re.search(r"course/(\d+)/open", html)
        cid = int(m.group(1)) if m else 1
        r = await ctx.request.post(f"{B}/t/course/{cid}/open", form={"kind": "normal"})
        sid = int(re.search(r"/t/session/(\d+)", r.url).group(1))

        sess = await ctx.new_page()
        state = {"delay_next": False, "delayed": 0}
        delayed_event = asyncio.Event()

        async def handler(route):
            if state["delay_next"] and route.request.method == "GET":
                state["delay_next"] = False
                state["delayed"] += 1
                delayed_event.set()
                resp = await route.fetch()  # 伺服器此刻已經算好的（延長／結束之前的）狀態
                await asyncio.sleep(4)
                await route.fulfill(response=resp)
            else:
                await route.continue_()

        await sess.route(f"**/api/t/session/{sid}/state", handler)
        await sess.goto(f"{B}/t/session/{sid}")
        await sess.wait_for_selector("#rows tr")
        await sess.evaluate(REC)
        await sess.evaluate("window.confirm=()=>true;window.alert=(m)=>{window.__alerts=(window.__alerts||[]).concat([m])}")
        state["delay_next"] = True
        try:
            await asyncio.wait_for(delayed_event.wait(), timeout=8)
        except TimeoutError:
            pass
        await asyncio.sleep(0.3)
        if action == "extend":
            await sess.click("#extendBtn")
        else:
            await sess.click("#closeBtn")
        await asyncio.sleep(6)
        rec = await sess.evaluate("window.__rec")
        print(f"{action}：畫面變化 [ms, phase, timer, closeBtn.disabled] =", rec)
        if action == "extend":
            # 第一筆是初始值（延長之前）；延長之後（第二筆起）不該再出現延長前那個「剩 3 分」
            reverted = any("剩 3 分" in row[2] for row in rec[1:])
            check(not reverted, "一般輪詢回應延後送達時，按「延長出席窗」後 6 秒內倒數不會退回延長前的值")
            check(len(rec) >= 2 and "剩 5 分" in rec[-1][2], f"最終顯示延長後的值，實得 {rec[-1] if rec else None}")
        else:
            reverted = any(row[1] == "簽到中" for row in rec[1:])
            check(not reverted, "一般輪詢回應延後送達時，按「結束點名」後 6 秒內不會回到「簽到中」")
            check(bool(rec) and rec[-1][1] == "已結束" and rec[-1][3] is True,
                  f"最終維持已結束、結束鈕鎖住，實得 {rec[-1] if rec else None}")
        await ctx.request.post(f"{B}/api/t/session/{sid}/close")
        await chrome.close()


asyncio.run(run("extend"))
asyncio.run(run("close"))
print("\n==>", "全部通過" if not FAIL else f"{len(FAIL)} 項失敗：{FAIL}")
sys.exit(1 if FAIL else 0)
