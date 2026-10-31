import importlib.util
import json
import unittest
from types import SimpleNamespace

from order_policy import compile_policy, assess_candidate, rank_candidates, CompatibleAPIExtractor, ClaudeExtractor
from order_policy.claude_extract import output_schema
from order_policy.extract import money_won
from order_policy.policy import empirical_cvar

CAT = dict(stores={"S01":"가상매장 A", "S03":"가상매장 C"},
           ingredients={"I016":"우유", "I009":"양상추"})


def candidate(name="a", amount=100000, lost=1000, waste=100, stockout=True):
    return dict(name=name, purchase_amount=amount, operational_violations=[],
                scenarios=[dict(lost_revenue=lost, waste_value=waste, any_stockout=stockout)] * 10)


class PolicyTests(unittest.TestCase):
    def test_two_inputs_and_scope(self):
        p = compile_policy("안전하게 주문하자, 30만원 이하", "이번에는 20만원 미만, S03만, 우유 발주 제외, 10% 여유", catalog=CAT)
        self.assertEqual(p["status"], "ready", p["issues"])
        self.assertEqual(p["preferences"]["service_level"], .99)
        self.assertEqual(p["constraints"]["budget"], dict(won=200000, operator="lt"))
        self.assertEqual(p["constraints"]["store_ids"], ["S03"])
        self.assertEqual(p["constraints"]["excluded_ingredient_ids"], ["I016"])
        self.assertEqual(p["preferences"]["extra_demand_buffer_ratio"], .1)
        self.assertEqual(p["scenario"]["demand_multiplier"], 1.)

    def test_request_mode_replaces_profile_but_preserves_budget(self):
        p = compile_policy("안전하게, 20만원 이하", "이번은 폐기를 줄여줘")
        self.assertEqual(p["mode"], "lean")
        self.assertEqual(p["constraints"]["budget"]["won"], 200000)

    def test_safety_is_not_demand(self):
        p = compile_policy("주문량이 많아도 감당 되게 하고싶어", "수요가 20% 증가")
        self.assertEqual(p["status"], "ready", p["issues"])
        self.assertAlmostEqual(p["scenario"]["demand_multiplier"], 1.2)

    def test_strict_and_inclusive_budget(self):
        for word, feasible in (("미만",False), ("언더",False), ("이하",True), ("이내",True)):
            p = compile_policy("", f"20만원 {word}")
            self.assertEqual(assess_candidate(p, candidate(amount=200000))["feasible"], feasible)

    def test_money(self):
        for text, amount in (("200,000원",200000),("20 만원",200000),("1.5만원",15000),("5천원",5000)):
            self.assertEqual(money_won(text), amount)

    def test_probability_and_waste_limits(self):
        p = compile_policy("", "품절 확률 10% 이하, 폐기비 5천원 이하")
        self.assertEqual(p["status"], "ready", p["issues"])
        self.assertFalse(assess_candidate(p,candidate())["feasible"])
        self.assertTrue(assess_candidate(p,candidate(stockout=False))["feasible"])

    def test_same_input_conflict_blocks(self):
        p = compile_policy("", "20만원 이하, 30만원 이하")
        self.assertEqual(p["status"], "needs_clarification")

    def test_unparsed_numeric_not_dropped(self):
        for text in ("안전하게 주문하고 우유 2팩", "내일만 수요 20% 증가", "안전하게, 예산 20만원 이상"):
            self.assertEqual(compile_policy("",text)["status"],"needs_clarification", text)

    def test_unknown_name_not_guessed(self):
        self.assertEqual(compile_policy("", "두유 발주 제외",catalog=CAT)["status"], "needs_clarification")

    def test_negation_not_reversed(self):
        self.assertEqual(compile_policy("", "안전하게 하지 마")["status"],"needs_clarification")
        p = compile_policy("긴급발주 허용", "긴급발주 금지")
        self.assertFalse(p["constraints"]["allow_emergency"])

    def test_cvar_fractional_tail(self):
        self.assertAlmostEqual(empirical_cvar([0.,10.,20.], .5), 50/3)
        self.assertEqual(empirical_cvar([0.,10.,20.], .9),20.)

    def test_no_feasible_does_not_relax(self):
        p = compile_policy("", "1천원 이하")
        out = rank_candidates(p,[candidate()])
        self.assertIsNone(out["best"])
        self.assertEqual(out["status"],"no_feasible_candidate")

    def test_weight_changes_ranking(self):
        a = candidate("품절억제", lost=0,waste=1000)
        b = candidate("폐기억제", lost=1000,waste=0)
        self.assertEqual(rank_candidates(compile_policy("안전하게"),[a,b])["best"]["name"], "품절억제")
        self.assertEqual(rank_candidates(compile_policy("폐기 최소화"),[a,b])["best"]["name"], "폐기억제")

    def test_invalid_scenarios_and_missing_checks(self):
        p=compile_policy()
        a=candidate();a["scenarios"]=[]
        self.assertFalse(assess_candidate(p,a)["feasible"])
        a=candidate();del a["operational_violations"]
        self.assertFalse(assess_candidate(p,a)["feasible"])
        a=candidate(amount=float("nan"))
        self.assertFalse(assess_candidate(p,a)["feasible"])

    def test_api_is_injectable_and_no_hallucinated_budget(self):
        import json
        def fake(payload):
            result=dict(changes=[dict(field="budget", value=dict(won=2000000,operator="le"),evidence="20만원 이하")],issues=[],warnings=[])
            return {"choices":[{"message":{"content":json.dumps(result)}}]}
        p=compile_policy("", "20만원 이하",extractor=CompatibleAPIExtractor(transport=fake))
        self.assertEqual(p["status"],"needs_clarification")

    def test_api_failure_does_not_silently_fallback(self):
        ex=CompatibleAPIExtractor(transport=lambda p: {})
        self.assertEqual(compile_policy("안전하게",extractor=ex)["status"],"needs_clarification")

    def test_explicit_service_level_and_delay(self):
        p=compile_policy("안전하게", "서비스 수준 97%, 배송 2일 지연")
        self.assertEqual(p["status"],"ready",p["issues"])
        self.assertEqual(p["preferences"]["service_level"],.97)
        self.assertEqual(p["scenario"]["delay_buffer_days"],2)

    def test_exclusions_accumulate_across_clauses_and_inputs(self):
        p=compile_policy("우유 제외", "양상추 제외, 우유 제외",catalog=CAT)
        self.assertEqual(p["status"],"ready",p["issues"])
        self.assertEqual(p["constraints"]["excluded_ingredient_ids"],["I009","I016"])

    def test_reversed_api_demand_direction_blocked(self):
        import json
        def fake(payload):
            result=dict(changes=[dict(field="demand_multiplier",value=1.2,evidence="수요 20% 감소")],issues=[],warnings=[])
            return {"choices":[{"message":{"content":json.dumps(result)}}]}
        p=compile_policy("", "수요 20% 감소",extractor=CompatibleAPIExtractor(transport=fake))
        self.assertEqual(p["status"],"needs_clarification")


