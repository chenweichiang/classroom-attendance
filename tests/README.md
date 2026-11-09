# attend 測試

這套測試是在一次失敗之後重整的。當時的測試鏈（分支覆蓋 97%、情境 50 項、模糊八千例、端對端、壓測）全綠，第一次進教室試用，連上的 9 支手機卻只有 6 支簽到成功。伺服器日誌重建出來的漏斗是：17 次掃碼開到頁面，其中 5 次（29%）一開到 QR 就已經過期，另有 2 支手機開到頁面卻沒送出，這些在原本的測試裡都看不到。

所以成效指標改看教室彩排：`tests/rehearsal.py --profile field` 每週出席比例 ≥ 95%、成功者耗時 p90 ≤ 60 秒。覆蓋率只是基本門檻。

## 一鍵

```bash
tests/gate.sh --quick                # 改完程式至少跑這個
tests/gate.sh --full                 # 動到時間、流程、前端，或部署前
tests/gate.sh --only unit,state      # 只跑指定層
tests/mutation.sh "app.x_do_checkin*"    # 對動到的函式跑突變測試（慢，不進 gate）
```

每層的完整輸出在 `/tmp/attend-gate/<層名>.log`，終端機只印一行結果，失敗才印該 log 最後 15 行，結尾印總表。伺服器埠號由腳本自己找空的，不會跟本機其他服務搶 8765。

安裝（與根目錄 README 的「測試」一節相同，先備條件也寫在那裡：uv、curl，`--full` 另需 sqlite3 指令列工具）：

```bash
uv venv .venv && uv pip install -p .venv/bin/python -r requirements.txt -r tests/requirements-dev.lock
.venv/bin/python -m playwright install --with-deps chromium webkit
```

裝的是 `tests/requirements-dev.lock` 的鎖定版本；`tests/requirements-dev.txt` 是產生 lock 的輸入，只列套件名、不鎖版本，直接拿來裝會拿到當天最新版，與 CI 不一致。工具設定集中在 `pyproject.toml`。

## CI

`.github/workflows/test.yml` 在每個 pull request 與每次 push 到 `main` 時，於 GitHub 的 `ubuntu-24.04`、Python 3.12 上跑 `tests/gate.sh --quick`，失敗時把 `/tmp/attend-gate/*.log` 上傳成 artifact。quick 九層全跑，不排除任何測試；full 多的四層（`realtime`、`load`、`rehearsal`、`flaky`）依賴實際時鐘或機器負載，共用 runner 上結果不穩定，不進 CI。

## 每一層在抓什麼

| 層 | 工具 | 抓什麼 | 抓不到什麼 |
|---|---|---|---|
| `static` | ruff（只開會是 bug 的規則）、pyright、vulture、djlint | 重複字典鍵、沒用到的變數、危險寫法、模板標記錯 | 邏輯對不對 |
| `unit` | pytest、pytest-cov（分支 97%）、pytest-randomly、Hypothesis | 每個端點在 open／late／closed 的行為與錯誤路徑；隨機順序抓測試之間的相依。這一層跑的是 `test_unit.py`、`test_api.py`、`test_friction.py`、`test_teacher_friction.py`、`test_log_funnel.py`、`test_session_report.py`、`test_harness.py`、`test_manage.py`、`test_fake_names.py`、`test_api_extra.py`、`test_closed_session.py`、`test_rehearsal_model.py`、`test_version.py`，覆蓋率合在一起量 | 覆蓋到不等於驗到（看 `mutation`） |
| `state` | Hypothesis `RuleBasedStateMachine`＋time-machine | 隨機操作序列下的七條不變式：每人每場一列、state／報表／`/me`／資料庫四方一致、活性（條件都對就一定簽得到）、安全性（結束後不再變、老師手動的不被學生端改掉）、借手機不得直接出席、無 5xx、PIN 鎖定只鎖本人。`--full` 跑 300 例 | 前端 |
| `friction` 系列 | `test_friction.py`、`test_teacher_friction.py`（在 `unit` 層）、`e2e_friction_playwright.py`、`e2e_teacher_friction_playwright.py` | 實際上課遇到的每個摩擦點各一條回歸測試：學生端（換瀏覽器改綁、PIN 計次、倒數、訊息文案）與老師端（取消結束、延長、手動簽到、Discord 暫緩與去重、卡住名單）；含「拿掉新瀏覽器 API 再跑一遍」 | |
| `scenario` | 自寫腳本 | 三門課交錯、跨課同手機、多週、延長、解綁重綁 | |
| `fuzz` | Schemathesis 4.x | 八項必過 checks（quick 每端點 50 例約 1 分鐘，full 500 例）；另四項因 OpenAPI 規格沒逐一宣告而誤報，只在 full 跑，只警告不算失敗 | 業務邏輯 |
| `e2e`、`e2e-edge` | Playwright（Chromium 老師、WebKit iPhone 學生） | 主流程與 23 項異常路徑，一次一支手機、掃到就送出 | 30 支手機同時、真實延遲 |
| `realtime`（full） | requests，真實時鐘 | QR 時效、進遲到、延長、自動結案的實際秒數 | |
| `load`（full） | Locust 47 人 | 5xx 與延遲 | 人的行為 |
| `rehearsal`（full） | Playwright 多 context＋實地延遲模型＋行為模型 | 成效指標：全班首次綁定與回簽的漏斗、過期比例、錯誤是否被鍵盤蓋住、耗時分佈；換瀏覽器與忘 PIN 的學生有沒有自己或在老師協助下完成；老師端狀態與學生端結果對帳。跑兩組亂數種子，任一組沒過就算失敗 | 相機掃不掃得到投影幕（軟體模擬不了）；實地另有 22% 開出簽到頁後沒有送出、成因未知 |
| `flaky`（full） | pytest-repeat | `test_api.py` 與 `test_teacher_friction.py` 重跑五次抓不穩定（偶發的來源是結束點名時發 Discord 的背景執行緒） | |
| `mutation`（另跑） | mutmut 3.8 | 把程式逐一改壞，測試沒轉紅＝那個行為沒人在驗。命名＝`app.x_<函式名>__mutmut_<序號>`，`mutmut show <名稱>` 看改了哪裡。原始碼就地還原，連續跑多個函式結果才可信 | 慢，只跑動到的函式。mutmut 3 跳過帶裝飾器的函式，36 個路由函式都不會被突變，所以簽到主邏輯抽成 `do_checkin`、取消結束抽成 `do_reopen_session`，路由只留薄殼；其他路由要進突變測試也得照做 |

