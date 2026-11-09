#!/bin/bash
# 突變測試（mutmut 3.8.0）。太慢，刻意不進 tests/gate.sh，單獨跑。
# 用法：tests/mutation.sh ["<萬用字元 pattern>" ...]
#   不帶參數＝跑全 app.py（980+ 個突變，非常慢，不建議）
#   帶 pattern＝只跑符合的突變，例：tests/mutation.sh "app.x_api_checkin*"
# 突變命名規則（mutmut 3.8.0 實測，用 app.x_hash_pin* 小範圍試跑確認）：
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
# mutmut 在統計階段失敗時一個突變都沒測，results 照樣是空的，看起來像「存活 0」。
# 結束碼不是 0、或沒有任何突變被測過，就直接報錯，不印存活數。
checked=$(.venv/bin/mutmut results --all true 2>&1 | grep -E '^ *app\.x_[^ ]+: ' | grep -vc ': not checked$' || true)
if [ "$rc" -ne 0 ] || [ "$checked" -eq 0 ]; then
  echo "!! mutmut 沒有跑完（結束碼 ${rc}、被測過的突變 ${checked} 個），存活數不可信；看上面的輸出找原因" >&2
  exit 1
fi
echo "被測過的突變：${checked} 個"
echo "== mutmut results（存活的突變）=="
survived=$(.venv/bin/mutmut results 2>&1 | grep -c ': survived$')
echo "存活數：$survived"
.venv/bin/mutmut results 2>&1 | grep ': survived$' || echo "（無存活，或全部 not checked／killed）"
# 外殼壞掉時每個子行程都 setup error、全部記成被殺，結果就是「測了很多、存活 0」——SQL 改大小寫這類
# 一定存活的等價突變也被殺。真的全殺幾乎不可能，測過的夠多卻一個都沒活，先當外殼壞掉處理，不要當好消息
if [ "$survived" -eq 0 ] && [ "$checked" -ge 100 ]; then
  echo "!! 被測過 ${checked} 個卻存活 0：先懷疑測試外殼。在暫存副本把一句 SQL 的關鍵字改小寫（一定等價）跑同一組測試，" >&2
  echo "!! 應該全過；再到暫存副本的 pyproject [tool.mutmut] 加 debug = true，只跑那個函式，看子行程輸出有沒有 setup error。有，這輪結果就不可信" >&2
fi

exit $rc
