"""실데이터 통합 검사: POLICY_TEST_DATA=<CSV폴더> python -m unittest discover -s tests -v"""
import os
import unittest
from pathlib import Path

from order_policy import compile_policy
from order_policy.engine_adapter import (load_engine, catalog_from_dataset, scope_dataset,
    engine_params, buffered_plan, check_operational, evaluate_with_policy)


@unittest.skipUnless(os.getenv("POLICY_TEST_DATA"), "POLICY_TEST_DATA 미지정: 실데이터 통합 검사 생략")
class EngineAdapterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import pandas as pd
        cls.pd = pd
        root=Path(__file__).resolve().parents[1]
        load_engine(os.getenv("POLICY_TEST_ENGINE", str(root)))
        from src.loader import load_all
        from src.engine import run_engine
        from src.engine.requirement import backtest_ingredient_arrays
        from src.engine.evaluate import historical_scenarios
        from src import prep
        full = load_all(os.environ["POLICY_TEST_DATA"])
        cls.catalog = catalog_from_dataset(full)
        cls.policy = compile_policy("안전하게", "S03만, 10% 여유", catalog=cls.catalog)
        cls.ds=scope_dataset(full,cls.policy)
        cls.out=run_engine(cls.ds,engine_params(cls.policy,asof_date="2026-09-23"))
        cls.plan=buffered_plan(cls.out,cls.policy)
        cls.daily=prep.daily_sales(cls.ds)
        bt=backtest_ingredient_arrays(cls.ds,cls.daily,cls.out.params)
        cls.scenarios=historical_scenarios(bt,cls.out.params.horizon_days,max_scenarios=30)

    def plan_for(self,iid="I016",qty=1,**extra):
        return self.pd.DataFrame([dict(store_id="S03",ingredient_id=iid,order_qty=qty,**extra)])

    def test_full_real_engine_evaluation(self):
        report,ev=evaluate_with_policy(self.ds,self.out,self.plan,self.policy,scenarios=self.scenarios,daily=self.daily)
        self.assertTrue(report["feasible"],report["violations"])
        self.assertEqual(len(ev.scenarios),30)
        self.assertEqual(set(ev.lines.store_id),{"S03"})

    def test_budget_blocks_real_plan(self):
        p=compile_policy("안전하게", "S03만, 20만원 미만, 10% 여유",catalog=self.catalog)
        report,_=evaluate_with_policy(self.ds,self.out,self.plan,p,scenarios=self.scenarios,daily=self.daily)
        self.assertFalse(report["feasible"])
        self.assertTrue(any("예산" in x for x in report["violations"]))

    def test_order_unit_is_not_use_unit(self):
        n,v=check_operational(self.ds,self.policy,self.plan_for(qty=2),self.out.params)
        self.assertEqual(n.use_qty.iloc[0],2000.)
        self.assertEqual(n.use_qty_per_order_unit.iloc[0],1000.)
        self.assertTrue(any("최소금액" in x for x in v))

    def test_fractional_quantity_rejected_before_engine_truncation(self):
        n,v=check_operational(self.ds,self.policy,self.plan_for(qty=1.5),self.out.params)
        self.assertIsNone(n)
        self.assertTrue(v)

    def test_unknown_id_and_forbidden_item(self):
        n,v=check_operational(self.ds,self.policy,self.plan_for(iid="I999"),self.out.params)
        self.assertIsNone(n)
        p=compile_policy("", "우유 제외",catalog=self.catalog)
        _,v=check_operational(self.ds,p,self.plan_for(),self.out.params)
        self.assertIn("금지 식자재 포함",v)

    def test_emergency_and_calendar_cannot_bypass(self):
        _,v=check_operational(self.ds,self.policy,self.plan_for(order_type="긴급"),self.out.params)
        self.assertIn("긴급발주 금지",v)
        _,v=check_operational(self.ds,self.policy,self.plan_for(arrival_date="2026-09-24"),self.out.params)
        self.assertTrue(any("의사결정일" in x or "일정" in x for x in v))

    def test_buffer_does_not_order_existing_surplus(self):
        from dataclasses import replace
        lines=self.out.orders.lines.copy()
        lines["available"]=1e12
        fake=replace(self.out, orders=replace(self.out.orders,lines=lines))
        self.assertTrue(buffered_plan(fake,self.policy).empty)


if __name__ == "__main__":
    unittest.main()
