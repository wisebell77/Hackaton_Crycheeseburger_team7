"""Local dashboard and policy extraction. API keys stay in server environment."""
import argparse
import os
from pathlib import Path
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

import policy_api

ROOT = Path(__file__).resolve().parent

class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT / 'dist'), **kwargs)

    def do_GET(self):
        if self.path == '/api/policy':
            policy_api.reply(self, 200, {'provider': policy_api.provider()}); return
        super().do_GET()

    def do_POST(self):
        if self.path != '/api/policy':
            self.send_error(404); return
        # Local app only. Reject cross-origin requests, including DNS rebinding hosts.
        host = self.headers.get('Host', '')
        if host not in {f'127.0.0.1:{self.server.server_port}', f'localhost:{self.server.server_port}'} or self.headers.get('Origin') not in (None, 'http://' + host):
            self.send_error(403); return
        policy_api.reply(self, *policy_api.handle(self.headers, self.rfile))

def load_env(path):
    """KEY=VALUE 줄만 읽고, 이미 설정된 환경변수는 덮어쓰지 않는다."""
    if not path.is_file():
        return
    for line in path.read_text(encoding='utf-8').splitlines():
        key, sep, value = line.strip().partition('=')
        if sep and key and not key.startswith('#'):
            os.environ.setdefault(key.strip(), value.strip().strip('\'"'))

if __name__ == '__main__':
    p = argparse.ArgumentParser(); p.add_argument('--port', type=int, default=8000)
    p.add_argument('--env-file', type=Path, default=ROOT.parent / '.env', help='API 키 파일 (Git에 올리지 않음)')
    args = p.parse_args()
    load_env(args.env_file)
    server = ThreadingHTTPServer(('127.0.0.1', args.port), Handler)
    print(f'식자재 매니저: http://localhost:{args.port}', flush=True)
    server.serve_forever()
