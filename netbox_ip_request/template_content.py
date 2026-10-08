"""IP 주소 목록에서 열 제목을 끌어다 놓아(drag & drop) 열 순서를 바꾸는 기능.

동작: 열 제목(th)을 다른 열 제목 위에 놓으면 새 순서를 NetBox 사용자 설정(테이블 설정과 같은 곳)에 저장하고
      화면을 새로 고친다. 사용자마다 따로 저장되며, '테이블 설정 > 재설정'으로 원래대로 돌아간다.
다른 목록(프리픽스, 장비 등)에도 쓰려면 configuration/plugins.py 의 'draggable_tables' 에 모델을 추가한다.
"""
from netbox.plugins import PluginTemplateExtension, get_plugin_config

DRAG_JS = r"""
<span class="text-secondary small ms-2 d-none d-md-inline" title="열 제목을 끌어 다른 열 제목 위에 놓으면 순서가 바뀝니다">
  <i class="mdi mdi-drag-horizontal-variant"></i> 열 제목을 끌어 순서 변경
</span>
<script>
(function () {
  if (window.__ipamColDrag) return; window.__ipamColDrag = true;
  function cookie(n) { const m = document.cookie.match('(^|;)\\s*' + n + '=([^;]*)'); return m ? decodeURIComponent(m[2]) : ''; }
  function setup() {
    const form = document.querySelector('form.userconfigform[data-config-root]');
    const table = document.querySelector('table.object-list');
    if (!form || !table) return;
    const cols = [...form.querySelectorAll('#id_columns option')].map(o => o.value);
    const ths = [...table.querySelectorAll('thead tr:first-child > th')].filter(th =>
      !th.querySelector('input[type=checkbox]') && !th.querySelector('a[hx-get*="sort=actions"]'));
    if (ths.length !== cols.length || ths.length < 2 || ths[0].dataset.ipamDrag) return;
    let from = null;
    ths.forEach((th, i) => {
      th.dataset.ipamDrag = cols[i];
      th.draggable = true;
      th.style.cursor = 'grab';
      th.querySelectorAll('a').forEach(a => a.draggable = false);
      th.addEventListener('dragstart', e => { from = i; e.dataTransfer.effectAllowed = 'move'; e.dataTransfer.setData('text/plain', cols[i]); th.style.opacity = '0.4'; });
      th.addEventListener('dragend', () => { th.style.opacity = ''; ths.forEach(t => t.style.boxShadow = ''); });
      th.addEventListener('dragover', e => { if (from === null) return; e.preventDefault(); ths.forEach(t => t.style.boxShadow = ''); th.style.boxShadow = (i > from ? 'inset -3px 0 0 #0d6efd' : 'inset 3px 0 0 #0d6efd'); });
      th.addEventListener('drop', e => {
        e.preventDefault();
        if (from === null || from === i) return;
        const order = cols.slice(); const [moved] = order.splice(from, 1); order.splice(i, 0, moved);
        const data = form.dataset.configRoot.split('.').reduceRight((v, k) => ({ [k]: v }), { columns: order });
        fetch(form.dataset.url, {
          method: 'PATCH', credentials: 'same-origin',
          headers: { 'Content-Type': 'application/json', 'Accept': 'application/json', 'X-CSRFToken': window.CSRF_TOKEN || cookie('csrftoken') || ((document.querySelector('input[name=csrfmiddlewaretoken]') || {}).value || '') },
          body: JSON.stringify(data)
        }).then(r => { if (r.ok) { location.reload(); } else { r.text().then(t => alert('열 순서 저장 실패: ' + r.status + ' ' + t.slice(0, 200))); } });
      });
    });
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', setup); else setup();
  document.addEventListener('htmx:afterSettle', setup);  // 정렬·페이지 이동으로 표가 다시 그려질 때
})();
</script>
"""


