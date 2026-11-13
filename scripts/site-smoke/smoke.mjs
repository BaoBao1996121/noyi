// Smoke check for the static demo site, in a real headless browser.
//
//   node scripts/site-smoke/smoke.mjs <base-url> <report-dir>
//
// Passes only if: the landing page and the demo app load with no console errors
// and no failed requests; the "watch the missed deadline" button opens the
// overdue task's replay and it actually plays; the memory galaxy draws.
// Every check prints why it failed; console logs, failed requests and
// screenshots are written to <report-dir> for the CI artifact.
import {chromium} from 'playwright';
import {mkdirSync, writeFileSync} from 'node:fs';
import {join} from 'node:path';

const base = (process.argv[2] || '').replace(/\/?$/, '/');
const dir = process.argv[3] || 'smoke-report';
if (!/^https?:\/\//.test(base)) {console.error('usage: smoke.mjs <base-url> <report-dir>'); process.exit(2);}
mkdirSync(dir, {recursive: true});

const log = [], failed = [], problems = [];
const fail = message => {problems.push(message); console.error('FAIL ' + message);};
const pass = message => console.log('ok   ' + message);

const browser = await chromium.launch(process.env.NOYI_CHROMIUM ? {executablePath: process.env.NOYI_CHROMIUM} : {});
const page = await browser.newPage({viewport: {width: 1440, height: 900}, locale: 'zh-CN'});
page.on('console', m => {log.push(`[${m.type()}] ${m.text()}`); if (m.type() === 'error') fail(`console error on ${page.url()}: ${m.text()}`);});
page.on('pageerror', e => {log.push(`[pageerror] ${e.message}`); fail(`page error on ${page.url()}: ${e.message}`);});
page.on('requestfailed', r => {failed.push(`${r.url()} ${r.failure()?.errorText}`); fail(`request failed: ${r.url()} ${r.failure()?.errorText}`);});
page.on('response', r => {if (r.status() >= 400) {failed.push(`${r.status()} ${r.url()}`); fail(`HTTP ${r.status()}: ${r.url()}`);}});

async function step(name, fn) {
  try {await fn(); pass(name);}
  catch (error) {fail(`${name}: ${error.message.split('\n')[0]}`);}
  await page.screenshot({path: join(dir, name.replace(/[^a-z0-9]+/gi, '-') + '.png')}).catch(() => {});
}

await step('landing', async () => {
  await page.goto(base, {waitUntil: 'networkidle'});
  const title = await page.textContent('h1');
  if (!title?.includes('说到做到')) throw new Error(`landing h1 is "${title}"`);
  const broken = await page.$$eval('img', imgs => imgs.filter(i => !i.complete || !i.naturalWidth).map(i => i.getAttribute('src')));
  if (broken.length) throw new Error(`images did not load: ${broken.join(', ')}`);
  const href = await page.getAttribute('#play-overdue', 'href');
  if (!/task=[0-9a-f]+/.test(href || '')) throw new Error(`overdue button has no task link (href=${href}); build.json stories missing?`);
});

await step('landing-english', async () => {
  await page.click('#lang');
  const title = await page.textContent('h1');
  if (!/keeps its word/.test(title || '')) throw new Error(`English h1 is "${title}"`);
  await page.click('#lang');
});

await step('overdue-replay-plays', async () => {
  await page.click('#play-overdue');
  await page.waitForURL(/app\/\?task=/);
  await page.waitForSelector('.replay-outcome.bad', {timeout: 15000});
  const outcome = await page.textContent('.replay-outcome');
  if (!outcome.includes('逾期')) throw new Error(`replay outcome is "${outcome}"`);
  const start = Number(await page.getAttribute('.replay', 'data-position'));
  await page.waitForTimeout(2500);
  const later = Number(await page.getAttribute('.replay', 'data-position'));
  if (!(later > start)) throw new Error(`replay did not advance (position ${start} → ${later})`);
});

await step('demo-read-only', async () => {
  if (!(await page.isDisabled('#message-input'))) throw new Error('message box is writable in the static demo');
  const bar = await page.textContent('.static-bar');
  if (!bar.includes('只读演示')) throw new Error('read-only notice missing');
});

await step('memory-galaxy', async () => {
  await page.goto(base + 'app/?tab=memory&view=galaxy', {waitUntil: 'networkidle'});
  await page.waitForSelector('.galaxy canvas, canvas.galaxy-canvas', {timeout: 15000});
  const box = await page.locator('.galaxy canvas, canvas.galaxy-canvas').first().boundingBox();
  if (!box || box.width < 100) throw new Error('galaxy canvas not drawn');
});

await browser.close();
writeFileSync(join(dir, 'console.log'), log.join('\n') + '\n');
writeFileSync(join(dir, 'failed-requests.log'), failed.join('\n') + '\n');
writeFileSync(join(dir, 'result.json'), JSON.stringify({base, problems}, null, 1));
if (problems.length) {console.error(`\n${problems.length} problem(s); see ${dir}/`); process.exit(1);}
console.log('\nsmoke check passed: ' + base);
