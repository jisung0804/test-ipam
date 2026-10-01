"""IP 자원 현황 — VLAN(대역)별 전체·대장 등록·실사용·가용 계산 + 엑셀 내보내기/가져오기

실사용 = 최근 N일(기본 30일) 안에 L3 장비 ARP 에 보인 IP (실제 장비 기준)
대장   = NetBox IP 주소 목록에 등록된 IP
가용   = 사용 가능 주소 수 − (대장 ∪ 실사용)          ← 둘 중 하나라도 걸리면 '쓰는 중'으로 봄
L2 MAC = 최근 1일 액세스 스위치 MAC 테이블에서 그 VLAN 으로 학습된 단말 MAC 수 (스위치 수 함께)
"""
import datetime as dt
import ipaddress
import io

import pandas as pd
from django.db.models import Count
from django.utils import timezone

from .models import ArpEntry, MacEntry

COLUMNS = [('vid', 'VLAN ID'), ('vlan_name', 'VLAN 이름'), ('description', '설명'), ('prefix', '대역'),
           ('tenant', '소속'), ('gateway', '게이트웨이'), ('size', '전체'), ('registered', '대장 등록'),
           ('in_use', '실사용'), ('reg_unused', '대장O·미사용'), ('unreg_used', '대장X·사용'),
           ('available', '가용'), ('util', '사용률(%)'), ('l2_mac', 'L2 단말 MAC'), ('l2_switch', '스위치 수')]


def compute(days=30, q='', site_id=None):
    from ipam.models import IPAddress, Prefix
    now = timezone.now()
    pfx_qs = Prefix.objects.select_related('vlan', 'tenant').order_by('prefix')
    if site_id:
        pfx_qs = pfx_qs.filter(_site_id=site_id) if hasattr(Prefix, '_site') else pfx_qs
    pfxs = [p for p in pfx_qs if p.prefix.version == 4 and p.prefix.prefixlen >= 16]
    nets = sorted(((ipaddress.ip_network(str(p.prefix)), p) for p in pfxs), key=lambda t: -t[0].prefixlen)
    cache = {}

    def owner(ip):
        a = ipaddress.ip_address(ip)
        key = ipaddress.ip_network(f'{ip}/24', strict=False)
        cand = cache.get(key)
        if cand is None:
            cand = cache[key] = [(n, p) for n, p in nets if n.overlaps(key)]
        return next((p for n, p in cand if a in n), None)

    reg, obs, gw = {}, {}, {}
    for addr, desc in IPAddress.objects.filter(vrf__isnull=True).values_list('address', 'description'):
        h = str(addr.ip)
        p = owner(h)
        if p:
            reg.setdefault(p.pk, set()).add(h)
            if desc and desc.startswith('게이트웨이'):
                gw[p.pk] = h
    for ip in ArpEntry.objects.filter(last_seen__gte=now - dt.timedelta(days=days)).values_list('ip', flat=True).distinct():
        try:
            p = owner(ip)
        except ValueError:
            continue
        if p:
            obs.setdefault(p.pk, set()).add(ip)
    l2 = {}
    for r in (MacEntry.objects.filter(last_seen__gte=now - dt.timedelta(days=1)).exclude(vlan='')
              .values('vlan').annotate(n=Count('mac', distinct=True), sw=Count('device', distinct=True))):
        l2[str(r['vlan'])] = (r['n'], r['sw'])
    rows = []
    for p in pfxs:
        n = p.prefix
        size = n.size - 2 if n.prefixlen < 31 else n.size
        R, O = reg.get(p.pk, set()), obs.get(p.pk, set())
        used = len(R | O)
        vid = p.vlan.vid if p.vlan else None
        row = {'pk': p.pk, 'vid': vid or '', 'vlan_name': p.vlan.name if p.vlan else '',
               'description': p.description or (p.vlan.description if p.vlan else ''), 'prefix': str(n),
               'tenant': p.tenant.name if p.tenant else '', 'gateway': gw.get(p.pk, ''), 'size': size,
               'registered': len(R), 'in_use': len(O), 'reg_unused': len(R - O), 'unreg_used': len(O - R),
               'available': max(size - used, 0), 'util': round(used * 100 / size, 1) if size else 0,
               'l2_mac': l2.get(str(vid), (0, 0))[0] if vid else 0, 'l2_switch': l2.get(str(vid), (0, 0))[1] if vid else 0}
        if q:
            hay = ' '.join(str(row[k]) for k in ('vid', 'vlan_name', 'description', 'prefix', 'tenant', 'gateway')).lower()
            if q.lower() not in hay:
                continue
        rows.append(row)
    tot = {k: sum(r[k] for r in rows) for k in ('size', 'registered', 'in_use', 'reg_unused', 'unreg_used', 'available')}
    tot['count'] = len(rows)
    tot['util'] = round((tot['size'] - tot['available']) * 100 / tot['size'], 1) if tot['size'] else 0
    return rows, tot


def to_excel(rows):
    df = pd.DataFrame([{label: r[key] for key, label in COLUMNS} for r in rows], columns=[l for _, l in COLUMNS])
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine='openpyxl') as w:
        df.to_excel(w, index=False, sheet_name='IP 자원')
        ws = w.sheets['IP 자원']
        for i, (_, label) in enumerate(COLUMNS, 1):
            ws.column_dimensions[ws.cell(1, i).column_letter].width = max(10, len(label) * 2 + 2)
    return buf.getvalue()


