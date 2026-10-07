"""수집 서버(NetBox 컨테이너)에서 장비들의 SNMPv3 응답만 확인 — NetBox 가 실제로 쓰는 계정·경로 그대로
장비 등록·수집과 같은 코드(netbox_ip_request.snmp)로 sysName 만 읽는다. 장비 설정은 바꾸지 않는다.

실행 (/opt/netbox-docker):
  docker compose exec -T -e IPS="165.246.48.2 165.246.48.3" netbox \
      /opt/netbox/venv/bin/python /opt/netbox/netbox/manage.py shell < /opt/test-ipam/deploy/centos/snmp_reach.py
출력: 한 줄에 한 대  REACH|<ip>|<ok|timeout|usm|auth|error>|<sysName 또는 사유>
  (tools/fix_collector_acl.sh 가 이 출력을 읽어 실패 장비만 ACL 을 다시 넣는다)
"""
import os
import re

from netbox_ip_request import snmp

ips = [x for x in re.split(r'[\s,]+', os.environ.get('IPS', '')) if x]
creds = snmp.credentials()
if not creds:
    print('REACH|-|error|SNMP 계정이 없음 — env/ipam-snmp.env 의 IPAM_SNMP_USER/AUTH/PRIV 확인')
elif ips:
    results = snmp.poll([{'name': ip, 'host': ip, 'arp': False, 'mac': False} for ip in ips], what=(), creds=creds)
    for r in results:
        err = r.get('error') or ''
        if not err:
            st, msg = 'ok', (r.get('system') or {}).get('name') or '-'
        elif '타임아웃' in err or 'timeout' in err.lower() or '시간 초과' in err:
            st, msg = 'timeout', err
        elif '없는 SNMP 사용자' in err or 'Unknown USM' in err:
            st, msg = 'usm', err
        elif '불일치' in err or '비밀번호' in err:
            st, msg = 'auth', err
        else:
            st, msg = 'error', err
        print(f"REACH|{r['host']}|{st}|{msg.replace('|', '/')[:200]}")
