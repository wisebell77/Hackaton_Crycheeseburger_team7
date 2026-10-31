"""LLM은 의미 추출만 담당. 병합, 범위 검사, 제약 검사, 점수는 결정론적 코드."""
from __future__ import annotations

import math
from copy import deepcopy

PRESETS = {
    "balanced": dict(service_level=.95, extra_demand_buffer_ratio=0., shortage_weight=1.,
                     waste_weight=1., tail_weight=.2, cvar_alpha=.9),
    "safe": dict(service_level=.99, extra_demand_buffer_ratio=0., shortage_weight=3.,
                 waste_weight=1., tail_weight=.4, cvar_alpha=.9),
    "lean": dict(service_level=.90, extra_demand_buffer_ratio=0., shortage_weight=1.,
                 waste_weight=3., tail_weight=.1, cvar_alpha=.9),
}
# 이 수치는 연구에서 추정한 계수가 아닌, UI에서 수정 가능한 제품 기본값이다.
RANGES = {
    "service_level": (.5, .9999), "extra_demand_buffer_ratio": (0., .5),
    "shortage_weight": (0., 10.), "waste_weight": (0., 10.),
    "tail_weight": (0., 1.), "cvar_alpha": (.5, .99),
    "demand_multiplier": (.1, 3.), "delay_buffer_days": (0, 7),
    "max_stockout_probability": (0., 1.), "max_expected_waste_won": (0., 1e12),
}
LIST_FIELDS = {"store_ids", "excluded_ingredient_ids"}
FIELDS = {"mode", "budget", "allow_emergency"} | set(RANGES) | LIST_FIELDS


def validate_value(field, value, catalog=None):
    if field not in FIELDS:
        raise ValueError(f"지원하지 않는 파라미터: {field}")
    if field == "mode":
        if value not in PRESETS:
            raise ValueError("mode는 balanced/safe/lean 중 하나")
    elif field == "budget":
        if (not isinstance(value, dict) or set(value) != {"won", "operator"}
                or value["operator"] not in {"lt", "le"}
                or type(value["won"]) not in (int, float)
                or not math.isfinite(value["won"]) or not 0 <= value["won"] <= 1e12):
            raise ValueError("예산은 {won: 0 이상의 유한 금액, operator: lt/le}")
    elif field == "allow_emergency":
        if type(value) is not bool:
            raise ValueError("allow_emergency는 bool")
    elif field in LIST_FIELDS:
        if not isinstance(value, list) or any(not isinstance(x, str) for x in value):
            raise ValueError(f"{field}는 ID 문자열 목록")
        kind = "stores" if field == "store_ids" else "ingredients"
        if catalog is None and value:
            raise ValueError(f"{field}: ID 검증용 catalog가 필요")
        unknown = set(value) - set((catalog or {}).get(kind, {}))
        if unknown:
            raise ValueError(f"미등록 {field}: {sorted(unknown)}")
        if field == "store_ids" and not value:
            raise ValueError("매장 범위가 비어 있음")
    else:
        lo, hi = RANGES[field]
        if type(value) not in (int, float) or not math.isfinite(value) or not lo <= value <= hi:
            raise ValueError(f"{field}: {lo}~{hi} 범위의 유한 숫자가 필요")
        if field == "delay_buffer_days" and int(value) != value:
            raise ValueError("지연일수는 정수")


def compile_policy(owner_text="", order_text="", *, extractor=None, catalog=None):
    """같은 필드의 명시적 주문 요청 > 기본 성향 > 시스템 기본값.

    요청에 없는 기본 예산/금지는 유지한다. 원본 텍스트 자체는 저장하지 않는다.
    조건 충돌/미해석은 needs_clarification: 계산 호출을 막는다.
    """
    if extractor is None:
        from .extract import RuleExtractor
        extractor = RuleExtractor()
    result = dict(version="1.0", mode="balanced", preferences=deepcopy(PRESETS["balanced"]),
                  scenario=dict(demand_multiplier=1., delay_buffer_days=0),
                  constraints=dict(budget=None, max_stockout_probability=None,
                                   max_expected_waste_won=None, allow_emergency=False,
                                   store_ids=None, excluded_ingredient_ids=[]),
                  audit=[], issues=[], warnings=[], status="ready")
    result["warnings"].append("서비스 수준은 안전재고 생성 계수이며, 시나리오 품절 확률 보장이 아닙니다.")
    for source, text in (("owner", owner_text), ("order", order_text)):
        if not text.strip():
            continue
        try:
            patch = extractor.extract(text, catalog=catalog)
            if not isinstance(patch, dict) or set(patch) - {"changes", "issues", "warnings"}:
                raise ValueError("추출 결과 형식 오류")
            changes = patch.get("changes", [])
            if not isinstance(changes, list):
                raise ValueError("changes는 목록이어야 함")
            result["issues"].extend(f"{source}: {s}" for s in patch.get("issues", []))
            result["warnings"].extend(str(s) for s in patch.get("warnings", []))
            seen = {}
            valid = []
            for item in changes:
                if not isinstance(item, dict) or set(item) != {"field", "value", "evidence"}:
                    raise ValueError("각 변경에는 field/value/evidence만 허용")
                field, value, evidence = item["field"], item["value"], item["evidence"]
                if not isinstance(evidence, str) or not evidence.strip() or evidence not in text:
                    raise ValueError("근거 문장은 사용자 원문에 실제로 있어야 함")
                validate_value(field, value, catalog)
                if field in seen and seen[field] != value and field != "excluded_ingredient_ids":
                    raise ValueError(f"같은 입력 안의 {field} 값이 충돌함")
                seen[field] = value
                # 명시 숫자를 임의로 바꾼 API 응답을 차단한다.
                from .extract import validate_evidence
                validate_evidence(field, value, evidence, catalog)
                valid.append(item)
            # mode 기본값을 먼저, 명시적인 세부 숫자를 나중에 적용: 출력 순서에 독립적.
            for item in sorted(valid, key=lambda x: x["field"] != "mode"):
                field, value = item["field"], deepcopy(item["value"])
                if field == "mode":
                    old = result["mode"]
                    result["mode"] = value
                    for key, val in PRESETS[value].items():
                        before = result["preferences"][key]
                        result["preferences"][key] = val
                        result["audit"].append(dict(source=source, field=key, before=before,
                                                    after=val, evidence=item["evidence"], mapping="preset-v1"))
                else:
                    group = next(g for g in ("preferences", "scenario", "constraints") if field in result[g])
                    old = result[group][field]
                    if field == "excluded_ingredient_ids":
                        value = sorted(set(old) | set(value))
                    result[group][field] = value
                result["audit"].append(dict(source=source, field=field, before=old,
                                            after=value, evidence=item["evidence"]))
        except (ValueError, TypeError, KeyError, OSError) as exc:
            result["issues"].append(f"{source}: {exc}")
    p = result["preferences"]
    if p["shortage_weight"] + p["waste_weight"] == 0:
        result["issues"].append("품절·폐기 가중치를 둘 다 0으로 둘 수 없음")
    result["status"] = "needs_clarification" if result["issues"] else "ready"
    return result


