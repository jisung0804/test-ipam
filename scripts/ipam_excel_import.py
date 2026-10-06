"""NetBox 웹 화면에서 호실 IP 대장(엑셀)을 올리는 스크립트

설치: NetBox 화면 > 사용자 정의(Customization) > 스크립트 > 추가 > 이 파일 업로드 (한 번만)
사용: 스크립트 목록 > '호실 IP 대장 엑셀 가져오기' > 파일 선택 > 실행
      - '변경사항 커밋' 체크 해제 = 점검만(아무것도 바뀌지 않음)
      - 체크 = 실제 반영. 형식 오류 행은 가능한 범위에서 반영하고, 같은 IP 행은 하나로 합침 (엄격 모드를 켜면 오류 시 전체 취소)
"""
from extras.scripts import BooleanVar, ChoiceVar, FileVar, Script, StringVar
from utilities.exceptions import AbortScript

import netbox_ip_request
from netbox_ip_request import room_import as RI

# NetBox 이미지(플러그인)가 옛 버전이어도 화면은 열리게 하고, 실행할 때 안내한다
EXISTING_MODES = getattr(RI, 'EXISTING_MODES', (('skip', '건너뜀 (새 IP만 추가)'), ('overwrite', '덮어쓰기')))
NEED = '0.3.1'


class RoomExcelImport(Script):
    class Meta:
        name = '호실 IP 대장 엑셀 가져오기'
        description = '현행 호실 기준 IP 대장(엑셀)을 NetBox에 반영 — 형식 오류는 건너뛰고, 같은 IP 행은 합쳐서 올림'
        commit_default = False          # 처음 열면 '점검만'이 기본
        scheduling_enabled = False
        job_timeout = 3600
        fieldsets = (('파일', ('excel_file',)), ('옵션', ('site_name', 'base', 'existing', 'strict')))

    excel_file = FileVar(label='엑셀 파일 (.xlsx)', description='첫 행이 제목 줄인 현행 양식 그대로')
    site_name = StringVar(label='사이트 이름', default='인하대학교')
    base = StringVar(label='IP 앞 두 자리', default='165.246', regex=r'^\d{1,3}\.\d{1,3}$')
    existing = ChoiceVar(label='이미 NetBox에 있는 IP', choices=EXISTING_MODES, default='skip')
    strict = BooleanVar(label='엄격 모드', default=False,
                        description='켜면 형식 오류·중복 IP가 1건이라도 있을 때 전체 취소 (끄면 무시하고 반영)')

    def run(self, data, commit_changes):
        ver = netbox_ip_request.config.version
        self.log_info(f'플러그인 버전 {ver} (스크립트는 {NEED} 이상 필요)')
        if tuple(int(x) for x in ver.split('.')) < tuple(int(x) for x in NEED.split('.')):
            raise AbortScript(f'NetBox 이미지의 플러그인이 옛 버전({ver})입니다. netbox-docker 폴더에서 새 zip 적용 후 '
                              f'"docker compose build --no-cache" → "docker compose up -d" 를 실행하세요. '
                              f'(확인: docker compose exec netbox-worker sh -c \'grep "version =" '
                              f'/opt/netbox/venv/lib/python3*/site-packages/netbox_ip_request/__init__.py\')')
        rep = RI.validate(data['excel_file'], data['base'], strict=data['strict'])
        for line, msg in rep['errors']:
            self.log_failure(f'{line}행: {msg}')
        for m in rep['merged'][:200]:
            self.log_info(f"통합 {m['ip']} ← {m['line']}행 | 호실 {', '.join(m['rooms']) or '-'} | "
                          f"소속 {m['dept'] or '-'} | 호실명 {m['room_name'] or '-'} | MAC {m['mac'] or '-'} | "
                          f"관리자 {m['manager'] or '-'} | 비고 {m['note'] or '-'}")
        # 경고는 종류별 건수를 먼저 보여 주고, 덜 흔한 종류부터 행 단위로 최대 300건
        kinds = {}
        for line, msg in rep['warnings']:
            if msg.startswith('[통합]'):
                continue
            k = ('스위치는 있는데 포트 번호 없음' if '포트 번호가 없음' in msg else
                 msg.split(']')[0] + ']' if msg.startswith('[') else msg.split(':')[0])
            kinds.setdefault(k, []).append((line, msg))
        for k, items in sorted(kinds.items(), key=lambda t: -len(t[1])):
            self.log_info(f'경고 종류: {k} — {len(items)}건')
        shown = 0
        for k, items in sorted(kinds.items(), key=lambda t: len(t[1])):
            for line, msg in items[:max(0, 300 - shown)][:100]:
                self.log_warning(f'{line}행: {msg}')
                shown += 1
        summary = (f"행 {len(rep['rows'])}건 (IP 미발급 {rep['blank']}건), 통합 {len(rep['merged'])}건, "
                   f"오류 {len(rep['errors'])}건, 경고 {len(rep['warnings'])}건")
        if rep['errors']:
            raise AbortScript(f'{summary} — 반영하지 않았습니다. 엄격 모드를 끄면 오류 행도 가능한 범위에서 반영합니다.')
        self.log_info(summary)
        c = RI.commit(rep['rows'], data['site_name'], mode=data['existing'], log=self.log_info)
        result = (f"호실 {c['room']}개, 스위치 {c['switch']}대, IP 신규 {c['created']}건, 덮어씀 {c['updated']}건, "
                  f"합침 {c['merged']}건, 건너뜀(이미 있음) {c['skipped']}건, 미발급 {c['blank']}건, 실패 {c['failed']}건 · "
                  f"스위치: 등록 장비에 연결 {c.get('sw_linked', 0)}대, 새로 만듦(SW-IP) {c.get('sw_new', 0)}대, "
                  f"포트 못 찾음 {c.get('port_unmatched', 0)}건")
        if commit_changes:
            self.log_success('반영 완료 — ' + result)
        else:
            self.log_success('점검 완료(반영 안 함) — 커밋하면: ' + result)
        return result
