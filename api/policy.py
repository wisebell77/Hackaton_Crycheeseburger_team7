"""Vercel 서버리스 함수: 배포 화면(web-dashboard/dist)과 같은 주소의 /api/policy.

로컬 serve.py와 같은 policy_api를 쓴다. Claude 키는 Vercel 환경변수 ANTHROPIC_API_KEY에만 둔다.
"""
import sys
from http.server import BaseHTTPRequestHandler
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'web-dashboard'))
import policy_api


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        policy_api.reply(self, 200, {'provider': policy_api.provider()})

    def do_POST(self):
        # 다른 사이트의 페이지가 방문자 브라우저로 이 함수(와 API 사용량)를 쓰지 못하게 같은 주소의 요청만 받는다.
        hosts = {self.headers.get('Host'), self.headers.get('X-Forwarded-Host')} - {None}
        if self.headers.get('Origin') not in {'https://' + h for h in hosts}:
            policy_api.reply(self, 403, {'error': '이 사이트 화면에서 보낸 요청만 처리합니다.'}); return
        policy_api.reply(self, *policy_api.handle(self.headers, self.rfile))
