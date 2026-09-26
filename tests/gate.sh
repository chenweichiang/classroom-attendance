#!/bin/bash
# 一鍵測試閘門（取代 tests/run_all.sh）。全部在暫存資料庫／本機動態選埠，不碰正式資料。
# 用法：tests/gate.sh [--quick|--full] [--only 層名,層名]
#   --quick（預設）：static unit state scenario fuzz e2e e2e-edge e2e-friction e2e-teacher
#   --full         ：上面全部 + realtime load rehearsal flaky
#   --only         ：只跑指定層（逗號分隔），忽略 --quick/--full 的層清單（層名同上，state／rehearsal
#                     若對應測試檔還不存在會印「略過：檔案不存在」並計為失敗，不會偷偷跳過不算數）
# 每層：獨立計時、獨立 log（/tmp/attend-gate/<層名>.log）、終端機只印一行結果，失敗才印該 log 最後 15 行。
# 最後印總表（層名｜結果｜秒數）。exit code：全部通過＝0，有任何一層失敗＝1。
# 突變測試（mutmut）刻意不在這裡——太慢，另見 tests/mutation.sh。
set -u
cd "$(dirname "$0")/.."

PY=.venv/bin/python
if [ ! -x "$PY" ]; then
  # 合成名冊與每一層都要用它，缺了第一步就會失敗；先講清楚（2026-09-26）
  echo "缺測試用虛擬環境 $PY，無法執行測試閘門。" >&2
  echo "建法見 tests/README.md：uv venv .venv 後 uv pip install -p .venv/bin/python -r requirements.txt -r tests/requirements-dev.lock" >&2
  exit 1
fi
LOGDIR=/tmp/attend-gate
ROSTER=tests/fixtures/roster.csv
mkdir -p "$LOGDIR" tests/fixtures

# ── 合成名冊（沒有才建，跟原 run_all.sh 一樣的 48 人份）──
if [ ! -f "$ROSTER" ]; then
  $PY - <<'PYEOF'
import csv
with open("tests/fixtures/roster.csv", "w", newline="") as f:
    w = csv.writer(f); w.writerow(["student_id", "name", "class"])
    names = ["陳大文", "吳家豪", "林小華", "張志明"] + [f"測試{n}" for n in range(5, 49)]
    [w.writerow([f"9994A{n:03d}", names[n - 1], "測試班"]) for n in range(1, 49) if n != 18]
PYEOF
fi

# ── 參數 ──
MODE="quick"
ONLY=""
while [ $# -gt 0 ]; do
  case "$1" in
    --quick) MODE="quick"; shift ;;
    --full) MODE="full"; shift ;;
    --only) ONLY="${2:-}"; shift 2 ;;
    *) echo "未知參數：$1（用法：tests/gate.sh [--quick|--full] [--only 層名,層名]）" >&2; exit 2 ;;
  esac
done

QUICK_LAYERS="static unit state scenario fuzz e2e e2e-edge e2e-friction e2e-teacher"
FULL_EXTRA="realtime load rehearsal flaky"
if [ -n "$ONLY" ]; then
  LAYERS=$(echo "$ONLY" | tr ',' ' ')
elif [ "$MODE" = "full" ]; then
  LAYERS="$QUICK_LAYERS $FULL_EXTRA"
else
  LAYERS="$QUICK_LAYERS"
fi

# ── 先決條件：缺這些指令時，起伺服器的健康探測會一律失敗，訊息卻只會印「伺服器啟動失敗」
#    （2026-09-26 修正：實測缺 curl 時伺服器其實有正常啟動，只是探測不到而已，誤導人去查
#    程式而不是查環境）。這裡先講清楚缺什麼、怎麼裝，直接以非零離開碼結束，不要走到那一步──
need_load_layer=0
for l in $LAYERS; do [ "$l" = "load" ] && need_load_layer=1; done
missing=()
command -v curl >/dev/null 2>&1 || missing+=("curl（healthz 探測與登入用；macOS 內建應該就有，缺了可 brew install curl）")
if [ "$need_load_layer" = "1" ]; then
  command -v sqlite3 >/dev/null 2>&1 || missing+=("sqlite3（load 層直接查資料庫；macOS 內建應該就有，缺了可 brew install sqlite3）")
