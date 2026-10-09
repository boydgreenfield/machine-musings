// Headless screenshot helper: `pnpm screenshot [url] [out-prefix]`
// Captures each layout and a selected-node view so changes can be eyeballed.
import { chromium } from 'playwright-core';
import { mkdirSync } from 'node:fs';

const url = process.argv[2] ?? 'http://localhost:4173/';
const prefix = process.argv[3] ?? 'screenshots/atlas';
mkdirSync('screenshots', { recursive: true });

const browser = await chromium.launch({
  executablePath: process.env.CHROMIUM_PATH ?? '/opt/pw-browsers/chromium-1194/chrome-linux/chrome',
  args: ['--no-proxy-server', '--use-gl=angle', '--use-angle=swiftshader', '--enable-unsafe-swiftshader', '--ignore-gpu-blocklist', '--no-sandbox'],
});
const page = await browser.newPage({ viewport: { width: 1600, height: 1000 }, deviceScaleFactor: 1 });
page.on('console', (m) => { if (m.type() === 'error' || m.type() === 'warning') console.log(`[browser:${m.type()}]`, m.text()); });
page.on('pageerror', (e) => console.log('[pageerror]', e.message));
await page.goto(url, { waitUntil: 'load' });
// Wait until transitions & camera flights are done, then give labels a moment to fade in/out.
async function settle(extraSim = 1.2) {
  await page.waitForFunction(() => window.atlas.settled, null, { timeout: 60000 });
  const t0 = await page.evaluate(() => window.atlas.simTime);
  await page.waitForFunction((t) => window.atlas.simTime > t, t0 + extraSim, { timeout: 60000 });
}
await settle();
await page.screenshot({ path: `${prefix}-intro.png` });
await page.click('#intro-explore');
await settle();
await page.screenshot({ path: `${prefix}-orbits.png` });

const steps = [
  ['constellation', '2'], ['tower', '3'], ['systems', '4'],
];
for (const [name, key] of steps) {
  await page.keyboard.press(key);
  await settle();
  await page.screenshot({ path: `${prefix}-${name}.png` });
}
await page.keyboard.press('1');
await settle();
await page.evaluate(() => { const w = window; w.atlas.events.onSelect(w.graph.byId.get('internal-medicine')); });
await settle();
await page.screenshot({ path: `${prefix}-selected.png` });
await page.evaluate(() => { const w = window; w.atlas.events.onSelect(w.graph.byId.get('hospice-palliative')); });
await settle();
await page.screenshot({ path: `${prefix}-bridge.png` });
await page.keyboard.press('Escape');
await page.keyboard.press('c');
await settle(1.5);
await page.screenshot({ path: `${prefix}-domain-lens.png` });
await page.keyboard.press('c'); // training lens
await page.keyboard.press('3');
await settle();
await page.screenshot({ path: `${prefix}-tower-training.png` });
await page.keyboard.press('1');
await settle();
// Hover a node: tooltip + highlight
const hp = await page.evaluate(() => window.atlas.project(window.graph.byId.get('cardiology')));
if (hp) { await page.mouse.move(hp.x, hp.y); await settle(0.6); await page.screenshot({ path: `${prefix}-hover.png` }); }
await page.mouse.move(5, 500);
// Search
await page.keyboard.press('/');
await page.keyboard.type('onco');
await settle(1.0);
await page.screenshot({ path: `${prefix}-search.png` });
await page.keyboard.press('Escape'); await page.keyboard.press('Escape');
// Tour
await page.keyboard.press('t');
await settle();
await page.screenshot({ path: `${prefix}-tour.png` });
await page.keyboard.press('Escape');
await page.keyboard.press('r');
await page.setViewportSize({ width: 420, height: 820 });
await page.keyboard.press('r');
await settle();
await page.screenshot({ path: `${prefix}-mobile.png` });
await page.evaluate(() => { const w = window; w.atlas.events.onSelect(w.graph.byId.get('pediatrics')); });
await settle();
await page.screenshot({ path: `${prefix}-mobile-selected.png` });
await browser.close();
console.log('screenshots written to', prefix + '-*.png');
