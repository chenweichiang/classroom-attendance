"""彩排模擬器的行為模型與指標（純函式部分，不起瀏覽器、不起伺服器）。
彩排本體（tests/rehearsal.py）跑一次要好幾分鐘，這裡守的是：實地比例有沒有落進模型、指標分類與閘門算得對。"""
import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location("rehearsal_under_test", ROOT / "tests" / "rehearsal.py")
R = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = R  # rehearsal.py 有 from __future__ import annotations＋dataclass，載入時要找得到自己的模組
SPEC.loader.exec_module(R)


def _students(n=30, seed=1, profile="field"):
    studs = R.build_students(n, seed)
    R.mark_fresh_students(studs, seed, profile)
    return studs


def test_field_model_constants_come_from_field_evidence():
    assert R.FRESH_RATIO == 0.18  # 實地三場每場 5–6／31 人改綁
    assert R.NEW_PIN_FIRST_RATIO == pytest.approx(10 / 15)  # 場次 10：新環境 15 人、pin_exists 10
    assert R.TEACHER_SECONDS_PER_STUDENT == 12  # 場次 10：2 分鐘內手動記 10 人
    for name in ("FORGET_PIN_RATIO", "TEACHER_RESET_RATIO"):
        assert hasattr(R, name), name
    src = (ROOT / "tests" / "rehearsal.py").read_text(encoding="utf-8")
    assert src.count("暫定") >= 2 and "11/11、11/12" in src, "暫定值要註明待補的實地場次"


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_field_profile_assigns_fresh_newpin_and_forgetting_students_by_the_evidence_ratios(seed):
    studs = _students(30, seed)
    fresh = [s for s in studs if s["fresh"]]
    assert len(fresh) == 5, len(fresh)  # 30 × 18% ≈ 5.4：清大每場 5–6 人
    plans = {p: [s for s in fresh if s["plan"] == p] for p in ("fresh_old_pin", "fresh_remembers", "fresh_forgets")}
    assert sum(len(v) for v in plans.values()) == 5
    assert len(plans["fresh_remembers"]) + len(plans["fresh_forgets"]) == 3  # 5 × 10/15 ≈ 3 人先設新 PIN
    assert len(plans["fresh_forgets"]) == 2  # 3 人 × 0.5，四捨五入往上
    assert all(s["plan"] == "normal" for s in studs if not s["fresh"])
    for s in fresh:
        assert s["new_pin"] != s["pin"] and len(s["wrong_pins"]) == 2 and len({s["pin"], s["new_pin"], *s["wrong_pins"]}) == 4


def test_fast_profile_has_no_fresh_or_forgetting_students():
    studs = _students(30, 1, "fast")
    assert not any(s["fresh"] for s in studs) and all(s["plan"] == "normal" for s in studs)


def test_plan_selection_is_reproducible_for_the_same_seed():
    a = [(s["student_id"], s["plan"], s["new_pin"]) for s in _students(30, 7) if s["fresh"]]
    b = [(s["student_id"], s["plan"], s["new_pin"]) for s in _students(30, 7) if s["fresh"]]
    assert a == b and a


def _res(sid, outcome, latency=None, mode="return", reason=None):
    return {"student_id": sid, "name": "測試" + sid, "device": "iphone", "outcome": outcome, "gave_up_reason": reason,
            "success_latency": latency, "landed_200": 1, "landed_410": 0, "switched_code": False, "code_attempts": 0,
            "errors": [], "covered": 0, "total_fails": 0, "mode": mode, "events": [], "fresh": False}


def _state(present=0, late=0, pending=0, absent=0, none=0):
    return {"counts": {"present": present, "late": late, "pending": pending, "absent": absent, "excused": 0, "none": none}}


def test_funnel_separates_self_reset_manual_and_absent_and_gates_on_total_attendance():
    results = [_res(f"S{i}", "present", 20 + i) for i in range(1, 25)]  # 24 人自己完成
    results += [_res("R1", "present", 150.0, mode="first"), _res("R2", "present", 160.0, mode="first")]  # 老師重設後完成
    results += [_res("M1", "gave_up", reason="teacher_manual"), _res("M2", "gave_up", reason="teacher_manual")]  # 老師手動記
    results += [_res("A1", "gave_up", reason="timeout_120s"), _res("A2", "gave_up", reason="timeout_120s")]  # 缺席
    actions = {"R1": {"action": "reset", "t": 1.0}, "R2": {"action": "reset", "t": 2.0},
               "M1": {"action": "manual", "t": 3.0}, "M2": {"action": "manual", "t": 4.0}}
    f = R.build_funnel(2, 30, results, _state(present=28, absent=2), actions=actions, forgetters=4)
    assert (f["self_done"], f["reset_done"], f["manual"], f["absent"]) == (24, 2, 2, 2)
    assert f["assisted"] == 4 and f["forgetters"] == 4 and f["assist_ok"] is True
    assert f["present_rate"] == pytest.approx(28 / 30) and f["gate_ok"] is False  # 93.3% < 95%
    assert f["latency_p90"] <= 60 and f["gate_p90_ok"] is True, "p90 只看自己完成的人，老師重設後完成的耗時另外列"
    assert f["reset_p50"] == pytest.approx(155.0)


