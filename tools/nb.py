"""NetBox REST 클라이언트 (환경변수 NETBOX_URL, NETBOX_TOKEN)"""
import os, requests
URL = os.environ.get('NETBOX_URL', 'http://127.0.0.1:8001').rstrip('/') + '/api'
TOKEN = (os.environ.get('NETBOX_TOKEN') or '').strip().strip('"\'')
# NetBox 4.7 토큰 화면은 'Bearer nbt_...' 전체를 보여준다 → 앞의 'Bearer '/'Token '은 떼고 사용
for _p in ('Bearer ', 'Token '):
    if TOKEN.startswith(_p):
        TOKEN = TOKEN[len(_p):].strip()
if not TOKEN:
    raise SystemExit('NETBOX_TOKEN 환경변수를 설정하세요 (NetBox 우측 상단 사용자 메뉴 > API Tokens)')


def session():
    s = requests.Session()
    s.headers.update({'Authorization': f'Token {TOKEN}', 'Accept': 'application/json',
                      'Content-Type': 'application/json'})
    return s


S = session()


def get_all(path, **params):
    params.setdefault('limit', 1000)
    out, url = [], URL + path
    while url:
        r = S.get(url, params=params); r.raise_for_status(); d = r.json()
        out += d['results']; url, params = d['next'], None
    return out
