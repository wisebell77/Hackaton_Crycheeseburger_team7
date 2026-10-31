"""/api/policy 공통 처리. 로컬 서버(serve.py)와 Vercel 함수(api/policy.py)가 함께 쓴다.

자유 문장 해석 키는 서버 환경변수에만 둔다: ANTHROPIC_API_KEY(Claude) 또는
POLICY_API_URL·POLICY_API_MODEL·POLICY_API_KEY(호환 API). 둘 다 있으면 Claude를 쓴다.
"""
import json
import os
import sys
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parent / 'model'))
from order_policy import compile_policy, ClaudeExtractor, CompatibleAPIExtractor


def provider():
    if os.getenv('ANTHROPIC_API_KEY'):
        return 'Claude'
    if os.getenv('POLICY_API_URL'):
        return '호환 API'
    return None


@lru_cache(maxsize=1)
def catalog():
    data = json.loads((ROOT / 'dist/data.json').read_text(encoding='utf-8'))
    return {'stores': {x['store_id']: x['store_name'] for x in data['stores']},
            'ingredients': {x['ingredient_id']: x['ingredient_name'] for x in next(iter(data['snapshots'].values()))}}


def handle(headers, rfile):
    """POST 본문 → (HTTP 상태, JSON 값)."""
    try:
        size = int(headers.get('Content-Length', '0'))
        if not 0 < size <= 65536:
            raise ValueError('요청 크기를 확인해주세요.')
        body = json.loads(rfile.read(size))
        if not isinstance(body, dict):
            raise ValueError('요청 형식을 확인해주세요.')
        text = body.get('text', '')
        if not isinstance(text, str) or len(text) > 2000:
            raise ValueError('요청은 2,000자 이내로 입력해주세요.')
        extractor = None
        if body.get('api') is True:
            name = provider()
            if name is None:
                raise ValueError('자유 문장 해석에는 서버 환경변수 ANTHROPIC_API_KEY(또는 POLICY_API_URL·POLICY_API_MODEL·POLICY_API_KEY)가 필요합니다.')
            extractor = ClaudeExtractor() if name == 'Claude' else CompatibleAPIExtractor()
        return 200, compile_policy(order_text=text, catalog=catalog(), extractor=extractor)
    except (ValueError, TypeError, KeyError) as e:
        return 400, {'error': str(e)}


def reply(handler, status, value):
    payload = json.dumps(value, ensure_ascii=False, allow_nan=False).encode()
    handler.send_response(status)
    handler.send_header('Content-Type', 'application/json; charset=utf-8')
    handler.send_header('Cache-Control', 'no-store')
    handler.send_header('Content-Length', str(len(payload)))
    handler.end_headers(); handler.wfile.write(payload)
