"""엔진 위치 고정: model/ 폴더 바로 아래의 src/ 를 쓴다 (예전 zip 자동 해제 방식은 제거)."""
from pathlib import Path
import sys


def load_engine(root, explicit=None):
    """root(보통 model/) 또는 explicit 폴더의 src/ 엔진을 import 경로에 올린다."""
    engine = Path(explicit or root).resolve()
    if not (engine / "src" / "engine" / "forecast.py").is_file():
        raise ValueError(f"예측 엔진 src/ 를 찾을 수 없습니다: {engine}")
    if "src" in sys.modules:
        loaded = Path(sys.modules["src"].__file__).resolve().parent
        if loaded != engine / "src":
            raise ValueError("다른 src 엔진이 이미 로드됐습니다. 새 프로세스에서 실행하세요.")
    if str(engine) not in sys.path:
        sys.path.insert(0, str(engine))
    return engine
