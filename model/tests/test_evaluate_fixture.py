"""발주안 평가 엔진 (src/engine/evaluate.py) — fixture 로 동작·일관성 확인.

1. 발주 없음 / 엔진 추천안 / 추천안×2 가 오류 없이 평가된다
2. 추천안은 발주 없음보다 기대 경로 품절이 적거나 같다
3. 발주 없음의 기대 경로 품절·폐기는 엔진 재고 전개(projection.summary)와 일치한다
4. 추천안 발주 금액 = 발주서 합계
5. 시나리오 분포: p90 ≥ 평균, 품절 확률은 0~1, 시나리오 수 ≥ 1
6. 추천안×2 는 발주 금액이 늘고 신규 로트 폐기가 줄지 않는다
7. 일별 전개 행 수 = 매장×식자재 × 기간, 재고 음수 없음
8. 발주안 정규화: 도착일·단가·금액이 채워진다 (긴급은 다음 날)
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src import prep  # noqa: E402
from src.engine import EngineParams, run_engine  # noqa: E402
from src.engine.evaluate import (compare_plans, evaluate_plan, historical_scenarios, normalize_plan,  # noqa: E402
                                 plan_from_orders, scale_plan, zero_plan)
from src.engine.requirement import backtest_ingredient_arrays  # noqa: E402
from src.loader import load_all  # noqa: E402

FIXTURE = ROOT / "data" / "fixture"


@pytest.fixture(scope="module")
def ds():
    if not FIXTURE.exists():
        import subprocess
        subprocess.run([sys.executable, str(ROOT / "tests" / "make_fixture.py")], check=True)
    return load_all(FIXTURE)


@pytest.fixture(scope="module")
def out(ds):
    return run_engine(ds, EngineParams())


@pytest.fixture(scope="module")
def scn(ds, out):
    bt = backtest_ingredient_arrays(ds, prep.daily_sales(ds), out.params)
    return historical_scenarios(bt, out.requirement["date"].nunique()) if bt is not None else None


@pytest.fixture(scope="module")
def evals(ds, out, scn):
    rec = plan_from_orders(out.orders.lines)
    plans = {"zero": zero_plan(), "rec": rec, "rec2": scale_plan(rec, 2.0)}
    return {k: evaluate_plan(ds, out.params, out.forecast, out.requirement, p, scenarios=scn, name=k,
                             lot_inputs=out.projection.lot_inputs) for k, p in plans.items()}


def test_runs_and_shapes(evals, out):
    for ev in evals.values():
        assert len(ev.lines) > 0 and len(ev.daily) > 0
        H = out.requirement["date"].nunique()
        assert len(ev.daily) == len(ev.lines) * H
        assert (ev.daily["on_hand_end"] >= -1e-9).all()
        assert ev.totals["n_scenarios"] >= 1


def test_recommendation_reduces_stockout(evals):
    z, r = evals["zero"], evals["rec"]
    assert r.totals["exp_lost_revenue"] <= z.totals["exp_lost_revenue"] + 1e-6
    assert r.lines["exp_shortage_window"].sum() <= z.lines["exp_shortage_window"].sum() + 1e-6


def test_zero_plan_matches_projection(evals, out):
    """발주 없음의 기대 경로(기간 전체, 추가 발주 없음)는 엔진 재고 전개와 같은 로트·같은 소요라 품절·폐기 합이 일치해야 한다."""
    z = evals["zero"].lines.set_index(["store_id", "ingredient_id"])
    s = out.projection.summary.set_index(["store_id", "ingredient_id"])
    common = z.index.intersection(s.index)
    assert len(common) > 0
    assert np.isclose(z.loc[common, "exp_shortage_horizon"].sum(), float(s.loc[common, "shortage_total"].sum()), rtol=1e-6, atol=1e-6)
    assert np.isclose(z.loc[common, "exp_waste_qty"].sum(), float(s.loc[common, "waste_qty"].sum()), rtol=1e-6, atol=1e-6)


def test_purchase_amount_matches_po_total(evals, out):
    rec = evals["rec"]
    assert np.isclose(rec.totals["purchase_amount"], float(out.orders.pos["total_amount"].fillna(0).sum()), rtol=1e-6, atol=1.0)


def test_distribution_sanity(evals):
    for ev in evals.values():
        t = ev.totals
        assert t["p95_loss"] >= t["p90_loss"] - 1e-6 >= -1e-6
        assert 0.0 <= t["p_any_stockout"] <= 1.0
        assert (ev.lines["loss_p90"] >= -1e-9).all() and (ev.lines["loss_mean"] >= -1e-9).all()
        assert (ev.scenarios["total_loss"] >= -1e-9).all()
        if t["n_scenarios"]:
            assert np.isclose(ev.scenarios["total_loss"].mean(), t["mean_loss"])


def test_scaled_plan_costs_more(evals):
    r, r2 = evals["rec"], evals["rec2"]
    assert r2.totals["purchase_amount"] >= r.totals["purchase_amount"]
    assert r2.lines["exp_waste_new_qty"].sum() >= r.lines["exp_waste_new_qty"].sum() - 1e-9
    assert r2.lines["exp_shortage_window"].sum() <= r.lines["exp_shortage_window"].sum() + 1e-9


def test_compare_table(evals):
    cmp = compare_plans(list(evals.values()))
    assert len(cmp) == 3 and {"발주안", "발주 금액", "기대 총손실", "p90 총손실"} <= set(cmp.columns)


def test_normalize_plan_fills_dates_and_prices(ds, out):
    ing = ds["03_ingredients"]
    iid = ing["ingredient_id"].iloc[0]
    plan = pd.DataFrame([
        {"store_id": "S01", "ingredient_id": iid, "order_qty": 2},
        {"store_id": "S01", "ingredient_id": iid, "order_qty": 1, "order_type": "긴급"},
    ])
    n = normalize_plan(ds, plan, out.params)
    assert len(n) == 2 and n["arrival_date"].notna().all() and n["unit_price"].notna().all()
    assert (n["amount"] == n["order_qty"] * n["unit_price"]).all()
    reg, emg = n.iloc[0], n.iloc[1]
    assert emg["arrival_date"] == pd.Timestamp(emg["order_date"]) + pd.Timedelta(days=1)
    assert reg["arrival_date"] >= out.params.asof + pd.Timedelta(days=1)
    assert reg["last_use_date"] > reg["arrival_date"]
