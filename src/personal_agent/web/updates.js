// Paired phones and the local desktop use the same protected update endpoint.
const $ = s => document.querySelector(s);
const phases = {queued:'已排队，等待开始', starting:'正在启动更新', checking:'正在检查 GitHub', preparing:'正在准备新版本',
  installing:'正在安装依赖', testing:'正在检查新版本', backup:'正在备份数据', rehearsal:'正在试运行新版本',
  switching:'正在切换版本，连接可能短暂中断', error:'更新意外中断，请查看电脑更新日志',
  interrupted:'更新进程已停止，请检查后重试'};
let timer, polling = false, waiting = false, hadBusy = false, lastVersion;
function label(s) {
  if (s.busy || ['error','interrupted'].includes(s.phase)) return phases[s.phase] || '正在更新';
  if (s.check_failed) return '检查未完成：可能是网络问题、main 历史被改写或该提交此前失败。当前版本保持不变。';
  return ({none:'已检查，没有新的可用版本', ok:'更新成功', install:'安装成功',
    rehearsal_failed:'新版本检查失败，继续使用原版本', health_failed_rolled_back:'新版本启动失败，已恢复原版本和数据',
    rolled_back:'恢复原版本后仍未通过检查，需要在电脑上排查'})[s.result] || '可以检查并更新到 GitHub main 最新代码';
}
async function refresh() {
  if (polling || !$('#home-update-status')) return;
  polling = true;
  try {
    const app = window.noyiApp;
    const s = await app.api('/api/home-update');
    if (!$('#home-update-status')) return;
    if (!s.available) { $('#home-update-status').textContent='此安装未启用家庭更新服务。'; $('#home-update-confirm').disabled=true; return; }
    if (s.busy) { hadBusy = true; waiting = false; }
    if (!s.busy && waiting) waiting = false;
    $('#home-update-status').textContent=label(s);
    $('#home-update-version').textContent='当前版本：'+(s.current || '未知');
    $('#home-update-time').textContent=s.checked?'最近检查：'+s.checked:'';
    $('#home-update-confirm').disabled=s.busy || !s.supervisor_online;
    if (!s.supervisor_online) $('#home-update-status').textContent='更新守护进程未运行';
    if (hadBusy && !s.busy && lastVersion && s.current !== lastVersion) $('#home-update-reload').hidden=false;
    lastVersion ||= s.current;
  } catch {
    if ($('#home-update-status')) $('#home-update-status').textContent='连接暂时中断，正在等待服务恢复，请勿重复提交。';
  } finally { polling=false; }
}
function open() {
  clearInterval(timer); waiting=false; hadBusy=false; lastVersion=null;
  window.noyiApp.modal('更新 Noyi', `<p id="home-update-version">正在读取版本…</p><p id="home-update-status" role="status">正在检查状态…</p><small id="home-update-time"></small>
    <p class="modal-note">从 GitHub main 获取最新代码，无需发布标签。更新前自动检查和备份；切换时会短暂断开连接。请先保存正在输入的内容，并尽量等当前任务结束后更新。</p>
    <label><input type="checkbox" id="home-update-agree"> 我确认更新到 main 最新代码</label>
    <div class="modal-actions"><button class="button primary" id="home-update-confirm" disabled>检查并更新</button><button class="button" id="home-update-reload" hidden>刷新使用新版</button></div>`);
  refresh(); timer=setInterval(()=> { if (!$('#home-update-status')) { clearInterval(timer); return; } refresh(); },2500);
}
document.addEventListener('click', async e => {
  const b=e.target.closest('button'); if (!b) return;
  if(b.id==='open-home-update') open();
  if(b.id==='home-update-reload') location.reload();
  if(b.id==='home-update-confirm') {
    if(!$('#home-update-agree').checked) { window.noyiApp.toast('请先勾选更新确认'); return; }
    b.disabled=true; waiting=true; hadBusy=true;
    try { await window.noyiApp.api('/api/home-update',{confirm:true}); $('#home-update-status').textContent='更新请求已提交，可以保持此页查看进度'; }
    catch(e) { window.noyiApp.toast(e.message); waiting=false; }
    refresh();
  }
});
// Keep this entry independent of desktop-only model settings and phone layout.
const ready=setInterval(()=> {
  if(!window.noyiApp || !window.noyiBoot || !$('.chat-header')) return;
  clearInterval(ready);
  if(document.documentElement.dataset.static === '1') return;
  const button=document.createElement('button'); button.id='open-home-update'; button.className='button';
  button.setAttribute('aria-label','更新 Noyi'); button.title='更新 Noyi'; button.textContent='更新';
  $('.chat-header').insertBefore(button, $('#show-details'));
},200);
