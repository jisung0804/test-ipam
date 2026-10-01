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


class DraggableColumns(PluginTemplateExtension):
    models = get_plugin_config('netbox_ip_request', 'draggable_tables') or ['ipam.ipaddress']

    def list_buttons(self):
        return DRAG_JS


template_extensions = [DraggableColumns]
