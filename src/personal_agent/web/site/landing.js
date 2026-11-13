// Landing page: language by browser default (switchable, remembered), and deep links that find the
// demo tasks by outcome in build.json, because task ids change with every build.
const KEY = 'noyi.lang';
const button = document.getElementById('lang');
function apply(lang) {
  document.documentElement.lang = lang === 'en' ? 'en' : 'zh-CN';
  document.querySelectorAll('[data-zh]').forEach(el => {el.textContent = el.dataset[lang] || el.dataset.zh;});
  button.textContent = lang === 'en' ? '中文' : 'EN';
  document.title = lang === 'en' ? 'Noyi · A personal agent that keeps its word' : 'Noyi · 说到做到的个人助理';
}
let saved = null;
try {saved = localStorage.getItem(KEY);} catch {}
let lang = saved || (/^zh\b/i.test(navigator.language || '') ? 'zh' : 'en');
apply(lang);
button.addEventListener('click', () => {lang = lang === 'en' ? 'zh' : 'en'; try {localStorage.setItem(KEY, lang);} catch {} apply(lang);});

try {
  const build = await (await fetch('build.json', {cache: 'no-cache'})).json();
  const byOutcome = outcome => Object.entries(build.stories || {}).find(([, o]) => o === outcome)?.[0];
  const overdue = byOutcome('overdue'), kept = byOutcome('fulfilled');
  if (overdue) document.getElementById('play-overdue').href = `app/?task=${overdue}&tab=replay&skin=pixel&play=1`;
  if (kept) document.getElementById('card-replay').href = `app/?task=${kept}&tab=replay&skin=pixel&play=1`;
  if (overdue) document.getElementById('card-proof').href = `app/?task=${overdue}&tab=replay`;
  document.querySelectorAll('#story-list li').forEach(li => {
    const id = byOutcome(li.dataset.outcome);
    if (id) li.addEventListener('click', () => {location.href = `app/?task=${id}&tab=replay&skin=pixel&play=1`;});
  });
  if (build.commit) document.getElementById('build').textContent = 'build ' + build.commit.slice(0, 7);
} catch (error) {
  console.error('build.json', error);
}
