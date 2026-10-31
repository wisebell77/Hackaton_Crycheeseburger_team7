"""Real-CSV integration tests. Set PROMOTION_TEST_DATA to your private data dir."""
import copy
import json
import os
from pathlib import Path
import unittest

import numpy as np
import pandas as pd

from promotion_forecast.bootstrap import load_engine

ROOT = Path(__file__).resolve().parents[1]
load_engine(ROOT)
from src.loader import load_all
from src import prep
from src.engine.params import EngineParams
from src.engine.requirement import ingredient_requirement
from promotion_forecast import validate_plan, make_forecaster


@unittest.skipUnless(os.environ.get("PROMOTION_TEST_DATA"), "실제 CSV는 배포하지 않음; PROMOTION_TEST_DATA 필요")
class TestPromotionForecast(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ds = load_all(os.environ["PROMOTION_TEST_DATA"])
        cls.daily = prep.daily_sales(cls.ds)
        cls.template = json.loads((ROOT / "promotion_plan.example.json").read_text())

    def setUp(self):
        self.plan = copy.deepcopy(self.template)
        self.params = EngineParams(asof_date=pd.Timestamp("2026-09-30"), horizon_days=10)

    def test_only_selected_store_menu_dates_change_and_input_immutable(self):
        original = self.daily.copy(deep=True)
        fc, _ = make_forecaster(self.plan)(self.ds, self.params, daily=self.daily)
        selected = fc.store_id.eq("S02") & fc.menu_id.isin(["M004", "M005"]) & fc.date.between("2026-10-03", "2026-10-07")
        self.assertEqual(int(selected.sum()), 10)
        np.testing.assert_allclose(fc.loc[selected, "forecast_qty"], fc.loc[selected, "baseline_forecast_qty"] * 1.3)
        np.testing.assert_allclose(fc.loc[~selected, "forecast_qty"], fc.loc[~selected, "baseline_forecast_qty"])
        pd.testing.assert_frame_equal(self.daily, original)

    def test_bom_increment_equals_target_menu_increment(self):
        fc, _ = make_forecaster(self.plan)(self.ds, self.params, daily=self.daily)
        actual = ingredient_requirement(fc, self.ds)
        base = ingredient_requirement(fc.assign(forecast_qty=fc.baseline_forecast_qty), self.ds)
        increment = ingredient_requirement(fc.assign(forecast_qty=fc.promo_increment_qty), self.ds)
        np.testing.assert_allclose(actual.requirement_qty - base.requirement_qty, increment.requirement_qty, atol=1e-8)

    def test_future_observations_do_not_change_forecast(self):
        params = EngineParams(asof_date=pd.Timestamp("2026-08-15"), horizon_days=7)
        self.plan["promotions"][0].update(start_date="2026-08-16", end_date="2026-08-20")
        changed = self.daily.copy()
        changed.loc[changed.date > params.asof, "demand_qty"] = 999999
        fc, _ = make_forecaster(self.plan)(self.ds, params, daily=self.daily)
        again, _ = make_forecaster(self.plan)(self.ds, params, daily=changed)
        np.testing.assert_allclose(fc.forecast_qty, again.forecast_qty)

    def test_historical_promo_days_excluded_from_baseline(self):
        params = EngineParams(asof_date=pd.Timestamp("2026-08-05"), horizon_days=7)
        _, basis = make_forecaster({"schema_version": 1, "promotions": []})(self.ds, params, daily=self.daily)
        evidence_count = 0
        for row in basis[basis.store_id == "S02"].itertuples():
            if not isinstance(row.base_values, list):
                continue  # 미판매/이력 부족 메뉴
            for value in row.base_values:
                evidence_count += 1
                self.assertFalse(pd.Timestamp("2026-07-07") <= value["date"] <= pd.Timestamp("2026-07-31"))
        self.assertGreater(evidence_count, 0)

    def test_overlap_unknown_id_bad_discount_missing_response_rejected(self):
        cases = []
        wrong = copy.deepcopy(self.plan)
        wrong["promotions"].append({**copy.deepcopy(wrong["promotions"][0]), "promotion_id": "OTHER"})
        cases.append(wrong)
        wrong = copy.deepcopy(self.plan)
        wrong["promotions"][0]["target_menu_ids"] = ["UNKNOWN"]
        cases.append(wrong)
        wrong = copy.deepcopy(self.plan)
        wrong["promotions"][0]["offer"]["value"] = 101
        cases.append(wrong)
        wrong = copy.deepcopy(self.plan)
        del wrong["promotions"][0]["response"]
        cases.append(wrong)
        wrong = copy.deepcopy(self.plan)
        wrong["promotions"][0]["response"]["uplift_pct"] = float("nan")
        cases.append(wrong)
        for case in cases:
            with self.subTest(case=case):
                with self.assertRaises(ValueError):
                    validate_plan(case, self.ds, self.params)

    def test_no_automatic_discount_elasticity(self):
        fc, _ = make_forecaster(self.plan)(self.ds, self.params, daily=self.daily)
        self.plan["promotions"][0]["offer"]["value"] = 20
        again, _ = make_forecaster(self.plan)(self.ds, self.params, daily=self.daily)
        np.testing.assert_allclose(fc.forecast_qty, again.forecast_qty)

    def test_historical_reference_requires_acceptance_and_one_event_is_one(self):
        response = {"mode": "historical_reference", "reference_promo_id": "P01", "accept_transfer_assumption": True}
        self.plan["promotions"][0]["response"] = response
        fc, _ = make_forecaster(self.plan)(self.ds, self.params, daily=self.daily)
        event = fc[fc.promo_active.eq(1)]
        self.assertTrue(event.promo_factor.between(1.3, 1.4).all())
        self.assertTrue(event.promo_response_note.str.contains("독립 행사 1건").all())
        response["accept_transfer_assumption"] = False
        with self.assertRaises(ValueError):
            validate_plan(self.plan, self.ds, self.params)

    def test_reference_cannot_use_unfinished_event_or_other_store(self):
        self.plan["promotions"][0]["response"] = {"mode": "historical_reference", "reference_promo_id": "P01", "accept_transfer_assumption": True}
        self.plan["promotions"][0].update(start_date="2026-07-21", end_date="2026-07-25")
        with self.assertRaises(ValueError):
            validate_plan(self.plan, self.ds, EngineParams(asof_date=pd.Timestamp("2026-07-20")))
        self.plan["promotions"][0].update(start_date="2026-10-03", end_date="2026-10-07", store_ids=["S01"])
        with self.assertRaises(ValueError):
            validate_plan(self.plan, self.ds, self.params)

    def test_unavailable_menu_cannot_be_forced_on(self):
        self.plan["promotions"][0]["target_menu_ids"] = ["M015"]
        with self.assertRaises(ValueError):
            validate_plan(self.plan, self.ds, self.params)

    def test_plan_and_global_multiplier_require_explicit_compounding(self):
        params = EngineParams(asof_date=self.params.asof, demand_multiplier=1.2)
        with self.assertRaises(ValueError):
            make_forecaster(self.plan)(self.ds, params, daily=self.daily)

    def test_ongoing_legacy_event_requires_explicit_plan_not_csv_deletion(self):
        params = EngineParams(asof_date=pd.Timestamp("2026-07-15"), horizon_days=7)
        with self.assertRaises(ValueError):
            make_forecaster({"schema_version": 1, "promotions": []})(self.ds, params, daily=self.daily)
        self.plan["promotions"][0].update(start_date="2026-07-07", end_date="2026-07-31")
        fc, _ = make_forecaster(self.plan)(self.ds, params, daily=self.daily)
        mask = fc.store_id.eq("S02") & fc.menu_id.isin(["M004", "M005"])
        self.assertTrue(fc.loc[mask, "promo_factor"].eq(1.3).all())
        self.assertEqual(len(self.ds.get("08_promotions")), 1)

    def test_fixed_discount_cannot_exceed_any_target_price(self):
        self.plan["promotions"][0]["offer"] = {"type": "amount_off", "value": 13000}
        with self.assertRaises(ValueError):
            validate_plan(self.plan, self.ds, self.params)


if __name__ == "__main__":
    unittest.main()
