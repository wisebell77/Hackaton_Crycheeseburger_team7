"""Local dashboard and policy extraction. API keys stay in server environment."""
import argparse
import json
import os
import sys
from pathlib import Path
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parent / 'model'))
from order_policy import compile_policy, CompatibleAPIExtractor

class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT / 'dist'), **kwargs)

    def do_POST(self):
        if self.path != '/api/policy':
            self.send_error(404); return
        # Local app only. Reject cross-origin requests, including DNS rebinding hosts.
        host = self.headers.get('Host', '')
        if host not in {f'127.0.0.1:{self.server.server_port}', f'localhost:{self.server.server_port}'} or self.headers.get('Origin') not in (None, 'http://' + host):
            self.send_error(403); return
        try:
            size = int(self.headers.get('Content-Length', '0'))
            if not 0 < size <= 65536:
                raise ValueError('요청 크기를 확인해주세요.')
            body = json.loads(self.rfile.read(size))
            text = body.get('text', '')
            if not isinstance(text, str) or len(text) > 2000:
                raise ValueError('요청은 2,000자 이내로 입력해주세요.')
            data = json.loads((ROOT / 'dist/data.json').read_text())
            catalog = {'stores': {x['store_id']: x['store_name'] for x in data['stores']},
                       'ingredients': {x['ingredient_id']: x['ingredient_name'] for x in next(iter(data['snapshots'].values()))}}
            use_api = body.get('api') is True
            if use_api and not os.getenv('POLICY_API_URL'):
                raise ValueError('자유 문장 모드에는 서버의 POLICY_API_URL, POLICY_API_MODEL, POLICY_API_KEY 설정이 필요합니다.')
            result = compile_policy(order_text=text, catalog=catalog,
                                    extractor=CompatibleAPIExtractor() if use_api else None)
            self.reply(200, result)
        except (ValueError, TypeError, KeyError) as e:
            self.reply(400, {'error': str(e)})

    def reply(self, status, value):
        payload = json.dumps(value, ensure_ascii=False, allow_nan=False).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Length', str(len(payload)))
        self.end_headers(); self.wfile.write(payload)

if __name__ == '__main__':
    p = argparse.ArgumentParser(); p.add_argument('--port', type=int, default=8000)
    args = p.parse_args()
    server = ThreadingHTTPServer(('127.0.0.1', args.port), Handler)
    print(f'식자재 매니저: http://localhost:{args.port}', flush=True)
    server.serve_forever()