class FakeClaude:
    """beta.messages.create 인자를 기록하고 정해진 응답을 돌려준다."""
    def __init__(self, result, stop_reason="end_turn"):
        self.calls=[]
        blocks=[SimpleNamespace(type="thinking", thinking=""), SimpleNamespace(type="text", text=json.dumps(result, ensure_ascii=False))]
        self.response=SimpleNamespace(stop_reason=stop_reason, content=blocks)
        self.beta=SimpleNamespace(messages=SimpleNamespace(create=self.create))

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self.response


class ClaudeExtractorTests(unittest.TestCase):
    def test_schema_covers_every_field(self):
        from order_policy.policy import FIELDS
        variants=output_schema()["properties"]["changes"]["items"]["anyOf"]
        self.assertEqual(sorted(f for v in variants for f in v["properties"]["field"]["enum"]), sorted(FIELDS))

    @unittest.skipUnless(importlib.util.find_spec("anthropic"), "anthropic 미설치")
    def test_request_shape_and_validation(self):
        text="안전하게, 20만원 미만, 우유는 빼줘"
        fake=FakeClaude(dict(changes=[dict(field="mode", value="safe", evidence="안전하게"),
                                      dict(field="budget", value=dict(won=200000, operator="lt"), evidence="20만원 미만"),
                                      dict(field="excluded_ingredient_ids", value=["I016"], evidence="우유는 빼줘")],
                             issues=[], warnings=[]))
        p=compile_policy("", text, catalog=CAT, extractor=ClaudeExtractor(client=fake))
        self.assertEqual(p["status"],"ready",p["issues"])
        self.assertEqual(p["mode"],"safe")
        self.assertEqual(p["constraints"]["budget"],dict(won=200000, operator="lt"))
        self.assertEqual(p["constraints"]["excluded_ingredient_ids"],["I016"])
        call=fake.calls[0]
        self.assertEqual(call["messages"],[dict(role="user", content=text)])
        self.assertIn("우유", call["system"][0]["text"])
        self.assertEqual(call["output_config"]["format"]["type"],"json_schema")
        self.assertEqual((call["betas"], call["fallbacks"]),(["server-side-fallback-2026-07-01"],"default"))

    @unittest.skipUnless(importlib.util.find_spec("anthropic"), "anthropic 미설치")
    def test_refusal_truncation_and_invented_number_block(self):
        invented=dict(changes=[dict(field="budget", value=dict(won=300000, operator="le"), evidence="20만원 이하")], issues=[], warnings=[])
        for fake in (FakeClaude({}, "refusal"), FakeClaude({}, "max_tokens"), FakeClaude(invented)):
            p=compile_policy("", "20만원 이하", extractor=ClaudeExtractor(client=fake))
            self.assertEqual(p["status"],"needs_clarification")


if __name__ == "__main__":
    unittest.main()
