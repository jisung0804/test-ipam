"""IP 신청 위치(건물·호실) → VLAN 대역 자동 매칭

사용자는 대역을 고르지 않는다. 건물(드롭다운)과 호실번호만 적으면 IP 대장(엑셀 최초 값 + 실제 L2 로 덮어쓴 값)에서
그 호실·층·건물이 실제로 쓰는 대역을 찾아 신청의 '발급 대역'에 넣는다. 근거가 약하면 비워 두고 관리자가 고른다.

매칭 순서 (앞에서 찾으면 멈춤)
  1) 같은 호실   : 그 호실에 등록된 IP 들이 속한 대역 중 가장 많은 것
  2) 같은 층     : 같은 건물·같은 층 호실들의 IP 중 한 대역이 절반 이상
  3) 같은 건물   : 건물 전체 IP 중 한 대역이 60% 이상
  4) 대역 설명   : 대역·VLAN 의 이름/설명에 건물 이름(예: 9호관)이 들어간 대역이 딱 하나
  그 밖         : 비워 둠 (관리자 선택)
호실번호 규칙: '건물번호-호실'(예: 9-101). 건물을 고르고 '101'만 적으면 '9-101'로 바꿔 저장한다.
층: 호실의 마지막 두 자리를 뺀 앞부분 (101→1층, 1203→12층, B102→B1층)
"""
import ipaddress
import re
from collections import Counter

FLOOR_SHARE = 0.5
BUILDING_SHARE = 0.6


def buildings():
    """신청서 드롭다운: 하위 위치(호실)가 있는 최상위 위치 = 건물. 이름의 숫자 순으로 정렬"""
    from dcim.models import Location
    qs = Location.objects.filter(parent__isnull=True, children__isnull=False).distinct()
    return sorted(qs, key=lambda l: (int(m.group()) if (m := re.match(r'\d+', l.name)) else 10 ** 6, l.name))


def building_no(building):
    n = building.name.strip()
    return n[:-2] if n.endswith('호관') else n


def full_room(building, room):
    room = (room or '').strip().replace(' ', '')
    if not room:
        return ''
    if '-' in room or building is None:
        return room
    return f'{building_no(building)}-{room}'


def floor_of(room):
    part = room.split('-', 1)[1] if '-' in room else room
    m = re.match(r'^(B?\d+?)(\d{2})[A-Za-z]?$', part, re.I)
    return m.group(1).upper() if m else None


def _prefixes():
    from ipam.models import Prefix
    nets = [(ipaddress.ip_network(str(p.prefix)), p) for p in Prefix.objects.filter(vrf__isnull=True).select_related('vlan')]
    return sorted([t for t in nets if t[0].version == 4 and t[0].prefixlen >= 16], key=lambda t: -t[0].prefixlen)


def _tally(loc_ids, nets):
    """위치(호실) id 들에 등록된 IP → 가장 작은 포함 대역별 개수"""
    from ipam.models import IPAddress
    c = Counter()
    if not loc_ids:
        return c
    for addr in IPAddress.objects.filter(custom_field_data__room__in=list(loc_ids), vrf__isnull=True) \
            .values_list('address', flat=True):
        a = ipaddress.ip_address(str(addr.ip))
        p = next((p for n, p in nets if a in n), None)
        if p is not None:
            c[p] += 1
    return c


def _clear_top(c, share):
    """1등 대역이 비율 이상이고 2등과 동점이 아니면 그 대역"""
    top = c.most_common(2)
    p, n = top[0]
    if n / sum(c.values()) < share or (len(top) > 1 and top[1][1] == n):
        return None, n
    return p, n


def _fmt(c, k=3):
    return ', '.join(f'{p.prefix}({n})' for p, n in c.most_common(k))


def match(building, room):
    """반환 {'prefix': Prefix|None, 'room': 정규화한 호실, 'building': Location|None, 'note': 근거, 'candidates': [(Prefix, 개수)]}"""
    from dcim.models import Location
    room = full_room(building, room)
    room_loc = Location.objects.filter(name=room).first() if room else None
    bld = building or (room_loc.parent if room_loc and room_loc.parent_id else None)
    if bld is None and room and '-' in room:
        bld = Location.objects.filter(name=f"{room.split('-')[0]}호관", parent__isnull=True).first()
    out = {'prefix': None, 'room': room, 'building': bld, 'note': '', 'candidates': []}
    if not room and bld is None:
        out['note'] = '건물·호실 정보 없음 — 관리자가 대역을 선택'
        return out
    nets = _prefixes()
    rooms = list(Location.objects.filter(parent=bld).values_list('pk', 'name')) if bld else []
    b_tally = _tally([pk for pk, _ in rooms], nets)
    out['candidates'] = b_tally.most_common(5)

    if room_loc is not None:                                                     # 1) 같은 호실
        c = _tally([room_loc.pk], nets)
        if c:
            p, n = c.most_common(1)[0]
            out.update(prefix=p, note=f'같은 호실({room}) IP {sum(c.values())}개 중 {n}개가 이 대역'
                                      + (f' · 다른 대역: {_fmt(Counter(dict(c.most_common()[1:])))}' if len(c) > 1 else ''))
            out['candidates'] = (b_tally or c).most_common(5)
            return out
    fl = floor_of(room) if room else None
    if bld and fl:                                                               # 2) 같은 층
        same = [pk for pk, name in rooms if floor_of(name) == fl]
        c = _tally(same, nets)
        if c:
            p, n = _clear_top(c, FLOOR_SHARE)
            if p is not None:
                out.update(prefix=p, note=f'같은 층({bld.name} {fl}층) IP {sum(c.values())}개 중 {n}개가 이 대역')
                return out
    if b_tally:                                                                  # 3) 같은 건물
        p, n = _clear_top(b_tally, BUILDING_SHARE)
        if p is not None:
            out.update(prefix=p, note=f'같은 건물({bld.name}) IP {sum(b_tally.values())}개 중 {n}개가 이 대역')
            return out
    if bld:                                                                      # 4) 대역·VLAN 설명
        key = bld.name
        hits = {p for n, p in nets if key in (p.description or '') or (p.vlan and (key in (p.vlan.name or '')
                                                                                    or key in (p.vlan.description or '')))}
        if len(hits) == 1:
            p = hits.pop()
            out.update(prefix=p, note=f"대역/VLAN 설명에 '{key}' 포함")
            return out
    why = '같은 호실·층·건물에 등록된 IP 가 없음' if not b_tally else f'건물 안에서 대역이 갈림: {_fmt(b_tally)}'
    out['note'] = f'찾지 못함({why}) — 관리자가 대역을 선택'
    return out