def test_funnel_gate_passes_when_total_attendance_is_at_least_95_percent():
    results = [_res(f"S{i}", "present", 30.0) for i in range(1, 28)] + [_res("M1", "gave_up", reason="teacher_manual"),
                                                                       _res("R1", "present", 130.0, mode="first"),
                                                                       _res("A1", "gave_up", reason="timeout_120s")]
    actions = {"M1": {"action": "manual", "t": 1.0}, "R1": {"action": "reset", "t": 2.0}}
    f = R.build_funnel(2, 30, results, _state(present=29, absent=1), actions=actions, forgetters=2)
    assert f["present_rate"] == pytest.approx(29 / 30) and f["gate_ok"] is True and f["reconcile_ok"] is True


def test_assisted_students_more_than_simulated_forgetters_fails_the_gate():
    """若 PIN 欄不清空，或「任何一次錯 PIN 就鎖」，記得 PIN 的人也會卡住，老師被迫處理的人數會超過忘 PIN 的人數。"""
    results = [_res(f"S{i}", "present", 30.0) for i in range(1, 30)] + [_res("M1", "gave_up", reason="teacher_manual")]
    results[0] = _res("M0", "gave_up", reason="teacher_manual")
    actions = {"M0": {"action": "manual", "t": 1.0}, "M1": {"action": "manual", "t": 2.0}}
    f = R.build_funnel(2, 30, results, _state(present=30), actions=actions, forgetters=1)
    assert f["present_rate"] == 1.0 and f["assisted"] == 2 and f["assist_ok"] is False and f["gate_ok"] is False


def test_self_completion_drop_is_visible_as_its_own_number():
    results = [_res(f"S{i}", "present", 30.0) for i in range(1, 11)] + [_res(f"M{i}", "gave_up", reason="teacher_manual") for i in range(1, 21)]
    actions = {f"M{i}": {"action": "manual", "t": float(i)} for i in range(1, 21)}
    f = R.build_funnel(2, 30, results, _state(present=30), actions=actions, forgetters=2)
    assert f["self_done"] == 10 and f["manual"] == 20 and f["assist_ok"] is False


def test_reconcile_compares_teacher_state_with_student_outcomes():
    results = [_res(f"S{i}", "present", 30.0) for i in range(1, 29)] + [_res("L1", "late", 200.0), _res("A1", "gave_up", reason="fails4")]
    ok = R.build_funnel(2, 30, results, _state(present=28, late=1, absent=1), actions={}, forgetters=0)
    assert ok["reconcile_ok"] is True and ok["reconcile_diff"] == {}
    bad = R.build_funnel(2, 30, results, _state(present=27, late=1, absent=2), actions={}, forgetters=0)
    assert bad["reconcile_ok"] is False and bad["reconcile_diff"]["present"] == (28, 27), bad["reconcile_diff"]
    assert bad["gate_ok"] is False, "對帳不一致要讓閘門失敗，不只是印出來"


def test_same_network_mode_makes_every_student_share_one_user_agent():
    uas = {R.same_network_user_agent(s) for s in _students(30, 1)}
    assert len(uas) == 1
    per_student = {R.student_user_agent(s, "Mozilla/5.0 X", same_network=False) for s in _students(30, 1)}
    assert len(per_student) == 30, "預設每人各自的 UA（D3 的前提）"


def test_remembering_fresh_student_who_does_not_finish_alone_fails_gate_even_above_95_percent():
    """若 PIN 欄殘留，記得舊 PIN 的新環境者每週只有約 1 人，30 人班少 1 人仍有 96.7% 出席；
    這群人單獨斷言，不然他們沒完成會被總比例吞掉。"""
    results = [_res(f"S{i}", "present", 30.0) for i in range(1, 30)] + [_res("K1", "gave_up", reason="timeout_120s")]
    results[0]["plan"] = "fresh_remembers"
    results[-1]["plan"] = "fresh_remembers"
    f = R.build_funnel(2, 30, results, _state(present=29, absent=1), actions={}, forgetters=2)
    assert f["present_rate"] == pytest.approx(29 / 30) and f["gate_p90_ok"] is True
    assert (f["remember_n"], f["remember_self"]) == (2, 1) and f["remember_ok"] is False and f["gate_ok"] is False
    results[-1] = _res("K1", "present", 40.0)
    results[-1]["plan"] = "fresh_remembers"
    ok = R.build_funnel(2, 30, results, _state(present=30), actions={}, forgetters=2)
    assert ok["remember_ok"] is True and ok["gate_ok"] is True
