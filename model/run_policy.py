"""정책 JSON 생성만 수행. 주문 전송/외부 앱 쓰기는 하지 않는다."""
import argparse
import csv
import json
from pathlib import Path

from order_policy import compile_policy, CompatibleAPIExtractor


def read_catalog(folder):
    catalog = {}
    for kind, prefix, id_col, name_col in (("stores", "01_", "store_id", "store_name"),
                                          ("ingredients", "03_", "ingredient_id", "ingredient_name")):
        paths = sorted(Path(folder).glob(prefix + "*.csv"))
        if len(paths) != 1:
            raise ValueError(f"{prefix} CSV 파일이 정확히 하나 필요합니다")
        with paths[0].open(encoding="utf-8-sig", newline="") as f:
            rows = list(csv.DictReader(f))
        catalog[kind] = {r[id_col]: r[name_col] for r in rows}
    return catalog


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--owner", default="균형 있게 주문하자")
    ap.add_argument("--order", default="")
    ap.add_argument("--data", help="01/03 CSV가 있는 폴더 (ID/이름 검증용)")
    ap.add_argument("--api", action="store_true")
    ap.add_argument("--out", help="결과 JSON 경로 (미지정 시 화면 출력)")
    args = ap.parse_args()
    catalog = read_catalog(args.data) if args.data else None
    result = compile_policy(args.owner, args.order, catalog=catalog,
                            extractor=CompatibleAPIExtractor() if args.api else None)
    content = json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False)
    if args.out:
        path = Path(args.out)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content + "\n", encoding="utf-8")
    else:
        print(content)
    return 0 if result["status"] == "ready" else 2


if __name__ == "__main__":
    raise SystemExit(main())