# --------------------------------------------------------------------- VLAN 엑셀 가져오기
VLAN_COLS = {'VLAN ID': 'vid', 'VLAN 이름': 'name', '설명': 'description', '대역': 'prefix', '소속': 'tenant',
             '게이트웨이': 'gateway'}


def _prefix_of(text, b1, b2):
    from ipam.models import Prefix
    t = str(text).strip()
    if t.isdigit():
        t = f'{b1}.{b2}.{int(t)}.0/24'
    try:
        net = ipaddress.ip_network(t, strict=False)
    except ValueError:
        return None
    return Prefix.objects.filter(prefix=str(net), vrf__isnull=True).first()


def _tenant(name):
    from tenancy.models import Tenant
    from .inventory import _slug
    t = Tenant.objects.filter(name=name[:100]).first()
    if t is None:
        t = Tenant(name=name[:100], slug=_slug(name, 'dept')); t.full_clean(); t.save()
    return t


def import_vlans(fileobj, site, base='165.246', log=print):
    """열: VLAN ID(필수) | VLAN 이름 | 설명 | 대역(예: 165.246.49.0/24 또는 49) | 소속 | 게이트웨이
    IP 자원 화면의 '엑셀 내보내기' 파일을 그대로 고쳐서 올려도 된다(다른 열은 무시).
    빈 칸은 기존 값을 지우지 않는다. 반환 Counter"""
    from collections import Counter
    from django.core.exceptions import ValidationError
    from ipam.models import IPAddress, Prefix, VLAN
    from tenancy.models import Tenant
    from .inventory import ensure_base, _slug
    c = Counter()
    try:
        df = pd.read_excel(fileobj, dtype=str).fillna('')
    except Exception as e:
        raise ValueError(f'엑셀 파일을 읽을 수 없음: {e}')
    df.columns = [str(x).strip() for x in df.columns]
    if 'VLAN ID' not in df.columns:
        raise ValueError("'VLAN ID' 열이 없습니다 (첫 행이 제목 줄이어야 함)")
    _, _, grp = ensure_base(site)
    b1, b2 = base.split('.')
    for i, r in df.iterrows():
        line = i + 2
        g = {v: str(r.get(k, '')).strip() for k, v in VLAN_COLS.items()}
        if not g['vid']:
            if g['prefix'] and (g['description'] or g['tenant']):   # VLAN 없는 대역: 설명·소속만
                p = _prefix_of(g['prefix'], b1, b2)
                if p is None:
                    log(f'{line}행: 대역 {g["prefix"]!r} 를 찾을 수 없음 — 건너뜀'); c['skipped'] += 1
                    continue
                p.snapshot()
                if g['description']:
                    p.description = g['description'][:200]
                if g['tenant']:
                    p.tenant = _tenant(g['tenant'])
                p.full_clean(); p.save(); c['prefix_only'] += 1
            continue
        try:
            vid = int(float(g['vid']))
            assert 1 <= vid <= 4094
        except (ValueError, AssertionError):
            log(f'{line}행: VLAN ID 형식 오류 {g["vid"]!r} — 건너뜀'); c['skipped'] += 1
            continue
        pfx_obj = _prefix_of(g['prefix'], b1, b2) if g['prefix'] else None
        v = pfx_obj.vlan if pfx_obj is not None and pfx_obj.vlan and pfx_obj.vlan.vid == vid else None
        v = v or VLAN.objects.filter(group=grp, vid=vid).first()
        if v is None:
            v = VLAN(group=grp, vid=vid, name=(g['name'] or f'VLAN{vid}')[:64], status='active'); c['vlan_new'] += 1
        else:
            v.snapshot(); c['vlan_upd'] += 1
        if g['name']:
            v.name = g['name'][:64]
        if g['description']:
            v.description = g['description'][:200]
        if g['tenant']:
            v.tenant = _tenant(g['tenant'])
        try:
            v.full_clean()
        except ValidationError as e:
            log(f'{line}행: VLAN {vid} 저장 실패 — {"; ".join(e.messages)[:150]}'); c['skipped'] += 1
            continue
        v.save()
        if g['prefix']:
            pf = g['prefix']
            if pf.isdigit():
                pf = f'{b1}.{b2}.{int(pf)}.0/24'
            try:
                net = ipaddress.ip_network(pf, strict=False)
            except ValueError:
                log(f'{line}행: 대역 형식 오류 {g["prefix"]!r} — VLAN 만 반영'); c['prefix_bad'] += 1
                continue
            p = Prefix.objects.filter(prefix=str(net), vrf__isnull=True).first()
            if p is None:
                p = Prefix(prefix=str(net), status='active', scope=site); c['prefix_new'] += 1
            else:
                p.snapshot()
            p.vlan = v
            if g['description']:
                p.description = g['description'][:200]
            if v.tenant:
                p.tenant = v.tenant
            p.full_clean(); p.save(); c['prefix_link'] += 1
            if g['gateway']:
                try:
                    gip = ipaddress.ip_address(g['gateway'] if '.' in g['gateway'].strip('.') and g['gateway'].count('.') == 3
                                               else f'{b1}.{b2}.{net.network_address.packed[2]}.{int(g["gateway"])}')
                except ValueError:
                    log(f'{line}행: 게이트웨이 형식 오류 {g["gateway"]!r}'); continue
                if gip in net and not IPAddress.objects.filter(address__net_host=str(gip)).exists():
                    o = IPAddress(address=f'{gip}/{net.prefixlen}', status='active', description='게이트웨이 (엑셀)')
                    o.full_clean(); o.save(); c['gateway_new'] += 1
    return c
