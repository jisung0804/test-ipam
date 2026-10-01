"""변이 검증: 플러그인 방어 로직을 하나씩 제거 → NetBox 재시작 → 스위트가 실패를 잡는지"""
import shutil, subprocess, sys
L = '../netbox_ip_request/logic.py'
M = '../netbox_ip_request/models.py'
MUT = [
 ('M1 승인 시 advisory lock 제거', L, """    with advisory_lock(IP_LOCK):
        with transaction.atomic():
            req = IPRequest""", """    if True:
        with transaction.atomic():
            req = IPRequest"""),
 ('M2 락을 트랜잭션 안쪽으로(순서 뒤집기)', L, """    with advisory_lock(IP_LOCK):
        with transaction.atomic():
            req = IPRequest""", """    with transaction.atomic():
        with advisory_lock(IP_LOCK):
            req = IPRequest"""),
 ('M3 승인 시 상태 확인 제거', L, "            if req.status != RequestStatusChoices.SUBMITTED:\n                raise AllocationError(f'{req}: 이미 처리된 신청입니다({req.get_status_display()})')", "            pass"),
 ('M4 ARP 최근 관측 IP 제외 제거', L, "        if str(ip) in seen:\n            continue", "        pass"),
 ('M5 미사용 경계 <= → <', L, "created) <= %s", "created) < %s"),
 ('M6 격리 해제 시 재관측 검사 제거', L, "                if ls and dt.datetime.fromisoformat(ls) >= start:\n                    continue", "                pass"),
 ('M7 장비 인터페이스 IP 제외 조건 제거', L, "               AND assigned_object_id IS NULL\n", ""),
 ('M8 발급된 MAC 재신청 검사 제거', M, "        if self._state.adding and self.mac and mac_in_use(self.mac):", "        if False:"),
 ('M9 불일치 알림 중복 방지 제거', L, "    _, created = Discrepancy.objects.get_or_create(kind=kind, ip=ip, resolved=False, defaults={'detail': detail[:300]})\n    return int(created)",
  "    Discrepancy.objects.filter(kind=kind, ip=ip).update(resolved=True)\n    Discrepancy.objects.create(kind=kind, ip=ip, detail=detail[:300])\n    return 1"),
]
orig = {f: open(f).read() for f in (L, M)}
try:
    for name, f, a, b in MUT:
        s = orig[f]; assert a in s, name
        open(f, 'w').write(s.replace(a, b))
        subprocess.run(['/home/claude/nb_restart.sh'], capture_output=True)
        out = subprocess.run([sys.executable, 'verify_netbox.py', '1'], capture_output=True, text=True).stdout
        fails = sorted({l.split()[1] for l in out.splitlines() if l.startswith('FAIL ')})
        print(f"{name:34s} → {'탐지됨' if fails else '미탐지!!'} {', '.join(fails)}", flush=True)
        open(f, 'w').write(orig[f])
finally:
    for f, s in orig.items():
        open(f, 'w').write(s)
    subprocess.run(['/home/claude/nb_restart.sh'], capture_output=True)
