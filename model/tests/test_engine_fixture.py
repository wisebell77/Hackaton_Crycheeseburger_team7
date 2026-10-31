"""
엔진(src/engine) 회귀 테스트 — fixture 기반.
    python -m pytest tests/test_engine_fixture.py -q

확인하는 것
1. 엔진이 fixture 에서 오류 없이 돈다 / ds 원본을 바꾸지 않는다
2. 예측은 음수가 없고 미취급(매장×메뉴×날짜)이면 0
3. FEFO 출발점: 기준일 로트 잔량 합 = 10_inventory_daily.closing_qty
4. 발주 수량은 정수, 0 보다 크면 최소발주수량 이상, 부족분을 덮음
5. 서비스 레벨을 올리면 어떤 라인의 발주량도 줄지 않는다
6. 수요 배율 1.3 이면 1.0 보다 총 소요가 크다
7. 설명(explain) dict 에 필요한 키가 모두 있다, 최소발주금액 상태가 계산된다
8. 알림 표 스키마와 세 유형(품절위험·사용기한임박·과잉), 대안 3행
"""
from __future__ import annotations

import shutil
import sys
import uuid
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
for p in (PROJECT_ROOT, PROJECT_ROOT / "tests"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import make_fixture  # noqa: E402
from src.engine import EngineParams, briefing_template, run_engine, summary_json  # noqa: E402
from src.engine.alerts import ALERT_COLS, ALERT_TYPES  # noqa: E402
from src.engine.alternatives import ALT_COLS, compare_alternatives  # noqa: E402
from src.engine.order import EXPLAIN_KEYS, evaluate_order_qty, format_explain, po_status, z_value  # noqa: E402
from src.loader import load_all  # noqa: E402

TMP_ROOT = PROJECT_ROOT / "data" / "_tmp_engine_tests"


@pytest.fixture(scope="module")
def ds():
    d = TMP_ROOT / f"fixture_{uuid.uuid4().hex[:6]}"
    make_fixture.generate(d, seed=7)
    yield load_all(d)
    shutil.rmtree(TMP_ROOT, ignore_errors=True)


@pytest.fixture(scope="module")
def out(ds):
    return run_engine(ds, EngineParams())


@pytest.fixture(scope="module")
def out_delay(ds):
    """입고 지연 2일: 도착 전 품절위험(높음)이 생기는 시나리오."""
    return run_engine(ds, EngineParams(delay_buffer_days=2))


def test_engine_runs(out):
    assert len(out.forecast) > 0 and len(out.requirement) > 0
    assert len(out.orders.lines) > 0 and len(out.orders.pos) > 0
    assert out.asof == pd.Timestamp("2026-09-30")
    assert out.decision_datetime == pd.Timestamp("2026-10-01 09:00")
    assert len(out.assumptions) >= 5


def test_dataset_not_mutated(ds):
    before = {k: df.copy(deep=True) for k, df in ds.tables.items() if df is not None}
    run_engine(ds, EngineParams(service_level=0.9, demand_multiplier=1.1))
    for k, df in before.items():
        pd.testing.assert_frame_equal(df, ds.tables[k], check_like=False)


def test_forecast_nonnegative_and_zero_when_unavailable(out):
    fc = out.forecast
    assert (fc["forecast_qty"] >= 0).all()
    off = fc[fc["is_available"] == 0]
    assert len(off) > 0, "fixture 에는 S02 M04 미취급 기간이 있어야 함"
    assert (off["forecast_qty"] == 0).all()
    assert set(off["store_id"] + "|" + off["menu_id"]) >= {"S02|M04"}


def test_basis_keeps_explanation_terms(out):
    b = out.basis
    for col in ("base_values", "base_by_weekday", "trend_factor", "holiday_factor", "promo_factor", "trend_note",
                "holiday_note", "promo_note", "sigma"):
        assert col in b.columns, col
    row = b[(b["store_id"] == "S01") & (b["menu_id"] == "M01")].iloc[0]
    assert len(row["base_values"]) > 0
    assert 0.7 <= row["trend_factor"] <= 1.3


def test_fefo_start_equals_closing(out):
    s = out.projection.summary.dropna(subset=["data_closing_qty"])
    assert len(s) > 0
    assert np.allclose(s["on_hand_asof"].to_numpy(), s["data_closing_qty"].to_numpy(), atol=1e-6)


def test_projection_identity(out):
    d = out.projection.daily
    lhs = d["on_hand_start"] + d["received"] - d["expired_waste"] - d["consumed"]
    assert np.allclose(lhs, d["on_hand_end"], atol=1e-6)
    assert np.allclose(d["consumed"] + d["shortage"], d["requirement"], atol=1e-6)


def test_order_quantities(out):
    L = out.orders.lines
    assert pd.api.types.is_integer_dtype(L["order_qty"])
    pos = L[L["order_qty"] > 0]
    assert len(pos) > 0
    assert (pos["order_qty"] >= pos["min_order_qty"].fillna(0)).all()
    assert np.allclose(L["order_use_qty"], L["order_qty"] * L["use_qty_per_order_unit"])
    assert (L["order_use_qty"] + 1e-6 >= L["net_need_use"]).all()
    assert (L.loc[L["net_need_use"] <= 0, "order_qty"] == 0).all()


def test_service_level_monotonic(ds):
    key = ["store_id", "ingredient_id"]
    prev = None
    for sl in (0.80, 0.95, 0.99):
        L = run_engine(ds, EngineParams(service_level=sl)).orders.lines[key + ["order_qty"]]
        if prev is not None:
            m = prev.merge(L, on=key, suffixes=("_lo", "_hi"), validate="one_to_one")
            assert (m["order_qty_hi"] >= m["order_qty_lo"]).all(), m[m["order_qty_hi"] < m["order_qty_lo"]]
        prev = L
    assert z_value(0.99) > z_value(0.95) > z_value(0.80)


def test_demand_multiplier_increases_requirement(ds, out):
    hi = run_engine(ds, EngineParams(demand_multiplier=1.3))
    assert hi.requirement["requirement_qty"].sum() > out.requirement["requirement_qty"].sum()
    assert np.isclose(hi.forecast["forecast_qty"].sum(), 1.3 * out.forecast["forecast_qty"].sum())


def test_explain_has_all_keys(out):
    for e in out.orders.lines["explain"]:
        missing = [k for k in EXPLAIN_KEYS if k not in e]
        assert not missing, missing
    e = out.orders.lines.sort_values("amount", ascending=False).iloc[0]["explain"]
    text = "\n".join(format_explain(e))
    for word in ("커버해야 하는 기간", "예측 소요", "안전재고", "가용재고", "입고 예정", "부족분", "발주단위 환산"):
        assert word in text, word


def test_min_order_status(out):
    pos = out.orders.pos
    assert set(pos["status"]) <= {"충족", "미달", "제한 없음", "발주 없음"}
    below = pos[pos["status"] == "미달"]
    assert np.allclose(below["shortfall"], below["min_order_amount"] - below["total_amount"])
    assert po_status(40000, 50000) == ("미달", 10000.0)
    assert po_status(60000, 50000) == ("충족", 0.0)
    assert po_status(60000, 0) == ("제한 없음", 0.0)
    assert po_status(0, 50000) == ("발주 없음", 0.0)
    # SP03 의 최소발주금액은 0 → 제한 없음
    assert (pos.loc[pos["supplier_id"] == "SP03", "status"].isin(["제한 없음", "발주 없음"])).all()


def test_alert_schema_and_types(out, out_delay):
    for o in (out, out_delay):
        assert list(o.alerts.columns) == ALERT_COLS
        assert set(o.alerts["type"]) <= set(ALERT_TYPES)
    assert set(out_delay.alerts["type"]) == set(ALERT_TYPES)
    hi = out_delay.alerts[(out_delay.alerts["type"] == "품절위험") & (out_delay.alerts["severity"] == "높음")]
    assert len(hi) > 0
    assert (hi["stockout_date"] < hi["arrival1"]).all()


def test_alternatives_three_rows(ds, out_delay):
    a = out_delay.alerts
    r = a[(a["type"] == "품절위험") & (a["severity"] == "높음")].iloc[0]
    t = compare_alternatives(ds, out_delay.forecast, out_delay.projection, out_delay.orders, out_delay.params,
                             r["store_id"], r["ingredient_id"])
    assert list(t.columns) == ALT_COLS
    assert list(t["대안"]) == ["정기 발주 대기", "긴급발주", "매장 간 이동"]
    assert t["가정"].str.len().gt(0).all()


def test_edit_order_quantity_reprojects(out):
    L = out.orders.lines
    r = L[L["order_qty"] > 0].iloc[0]
    zero = evaluate_order_qty(out.projection, r["explain"], 0)
    more = evaluate_order_qty(out.projection, r["explain"], int(r["order_qty"]) + 5)
    assert zero["amount"] == 0
    assert more["amount"] > float(r["amount"])
    assert more["order_use_qty"] > float(r["order_use_qty"])
    assert zero["shortage_after"] >= more["shortage_after"]


def test_briefing_and_summary(out):
    text = briefing_template(out, "S01")
    assert 3 <= text.count("다.") + text.count("요.") <= 6
    js = summary_json(out, "S01")
    assert js["기준일"] == "2026-09-30" and isinstance(js["발주서"], list)


def test_sigma_backtest_schema(out):
    """식자재 σ 표: 백테스트 방식이면 커버일수별 목록이 있고 전부 0 이상, 급변 플래그는 bool."""
    s = out.sigma
    assert {"store_id", "ingredient_id", "sigma_daily", "method"} <= set(s.columns)
    assert (s["sigma_daily"] >= 0).all()
    if (s["method"] == "backtest").any():
        bt = s[s["method"] == "backtest"]
        assert bt["sigma_by_L"].map(lambda v: isinstance(v, list) and len(v) >= 1 and all(x >= 0 for x in v)).all()
        assert bt["shift_flag"].map(lambda v: isinstance(v, (bool, np.bool_))).all()
    line = out.orders.lines.iloc[0]["explain"]
    assert line["sigma_window"] >= 0 and line["safety_stock"] >= 0


def test_trend_off_by_default(out):
    """백테스트 결과대로 추세 계수는 기본 미적용(=1.0)."""
    assert not out.params.apply_trend
    assert np.allclose(out.forecast["trend_factor"], 1.0)
    assert (out.basis["trend_note"].str.contains("미적용")).any()
