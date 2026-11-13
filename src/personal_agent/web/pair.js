const form = document.getElementById('pair-form'), input = document.getElementById('code'), status = document.getElementById('pair-status');
async function pair(code) {
  status.className = 'status'; status.textContent = '正在配对…';
  try {
    const res = await fetch('/api/pair', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({code})});
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.error || '配对没有成功');
    status.className = 'status ok'; status.textContent = '配对好了，正在打开…';
    history.replaceState(null, '', '/'); setTimeout(() => location.replace('/'), 600);
  } catch (e) { status.textContent = e.message; }
}
form.onsubmit = e => { e.preventDefault(); pair(input.value); };
const fromLink = new URLSearchParams(location.search).get('pair');
if (fromLink) { input.value = fromLink; pair(fromLink); }