def empirical_cvar(values, alpha):
    """동일 확률 경험분포 CVaR. 경계 표본은 필요한 질량만 포함한다."""
    values = sorted(values, reverse=True)
    if not values:
        raise ValueError("CVaR 계산에는 시나리오가 필요")
    mass = len(values) * (1. - alpha)
    full = int(math.floor(mass))
    frac = mass - full
    return (sum(values[:full]) + (values[full] * frac if frac > 0 else 0.)) / mass


def assess_candidate(policy, candidate):
    """정규화·제약 검사를 마친 평가 결과를 검사/채점한다.

    candidate: name, purchase_amount, scenarios[{lost_revenue,waste_value,any_stockout}],
               operational_violations[list]. 운영 제약 확인 누락도 탈락시킨다.
    """
    violations = []
    if policy["status"] != "ready":
        violations.append("정책에 미해결 요청이 있음")
    if "operational_violations" not in candidate:
        violations.append("공급사/수량/매장 등 운영 제약 검사 결과가 없음")
    else:
        violations.extend(candidate["operational_violations"])
    purchase = candidate.get("purchase_amount")
    if type(purchase) not in (int, float) or not math.isfinite(purchase) or purchase < 0:
        violations.append("발주 금액 미확인/오류")
    c = policy["constraints"]
    if not violations and c["budget"] is not None:
        b = c["budget"]
        if purchase > b["won"] or (b["operator"] == "lt" and purchase >= b["won"]):
            violations.append(f"발주 예산 초과: {purchase:g}원, 조건 {b}")
    scenarios = candidate.get("scenarios", [])
    if not scenarios:
        violations.append("시나리오 없음: 확률/꼬리위험 평가 불가")
    p = policy["preferences"]
    losses, waste, stockouts = [], [], []
    for s in scenarios:
        try:
            l, w, so = s["lost_revenue"], s["waste_value"], s["any_stockout"]
            if any(type(v) not in (float, int) or not math.isfinite(v) or v < 0 for v in (l, w)):
                raise ValueError()
            if so not in (True, False, 0, 1):
                raise ValueError()
            losses.append(p["shortage_weight"] * l + p["waste_weight"] * w)
            waste.append(w)
            stockouts.append(int(bool(so)))
        except (KeyError, TypeError, ValueError):
            violations.append("시나리오 값 오류")
            break
    metrics, score = {}, None
    if len(losses) == len(scenarios) and losses:
        prob, ew = sum(stockouts) / len(losses), sum(waste) / len(losses)
        mean, tail = sum(losses) / len(losses), empirical_cvar(losses, p["cvar_alpha"])
        score = (1-p["tail_weight"]) * mean + p["tail_weight"] * tail
        metrics = dict(p_any_stockout=prob, expected_waste_won=ew,
                       weighted_mean_loss=mean, weighted_cvar=tail)
        if c["max_stockout_probability"] is not None and prob > c["max_stockout_probability"] + 1e-12:
            violations.append("품절 확률 상한 초과")
        if c["max_expected_waste_won"] is not None and ew > c["max_expected_waste_won"] + 1e-8:
            violations.append("기대 폐기비 상한 초과")
    return dict(name=candidate.get("name", ""), feasible=not violations, violations=violations,
                score=score if not violations else None, metrics=metrics, purchase_amount=purchase)


def rank_candidates(policy, candidates):
    reports = [assess_candidate(policy, c) for c in candidates]
    feasible = sorted((r for r in reports if r["feasible"]), key=lambda r: (r["score"], r["purchase_amount"], r["name"]))
    return dict(status="ok" if feasible else "no_feasible_candidate", best=feasible[0] if feasible else None,
                ranked=feasible, reports=reports,
                search_scope="주어진 후보 안에서 비교; 전역 최적해/실제 품절 확률 보장 아님")