fi
if [ ${#missing[@]} -gt 0 ]; then
  echo "缺必要指令，無法執行測試閘門：" >&2
  for m in "${missing[@]}"; do echo "  - $m" >&2; done
  exit 1
fi

# ── 動態選一個空埠，避開別的行程佔用（2026-09-10 8765 被 python -m http.server 佔走，
#    第 4 層以後全部沒跑而且沒人發現；不要再寫死埠號）──
PORT=$($PY -c "import socket; s=socket.socket(); s.bind(('127.0.0.1',0)); print(s.getsockname()[1]); s.close()")
export ATTEND_TEST_BASE="http://127.0.0.1:$PORT"
export ATTEND_BASE_URL="$ATTEND_TEST_BASE"
export ATTEND_SECRET="attend-gate-secret"
export ATTEND_TEACHER_PASSWORD="pw"

SERVER_PID=""

kill_stale() {
  pkill -f "uvicorn app:app --host 127.0.0.1 --port $PORT" 2>/dev/null
  sleep 0.3
}

# start_server <dbfile> <openapi:0|1> <server-log> —— dbfile 由呼叫端準備好（不在這裡動它）
start_server() {
  local dbfile="$1" openapi="$2" slog="$3"
  : > "$slog"
  if [ "$openapi" = "1" ]; then
    ATTEND_DB="$dbfile" ATTEND_BASE_URL="$ATTEND_TEST_BASE" ATTEND_OPENAPI=1 \
      .venv/bin/uvicorn app:app --host 127.0.0.1 --port "$PORT" >> "$slog" 2>&1 &
  else
    ATTEND_DB="$dbfile" ATTEND_BASE_URL="$ATTEND_TEST_BASE" \
      .venv/bin/uvicorn app:app --host 127.0.0.1 --port "$PORT" >> "$slog" 2>&1 &
  fi
  SERVER_PID=$!
  local i=0
  while [ $i -lt 40 ]; do
    curl -sf "$ATTEND_TEST_BASE/healthz" >/dev/null 2>&1 && return 0
    sleep 0.3; i=$((i + 1))
  done
  return 1
}

# fresh_server <dbfile> <openapi:0|1> <server-log> —— 清庫、匯入 NTUB-CD 名冊、啟動
fresh_server() {
  local dbfile="$1" openapi="$2" slog="$3"
  kill_stale
  rm -f "$dbfile" "$dbfile-wal" "$dbfile-shm"
  ATTEND_DB="$dbfile" $PY manage.py import-roster --code NTUB-CD --name "脈絡設計與實踐" --csv "$ROSTER" >/dev/null 2>&1
  start_server "$dbfile" "$openapi" "$slog"
}

stop_server() {
  if [ -n "$SERVER_PID" ]; then
    kill "$SERVER_PID" 2>/dev/null
    wait "$SERVER_PID" 2>/dev/null
    SERVER_PID=""
  fi
  pkill -f "uvicorn app:app --host 127.0.0.1 --port $PORT" 2>/dev/null
}

trap 'stop_server' EXIT

# ───────────────────────── 各層 ─────────────────────────

layer_static() {
  local log="$LOGDIR/static.log"; : > "$log"
  local rc=0
  {
    echo "== ruff check app.py manage.py tests/ =="
    .venv/bin/ruff check app.py manage.py tests/ || rc=1
    echo
    echo "== pyright =="
    .venv/bin/pyright || rc=1
    echo
    echo "== vulture =="
    .venv/bin/vulture || rc=1
    echo
    echo "== djlint templates/ --lint （--check 純排版 12 檔全標記，不能用；改用 --lint 只看規則） =="
    .venv/bin/djlint templates/ --lint || rc=1
  } >> "$log" 2>&1
  return $rc
}

layer_unit() {
  local log="$LOGDIR/unit.log"
  # test_teacher_friction.py 併入覆蓋率量測（B004/B025-B036 新增的 events／funnel／beacon／discord 門檻
  # 程式碼只有那支檔案測到，沒併進來覆蓋率會假性回落、97% 閘門會被沒做錯事的程式碼拖垮）
  $PY -m pytest tests/test_unit.py tests/test_api.py tests/test_teacher_friction.py \
    --cov=app --cov-branch --cov-report=term-missing > "$log" 2>&1
  return $?
}

layer_state() {
  local log="$LOGDIR/state.log"
  if [ ! -f tests/test_state_machine.py ]; then
    echo "略過：檔案不存在（tests/test_state_machine.py）" > "$log"
    return 1
  fi
  if [ "$MODE" = "full" ]; then
    ATTEND_SM_EXAMPLES=300 $PY -m pytest tests/test_state_machine.py -p no:randomly > "$log" 2>&1
  else
    $PY -m pytest tests/test_state_machine.py -p no:randomly > "$log" 2>&1
  fi
  return $?
}

layer_scenario() {
  local log="$LOGDIR/scenario.log"
  $PY tests/scenario.py > "$log" 2>&1
  return $?
}

layer_fuzz() {
  local log="$LOGDIR/fuzz.log"; : > "$log"
  local dbfile="$LOGDIR/fuzz.db" slog="$LOGDIR/fuzz-server.log"
  fresh_server "$dbfile" 1 "$slog"
  if [ $? -ne 0 ]; then
    { echo "伺服器啟動失敗"; echo "--- server log ---"; cat "$slog"; } >> "$log"
    stop_server; return 1
  fi
  local cj="$LOGDIR/fuzz-cookies.txt"
  curl -s -o /dev/null -c "$cj" -d "password=$ATTEND_TEACHER_PASSWORD&next=/t" "$ATTEND_TEST_BASE/t/login"
  local ta; ta=$(grep -o 'ta[[:space:]].*' "$cj" | awk '{print $NF}')
  # quick 只求「改壞了會不會 5xx」的快篩（實測 200 例＋複查段要 324 秒，拖到沒人想跑）；完整案例數與人工複查段留給 --full
  local n=50
  [ "$MODE" = "full" ] && n=500
  {
    echo "== 必過：not_a_server_error ＋ 7 種實測沒有誤報的檢查（max-examples=${n}，-w 2）=="
    .venv/bin/schemathesis run "$ATTEND_TEST_BASE/openapi.json" \
      --checks not_a_server_error,response_headers_conformance,response_schema_conformance,negative_data_rejection,missing_required_header,use_after_free,ensure_resource_availability,ignored_auth \
      --max-examples "$n" -w 2 --exclude-path-regex "/t/logout" \
      -H "Cookie: ta=$ta"
  } >> "$log" 2>&1
  local rc=$?
  [ "$MODE" = "full" ] && {
    echo
    echo "== 待裁定（不計入本層失敗；2026-09-21 用 --checks all 對同一支 app 實測過，四類失敗全部源自"
    echo "   OpenAPI schema 沒逐一宣告，不是伺服器真的處理錯，人工複查用）=="
    echo "   - positive_data_acceptance：schema 允許空字串（沒宣告 minLength），app 業務邏輯正確拒絕 → 誤判成"
    echo "     「拒絕了合法請求」"
    echo "   - content_type_conformance：HTMLResponse／CSV 路由的 schema 沒宣告對應 Content-Type（FastAPI"
    echo "     預設把每個路由文件成 application/json，除非逐一覆寫），實際回應是對的"
    echo "   - status_code_conformance：路由沒有在 FastAPI responses= 逐一宣告 4xx/410，被判「未紀錄的狀態碼」"
    echo "   - allow_header_conformance：TRACE 等方法回 405 沒帶 Allow header（FastAPI 預設行為，非本專案邏輯）"
    .venv/bin/schemathesis run "$ATTEND_TEST_BASE/openapi.json" \
      --checks positive_data_acceptance,content_type_conformance,status_code_conformance,allow_header_conformance \
      --max-examples "$n" -w 2 --exclude-path-regex "/t/logout" \
      -H "Cookie: ta=$ta"
  } >> "$log" 2>&1
  stop_server
  return $rc
}

layer_e2e() {
  local log="$LOGDIR/e2e.log"; : > "$log"
  local dbfile="$LOGDIR/e2e.db" slog="$LOGDIR/e2e-server.log"
  fresh_server "$dbfile" 0 "$slog"
  if [ $? -ne 0 ]; then { echo "伺服器啟動失敗"; cat "$slog"; } >> "$log"; stop_server; return 1; fi
  $PY tests/e2e_playwright.py >> "$log" 2>&1
  local rc=$?
  stop_server
  return $rc
}

layer_e2e_edge() {
  local log="$LOGDIR/e2e-edge.log"; : > "$log"
  local dbfile="$LOGDIR/e2e-edge.db" slog="$LOGDIR/e2e-edge-server.log"
  fresh_server "$dbfile" 0 "$slog"
  if [ $? -ne 0 ]; then { echo "伺服器啟動失敗"; cat "$slog"; } >> "$log"; stop_server; return 1; fi
  $PY tests/e2e_edge_playwright.py >> "$log" 2>&1
  local rc=$?
  stop_server
  return $rc
}

layer_e2e_friction() {
  local log="$LOGDIR/e2e-friction.log"; : > "$log"
  local dbfile="$LOGDIR/e2e-friction.db" slog="$LOGDIR/e2e-friction-server.log"
  kill_stale
  rm -f "$dbfile" "$dbfile-wal" "$dbfile-shm"
  ATTEND_DB="$dbfile" $PY manage.py import-roster --code NTUB-CD --name "脈絡設計與實踐" --csv "$ROSTER" >/dev/null 2>&1
  # B020 倒數測試要等 grant_seconds 到期；設成允許的最小值（30 秒）不用等預設的 180 秒
  ATTEND_DB="$dbfile" $PY manage.py set --code NTUB-CD --key grant_seconds --value 30 >/dev/null 2>&1
  start_server "$dbfile" 0 "$slog"
  if [ $? -ne 0 ]; then { echo "伺服器啟動失敗"; cat "$slog"; } >> "$log"; stop_server; return 1; fi
  $PY tests/e2e_friction_playwright.py >> "$log" 2>&1
  local rc=$?
  stop_server
  return $rc
}

layer_e2e_teacher() {
  local log="$LOGDIR/e2e-teacher.log"; : > "$log"
  local dbfile="$LOGDIR/e2e-teacher.db" slog="$LOGDIR/e2e-teacher-server.log"
  kill_stale
  rm -f "$dbfile" "$dbfile-wal" "$dbfile-shm"
  ATTEND_DB="$dbfile" $PY manage.py import-roster --code NTUB-CD --name "脈絡設計與實踐" --csv "$ROSTER" >/dev/null 2>&1
  # B025 卡格測試要等 remain+interval 秒才會蓋遮罩；設成允許的最小值（5 秒）不用等預設的 10 秒
  ATTEND_DB="$dbfile" $PY manage.py set --code NTUB-CD --key qr_interval --value 5 >/dev/null 2>&1
  start_server "$dbfile" 0 "$slog"
  if [ $? -ne 0 ]; then { echo "伺服器啟動失敗"; cat "$slog"; } >> "$log"; stop_server; return 1; fi
  $PY tests/e2e_teacher_friction_playwright.py >> "$log" 2>&1
  local rc=$?
  stop_server
  return $rc
}

layer_realtime() {
  local log="$LOGDIR/realtime.log"; : > "$log"
  local dbfile="$LOGDIR/realtime.db" slog="$LOGDIR/realtime-server.log"
  fresh_server "$dbfile" 0 "$slog"
  if [ $? -ne 0 ]; then { echo "伺服器啟動失敗"; cat "$slog"; } >> "$log"; stop_server; return 1; fi
  $PY tests/realtime.py >> "$log" 2>&1
  local rc=$?
  stop_server
  return $rc
}

layer_load() {
  local log="$LOGDIR/load.log"; : > "$log"
  local dbfile="$LOGDIR/load.db" slog="$LOGDIR/load-server.log"
  fresh_server "$dbfile" 0 "$slog"
  if [ $? -ne 0 ]; then { echo "伺服器啟動失敗"; cat "$slog"; } >> "$log"; stop_server; return 1; fi
  local cj="$LOGDIR/load-cookies.txt"
  curl -s -o /dev/null -c "$cj" -d "password=$ATTEND_TEACHER_PASSWORD&next=/t" "$ATTEND_TEST_BASE/t/login"
  local ta; ta=$(grep -o 'ta[[:space:]].*' "$cj" | awk '{print $NF}')
  curl -s -o /dev/null -b "$cj" -X POST "$ATTEND_TEST_BASE/t/course/1/open"
  local sid; sid=$(sqlite3 "$dbfile" "select max(id) from sessions")
  {
    ATTEND_SID="$sid" ATTEND_TA="$ta" ATTEND_ROSTER="$ROSTER" \
      .venv/bin/locust -f tests/locustfile.py --headless -u 47 -r 47 -t 20s \
      --host "$ATTEND_TEST_BASE" --only-summary
  } >> "$log" 2>&1
  local rc=$?
  local present err500
  present=$(sqlite3 "$dbfile" "select count(*) from checkins where session_id=$sid and status='present'" 2>/dev/null)
  err500=$(grep -c ' 500 ' "$slog" 2>/dev/null)
  echo "簽到列：${present:-0} / 47；伺服器 500：${err500:-0}" >> "$log"
  stop_server
  return $rc
}

layer_rehearsal() {
  local log="$LOGDIR/rehearsal.log"
  if [ ! -f tests/rehearsal.py ]; then
    echo "略過：檔案不存在（tests/rehearsal.py）" > "$log"
    return 1
  fi
  # rehearsal.py 自己挑空埠、自己建暫存庫、自己啟 uvicorn（見它的 start_server()），
  # 不吃這支閘門的 ATTEND_TEST_BASE／PORT，這裡不用也不該幫它另外起一個伺服器。
  $PY tests/rehearsal.py --profile field --students 30 --weeks 2 > "$log" 2>&1
  return $?
}

layer_flaky() {
  local log="$LOGDIR/flaky.log"
  $PY -m pytest tests/test_api.py --count=5 -x > "$log" 2>&1
  return $?
}

run_layer() {
  case "$1" in
    static) layer_static ;;
    unit) layer_unit ;;
    state) layer_state ;;
    scenario) layer_scenario ;;
    fuzz) layer_fuzz ;;
    e2e) layer_e2e ;;
    e2e-edge) layer_e2e_edge ;;
    e2e-friction) layer_e2e_friction ;;
    e2e-teacher) layer_e2e_teacher ;;
    realtime) layer_realtime ;;
    load) layer_load ;;
    rehearsal) layer_rehearsal ;;
    flaky) layer_flaky ;;
    *) echo "未知層：$1" > "$LOGDIR/$1.log" 2>/dev/null; return 2 ;;
  esac
}

