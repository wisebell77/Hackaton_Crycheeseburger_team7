"""예측 엔진 단독 실행 — 데이터 폴더(또는 zip)만 주면 예측·소요량·σ·재고 전개·발주 추천·알림을 CSV로 쓴다.

사용 예 (이 파일이 있는 폴더에서):
    python run_forecast.py --data "C:/.../크라이치즈버거_AX해커톤_주제1_데이터/data"
    python run_forecast.py --data data.zip --out my_out --asof 2026-09-30 --horizon 14 --service-level 0.95
    python run_forecast.py --data <폴더> --forecast-only      # 예측·소요량·σ 까지만 (재고 전개·발주 생략)

산출물 (--out 폴더, 기본 forecast_out/):
    forecast_menu_daily.csv        매장×메뉴×날짜 예측 (forecast_qty, 계수들, 취급 여부)
    forecast_basis.csv             예측 근거 (같은 요일 실제값, 추세·공휴일·프로모션 계수, σ)
    requirement_ingredient_daily.csv  매장×식자재×날짜 소요량 (use_unit)
    sigma_ingredient.csv           식자재 σ (안전재고 입력)
    projection_summary.csv / projection_daily.csv / expiring_lots.csv   재고 전개 결과
    orders_lines.csv / orders_pos.csv    발주 추천 (라인·발주서), explain 은 JSON 문자열
    alerts.csv                     알림
    assumptions.txt                이번 실행에 적용된 가정
    briefing.txt                   규칙 기반 브리핑 (전체·매장별)
    run_info.json                  입력 파라미터·소요 시간·로드 리포트 요약
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import pandas as pd  # noqa: E402

from src import prep  # noqa: E402
from src.engine import EngineParams, run_engine  # noqa: E402
from src.engine.forecast import forecast_menu_daily  # noqa: E402
from src.engine.params import resolve_params  # noqa: E402
from src.engine.requirement import ingredient_requirement, ingredient_sigma  # noqa: E402
from src.engine.run import briefing_template, kpis  # noqa: E402
from src.loader import load_all  # noqa: E402


def _jsonable(df: pd.DataFrame) -> pd.DataFrame:
    """dict/list 컬럼을 JSON 문자열로 바꿔 CSV 에 안전하게 쓴다."""
    out = df.copy()
    for c in out.columns:
        if out[c].dtype == object and out[c].map(lambda v: isinstance(v, (dict, list, tuple))).any():
            out[c] = out[c].map(lambda v: json.dumps(v, ensure_ascii=False, default=str) if isinstance(v, (dict, list, tuple)) else v)
    return out


def _save(df: pd.DataFrame | None, path: Path) -> None:
    if df is None or len(df) == 0:
        return
    _jsonable(df).to_csv(path, index=False, encoding="utf-8-sig")


def main() -> int:
    ap = argparse.ArgumentParser(description="식자재 발주 예측 엔진 단독 실행")
    ap.add_argument("--data", required=True, help="CSV 17개가 든 폴더 (하위 폴더 1단계까지 탐색) 또는 zip")
    ap.add_argument("--out", default="forecast_out", help="산출물 폴더 (기본 forecast_out)")
    ap.add_argument("--asof", default=None, help="기준일 YYYY-MM-DD (기본: 일별 재고 마지막 날)")
    ap.add_argument("--horizon", type=int, default=14, help="예측 기간(일), 기본 14")
    ap.add_argument("--service-level", type=float, default=0.95, help="서비스 레벨 0.80~0.99, 기본 0.95")
    ap.add_argument("--demand-pct", type=float, default=0.0, help="수요 시나리오 ±%% (예: 10 → 1.1배)")
    ap.add_argument("--no-promo", action="store_true", help="프로모션 계수 끄기")
    ap.add_argument("--no-holiday", action="store_true", help="공휴일 계수 끄기")
    ap.add_argument("--delay-buffer", type=int, default=0, help="입고 지연 보정(일)")
    ap.add_argument("--store", default=None, help="브리핑·KPI 를 한 매장만 (예: S01). 산출물은 전 매장")
    ap.add_argument("--forecast-only", action="store_true", help="예측·소요량·σ 까지만 계산")
    args = ap.parse_args()

    t0 = time.perf_counter()
    out_dir = Path(args.out); out_dir.mkdir(parents=True, exist_ok=True)
    print(f"[1/3] 데이터 읽는 중: {args.data}")
    ds = load_all(args.data)
    loaded = [k for k, v in ds.tables.items() if v is not None]
    missing = [k for k, v in ds.tables.items() if v is None]
    print(f"      파일 {len(loaded)}/{len(ds.tables)} 로드" + (f", 누락: {missing}" if missing else ""))

    params = EngineParams(asof_date=pd.Timestamp(args.asof) if args.asof else None, horizon_days=args.horizon,
                          service_level=args.service_level, demand_multiplier=1 + args.demand_pct / 100.0,
                          apply_promo=not args.no_promo, apply_holiday=not args.no_holiday, delay_buffer_days=args.delay_buffer)
    info: dict = {"data": str(args.data), "args": vars(args), "stage": None, "error": None}

    if not args.forecast_only:
        print("[2/3] 엔진 전체 실행 (예측 → 소요량 → 재고 전개 → 발주 추천 → 알림)")
        try:
            out = run_engine(ds, params)
            _save(out.forecast, out_dir / "forecast_menu_daily.csv")
            _save(out.basis, out_dir / "forecast_basis.csv")
            _save(out.requirement, out_dir / "requirement_ingredient_daily.csv")
            _save(out.sigma, out_dir / "sigma_ingredient.csv")
            _save(out.projection.summary, out_dir / "projection_summary.csv")
            _save(out.projection.daily, out_dir / "projection_daily.csv")
            _save(out.projection.expiring, out_dir / "expiring_lots.csv")
            _save(out.orders.lines, out_dir / "orders_lines.csv")
            _save(out.orders.pos, out_dir / "orders_pos.csv")
            _save(out.alerts, out_dir / "alerts.csv")
            (out_dir / "assumptions.txt").write_text("\n".join(f"- {a}" for a in out.assumptions), encoding="utf-8")
            briefs = ["[전체]", briefing_template(out)]
            for s in list(out.stores["store_id"]):
                briefs += ["", f"[{s}]", briefing_template(out, s)]
            (out_dir / "briefing.txt").write_text("\n".join(briefs), encoding="utf-8")
            k = kpis(out, args.store)
            info.update({"stage": "full", "asof": str(out.params.asof.date()), "horizon_days": out.params.horizon_days,
                         "timings_sec": {a: round(b, 2) for a, b in out.timings.items()}, "kpis": {a: (float(b) if isinstance(b, (int, float)) else str(b)) for a, b in k.items()}})
            print(f"      기준일 {out.params.asof.date()} · 예측 {out.params.horizon_days}일 · 발주 라인 {len(out.orders.lines)} · 알림 {len(out.alerts)}")
            print("      KPI" + (f" ({args.store})" if args.store else "") + ": " + ", ".join(f"{a}={b}" for a, b in k.items()))
        except Exception as e:  # 재고 전개·발주 단계가 실데이터에서 깨져도 예측 결과는 남긴다
            info["error"] = f"{type(e).__name__}: {e}"
            print(f"      !! 엔진 전체 실행 실패 → 예측·소요량만 저장합니다. 오류: {info['error']}")
            traceback.print_exc()
            args.forecast_only = True

    if args.forecast_only:
        print("[2/3] 예측·소요량·σ 만 계산")
        params2, notes = resolve_params(ds, params)
        daily = prep.daily_sales(ds)
        fc_notes: list[str] = []
        forecast, basis = forecast_menu_daily(ds, params2, daily=daily, notes=fc_notes)
        req = ingredient_requirement(forecast, ds)
        sigma = ingredient_sigma(basis, ds)
        _save(forecast, out_dir / "forecast_menu_daily.csv"); _save(basis, out_dir / "forecast_basis.csv")
        _save(req, out_dir / "requirement_ingredient_daily.csv"); _save(sigma, out_dir / "sigma_ingredient.csv")
        (out_dir / "assumptions.txt").write_text("\n".join(f"- {a}" for a in [f"{k}: {v}" for k, v in notes.items()] + fc_notes), encoding="utf-8")
        info.update({"stage": info.get("stage") or "forecast_only", "asof": str(params2.asof.date()), "horizon_days": params2.horizon_days})
        tot = forecast.groupby("store_id")["forecast_qty"].sum().round(0).to_dict()
        print(f"      기준일 {params2.asof.date()} · 예측 {params2.horizon_days}일 · 매장별 예측 합계 {tot}")

    info["elapsed_sec"] = round(time.perf_counter() - t0, 2)
    (out_dir / "run_info.json").write_text(json.dumps(info, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"[3/3] 저장 완료: {out_dir.resolve()}  ({info['elapsed_sec']}초)")
    for p in sorted(out_dir.iterdir()):
        print(f"      - {p.name}")
    return 0 if info["error"] is None else 2


if __name__ == "__main__":
    sys.exit(main())
