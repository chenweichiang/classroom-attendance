#!/bin/bash
# 突變測試（mutmut 3.8.0）。太慢，刻意不進 tests/gate.sh，單獨跑。
# 用法：tests/mutation.sh ["<萬用字元 pattern>" ...]
#   不帶參數＝跑全 app.py（980+ 個突變，非常慢，不建議）
#   帶 pattern＝只跑符合的突變，例：tests/mutation.sh "app.x_api_checkin*"
# 突變命名規則（2026-09-21 實測 mutmut 3.8.0，用 app.x_hash_pin* 小範圍試跑確認）：
#   app.x_<函式名>__mutmut_<序號>　（模組.x_開頭前綴＋函式名＋兩底線 mutmut＋流水號）
# 跑完印 `mutmut results` 裡「survived」（存活＝測試沒驗到那個變化，是覆蓋率缺口的訊號）的數量與清單。
set -u
cd "$(dirname "$0")/.."

rm -rf mutants .mutmut-cache

if [ $# -gt 0 ]; then
  .venv/bin/mutmut run "$@"
else
  echo "沒有帶 pattern，將跑全部 app.py 的突變（980+，會很久）。5 秒後開始，Ctrl-C 可取消。"
  sleep 5
  .venv/bin/mutmut run
fi
rc=$?

echo
echo "== mutmut results（存活的突變）=="
survived=$(.venv/bin/mutmut results 2>&1 | grep -c ': survived$')
echo "存活數：$survived"
.venv/bin/mutmut results 2>&1 | grep ': survived$' || echo "（無存活，或全部 not checked／killed）"

exit $rc
