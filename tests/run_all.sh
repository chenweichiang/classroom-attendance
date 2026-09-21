#!/bin/bash
# 已由 tests/gate.sh 取代（分層計時／每層獨立 log／總表／不寫死埠號）。
# 這支只做舊呼叫方式的轉譯，讓還在用舊指令的人不用改：
#   tests/run_all.sh          （舊：全部層含 realtime／壓測）→ tests/gate.sh --full
#   tests/run_all.sh --quick  （舊：略過 realtime／壓測）    → tests/gate.sh --quick
# 直接用新指令：tests/gate.sh [--quick|--full] [--only 層名,層名]
set -u
cd "$(dirname "$0")/.."
if [ "${1:-}" = "--quick" ]; then
  exec tests/gate.sh --quick
else
  exec tests/gate.sh --full
fi