## 彩排的延遲模型

數字取自一次實地點名的伺服器日誌，只在有新的實地資料時調整：掃到→瀏覽器送出請求＝對數常態、中位數 7 秒、sigma 0.6（校準時模擬出的「開頁時已過期」比例 23–33%，實地 29%；QR 容許時間改過之後要重新比對）；首次綁定從開頁到送出中位數 37 秒；錯誤訊息被鍵盤蓋住時學生看不到，會連按再花 20–45 秒才發現。`--app-dir` 可以對任一版本的程式跑，拿來比較修改前後。

行為模型：`--profile field` 另外模擬實地看到的另一種失敗。每週有一定比例的學生是新的瀏覽器環境（iPhone 從控制中心掃碼器或私密分頁開，每次都是新 cookie），其中一部分會先在首綁表單設一組新 PIN，再被告知要輸原本那組；又有一部分真的忘了舊 PIN，亂猜到被鎖。模擬的老師每隔幾秒看一次卡住名單，對忘 PIN 的人選「重設 PIN」或手動記出席。指標分四類印出（自己完成、老師重設後完成、老師手動記、缺席），閘門是出席總比例 ≥ 95%、自己完成者 p90 ≤ 60 秒、老師協助人數不超過模擬的忘 PIN 人數、老師端狀態與學生端結果對帳一致，兩組種子都要過。比例是暫定值（寫在 `rehearsal.py` 的常數，附註了出處），量到更多實地數字再換，不准為了過關調。`--same-network` 讓全班同一個出口 IP、同一種瀏覽器字串（指紋比對的最壞情況），只印轉待確認人數與老師要當場確認的人數，不進閘門。

下一次真實點名之後，用 `tools/log_funnel.py` 重建漏斗、`tools/session_report.py` 看每個人卡在哪一步，和彩排的預測對照；落差大就回頭校正模型。

## 慣例

- 每個修正附一條回歸測試，先看到它由紅轉綠才算修好。
- 不靠刪斷言、加 skip 或調降門檻讓測試轉綠。
- 測試裡不用固定 sleep 猜時間（`realtime` 與 `rehearsal` 是在測計時本身，例外）；時間邏輯用 time-machine。
- 速率限制存在行程內記憶體，uvicorn 必須單 worker（Dockerfile 已是）。
- 在正式站壓測要另建一門 LOADTEST 課、測完刪掉；讀正式站日誌時用時間窗把壓測切開。
- 測試資料一律用虛構學號（`9994A…`、`999…`）與虛構姓名，不放真實名冊。測試素材裡出現的中文全名只准是 `tests/FAKE_NAMES.txt` 裡的名字，或「測試＋數字」這種佔位名；`test_fake_names.py` 會掃 `tests/` 底下的檔案（含 `gate.sh` 生成的名冊），不在清單裡就紅。要加新的虛構名字，先加進清單。
- 每層各自用 `/tmp/attend-gate/` 底下自己的資料庫，不寫固定的 `/tmp` 檔名，幾層平行跑不會互踩。
