"""후보 생성 검사. 오프라인: 증량 규칙. 실데이터: POLICY_TEST_DATA=<CSV폴더> python -m unittest discover -s tests -v"""
import os
import unittest
from pathlib import Path

import pandas as pd

from order_policy import compile_policy
from order_policy.candidates import top_up


class TopUpTests(unittest.TestCase):
    def frame(self):
        return pd.DataFrame([
            dict(ingredient_id="I001", order_qty=1, unit_price=10000., daily_units=0.5),
            dict(ingredient_id="I002", order_qty=1, unit_price=20000., daily_units=2.0),
            dict(ingredient_id="I003", order_qty=0, unit_price=5000., daily_units=9.0),   # 선정 안 됨 → 증량 대상 아님
            dict(ingredient_id="I004", order_qty=1, unit_price=30000., daily_units=1.0),
        ])

    def test_round_robin_from_highest_consumption(self):
        qty, total = top_up(self.frame(), 120000., set())
        self.assertGreaterEqual(total, 120000.)
        # 시작 60,000 → I002(+20k)=80k → I004(+30k)=110k → I001(+10k)=120k 에서 멈춤
        self.assertEqual(qty.tolist(), [2, 2, 0, 2])
        self.assertEqual(total, 120000.)

    def test_excluded_and_unselected_untouched(self):
        qty, total = top_up(self.frame(), 100000., {"I002"})
        self.assertEqual(qty["I003" == self.frame().ingredient_id].tolist(), [0])
        self.assertEqual(int(qty.iloc[1]), 1)  # 제외 품목은 그대로
        self.assertGreaterEqual(total, 100000.)

    def test_already_enough_or_nothing_selected(self):
        f = self.frame()
        qty, total = top_up(f, 50000., set())
        self.assertEqual(qty.tolist(), f.order_qty.tolist())
        f["order_qty"] = 0
        qty, total = top_up(f, 50000., set())
        self.assertEqual(qty.sum(), 0)


@unittest.skipUnless(os.getenv("POLICY_TEST_DATA"), "POLICY_TEST_DATA 미지정: 실데이터 후보 생성 검사 생략")
class CandidateGenerationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from order_policy.engine_adapter import load_engine, catalog_from_dataset, scope_dataset
        root = Path(__file__).resolve().parents[1]
        load_engine(os.getenv("POLICY_TEST_ENGINE", str(root)))
        from src.loader import load_all
        from order_policy.candidates import generate_candidates, min_order_amounts, plan_economics
        # 클래스 속성에 넣은 함수는 self 를 받는 메서드가 되므로 staticmethod 로 감싼다
        cls.generate, cls.min_amounts, cls.econ = (staticmethod(generate_candidates), staticmethod(min_order_amounts),
                                                    staticmethod(plan_economics))
        full = load_all(os.environ["POLICY_TEST_DATA"])
        cls.catalog = catalog_from_dataset(full)
        cls.policy = compile_policy("안전하게", "S03만", catalog=cls.catalog)
        cls.ds = scope_dataset(full, cls.policy)
        cls.res = generate_candidates(cls.ds, cls.policy, asof_date="2026-09-23", max_scenarios=30)

    def test_grid_and_variants_exist(self):
        names = [c["name"] for c in self.res["candidates"]]
        self.assertGreaterEqual(len(names), 4)
        self.assertIn("발주 없음", names)
        self.assertTrue(any("증량" in n for n in names), names)
        self.assertTrue(any("연기" in n for n in names), names)

    def test_topup_meets_minimum_and_postpone_drops_po(self):
        mins = self.min_amounts(self.ds)
        for c in self.res["candidates"]:
            if c["variant"] not in ("최소금액 증량", "최소금액 연기") or c["plan"].empty:
                continue
            p = self.econ(c["out"], c["plan"])
            totals = p.groupby(["store_id", "supplier_id", "order_date"])["amount"].sum()
            for (s, sup, d), v in totals.items():
                self.assertGreaterEqual(v, mins[sup] - 1e-6, (c["name"], s, sup, d, v))
            if c["variant"] == "최소금액 증량":
                self.assertFalse(any("최소금액" in x for x in c["violations"]), c["violations"])

    def test_ranking_has_feasible_best(self):
        self.assertIsNotNone(self.res["best"], self.res["table"][["후보", "불가 사유"]].to_string())
        best = next(c for c in self.res["candidates"] if c["name"] == self.res["best"])
        self.assertFalse(best["violations"])
        t = self.res["table"]
        self.assertTrue((t["실행 가능"] == True).any())  # noqa: E712
        feas = t[t["실행 가능"] == True]  # noqa: E712
        self.assertEqual(feas.iloc[0]["후보"], self.res["best"])

    def test_budget_picks_cheaper_feasible(self):
        p = compile_policy("안전하게", "S03만, 20만원 미만", catalog=self.catalog)
        res = self.generate(self.ds, p, asof_date="2026-09-23", max_scenarios=30, scenarios=self.res["scenarios"])
        if res["best"] is None:
            self.assertEqual(res["ranking"]["status"], "no_feasible_candidate")
        else:
            best = next(c for c in res["candidates"] if c["name"] == res["best"])
            self.assertLess(best["ev"].totals["purchase_amount"], 200000)


if __name__ == "__main__":
    unittest.main()