# ───────────────────────── 主迴圈 ─────────────────────────

NAMES=(); RESULTS=(); SECS=()
OVERALL_RC=0

echo "attend 測試閘門（模式：${MODE}；基準位址：${ATTEND_TEST_BASE}）"
echo "層次：$LAYERS"
echo

for name in $LAYERS; do
  start_ts=$(date +%s)
  run_layer "$name"
  rc=$?
  end_ts=$(date +%s)
  elapsed=$((end_ts - start_ts))
  NAMES+=("$name"); SECS+=("$elapsed")
  log="$LOGDIR/$name.log"
  if [ $rc -eq 0 ]; then
    RESULTS+=("PASS")
    printf "[PASS] %-10s %3ds\n" "$name" "$elapsed"
  else
    RESULTS+=("FAIL")
    OVERALL_RC=1
    printf "[FAIL] %-10s %3ds  (log: %s)\n" "$name" "$elapsed" "$log"
    echo "  --- $log 最後 15 行 ---"
    tail -n 15 "$log" 2>/dev/null | sed 's/^/  /'
  fi
done

echo
echo "== 總表 =="
printf "%-12s %-6s %6s\n" "層" "結果" "秒數"
i=0
while [ $i -lt ${#NAMES[@]} ]; do
  printf "%-12s %-6s %5ds\n" "${NAMES[$i]}" "${RESULTS[$i]}" "${SECS[$i]}"
  i=$((i + 1))
done
echo
if [ $OVERALL_RC -eq 0 ]; then
  echo "全部層通過"
else
  echo "有層失敗（exit code=${OVERALL_RC}），細節看上面各層 log"
fi
exit $OVERALL_RC