BULK_DELETE_TOP = r"""
<button type="button" class="btn btn-red" id="ipam-top-delete" title="체크한 행 삭제 (아래 '선택 항목 삭제'와 같음)">
  <i class="mdi mdi-trash-can-outline"></i> 선택 삭제 <span class="badge bg-white text-red ms-1" id="ipam-sel-count">0</span>
</button>
<script>
(function () {
  if (window.__ipamTopDel) return; window.__ipamTopDel = true;
  function count() {
    const all = document.querySelector('input[name="_all"]:checked');
    const n = document.querySelectorAll('table.object-list input[name="pk"]:checked').length;
    const el = document.getElementById('ipam-sel-count');
    if (el) el.textContent = all ? '전체' : n;
    return all ? 1 : n;
  }
  document.addEventListener('change', count);
  document.addEventListener('htmx:afterSettle', count);
  document.addEventListener('click', function (e) {
    if (!e.target.closest('#ipam-top-delete')) return;
    if (!count()) { alert('먼저 삭제할 행의 체크박스를 선택하세요. (머리글 체크박스 = 이 페이지 전체)'); return; }
    const b = document.querySelector('button[name="_delete"]');
    if (b) { b.disabled = false; b.click(); }
  });
})();
</script>
"""


class DraggableColumns(PluginTemplateExtension):
    models = get_plugin_config('netbox_ip_request', 'draggable_tables') or ['ipam.ipaddress']

    def list_buttons(self):
        return DRAG_JS


class IPListExtras(PluginTemplateExtension):
    models = ['ipam.ipaddress']

    def list_buttons(self):
        return BULK_DELETE_TOP


class DeviceListExtras(PluginTemplateExtension):
    models = ['dcim.device']

    def list_buttons(self):
        return ('<a href="/extras/scripts/" class="btn btn-teal" title="도구 > 장비 등록 (관리 IP만 입력)">'
                '<i class="mdi mdi-lan-connect"></i> 관리 IP로 장비 등록</a>')


LED_CSS = """<style>
.ipam-led{display:inline-block;width:10px;height:10px;border-radius:50%;vertical-align:middle;background:#868e96}
.ipam-led-ok{background:#2fb344;box-shadow:0 0 6px #2fb344}
.ipam-led-running{background:#2fb344;box-shadow:0 0 6px #2fb344;animation:ipamBlink 1s ease-in-out infinite}
.ipam-led-down{background:#d63939;box-shadow:0 0 6px #d63939}
@keyframes ipamBlink{50%{opacity:.25}}
</style>"""


class SnmpLed(PluginTemplateExtension):
    """우측 상단 SNMP 수집 상태 LED (정상·수집 중: 녹색 / 중단: 적색 / 꺼짐: 회색). 관리자에게만 표시, 30초마다 갱신"""

    def navbar(self):
        from django.urls import reverse
        from urllib.parse import urlencode
        from .jobs import SnmpCollectJob, collect_status
        req = self.context.get('request')
        u = getattr(req, 'user', None)
        if not (u and u.is_authenticated and (u.is_superuser or u.has_perm('netbox_ip_request.change_iprequest'))):
            return ''
        try:
            st = collect_status()
        except Exception as e:          # 화면 전체가 깨지지 않도록
            st = {'state': 'down', 'label': '상태 확인 실패', 'detail': str(e)[:100]}
        from django.utils.html import escape
        jobs = reverse('core:job_list') + '?' + urlencode({'name': SnmpCollectJob.Meta.name})
        api = reverse('plugins:netbox_ip_request:snmp_status')
        title = escape(f"SNMP 수집: {st['label']} — {st['detail']}")
        return (LED_CSS +
                f'<a href="{jobs}" class="nav-link px-2 ipam-snmp-led" title="{title}" aria-label="{title}">'
                f'<span class="ipam-led ipam-led-{st["state"]}"></span>'
                f'<span class="small text-secondary ms-1 d-none d-xl-inline">SNMP</span></a>'
                '<script>(function(){if(window.__ipamLed)return;window.__ipamLed=1;'
                f'setInterval(function(){{fetch("{api}",{{credentials:"same-origin"}}).then(r=>r.json()).then(function(s){{'
                'document.querySelectorAll(".ipam-snmp-led").forEach(function(a){'
                'a.title=a.ariaLabel="SNMP 수집: "+s.label+" — "+s.detail;'
                'a.querySelector(".ipam-led").className="ipam-led ipam-led-"+s.state;});}).catch(function(){});},30000);})();</script>')


template_extensions = [DraggableColumns, IPListExtras, DeviceListExtras, SnmpLed]
