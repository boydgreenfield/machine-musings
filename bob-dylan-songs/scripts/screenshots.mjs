// Headless screenshots of each layout, used to eyeball the visuals.
// usage: node scripts/screenshots.mjs [outDir] [baseUrl]
import { chromium } from 'playwright-core';
import { mkdirSync } from 'node:fs';
import { join } from 'node:path';

const out = process.argv[2] || 'shots';
const base = process.argv[3] || 'http://localhost:5173/';
mkdirSync(out, { recursive: true });
const exe = process.env.CHROME_PATH || '/opt/pw-browsers/chromium-1194/chrome-linux/chrome';
const browser = await chromium.launch({ executablePath: exe, args: ['--use-gl=angle', '--use-angle=swiftshader', '--enable-unsafe-swiftshader', '--ignore-gpu-blocklist'] });
const page = await browser.newPage({ viewport: { width: 1440, height: 900 }, deviceScaleFactor: 1 });
page.on('console', (m) => { if (m.type() === 'error' || m.type() === 'warning') console.log('[console]', m.type(), m.text()); });
page.on('pageerror', (e) => console.log('[pageerror]', e.message));
await page.goto(base, { waitUntil: 'load' });
await page.waitForFunction(() => window.__ready === true, null, { timeout: 60000 });
await page.screenshot({ path: join(out, '00-intro.png') });
await page.evaluate(() => window.__app.enter());
await page.waitForTimeout(1500);
const settle = async () => { await page.evaluate(() => window.__app.settle()); await page.waitForTimeout(900); };
await settle();
const shots = process.argv[4] ? process.argv[4].split(',') : ['galaxy', 'highway', 'records', 'lexicon'];
for (const m of shots) {
  await page.evaluate((mode) => window.__app.setMode(mode), m);
  await settle();
  await page.screenshot({ path: join(out, `${m}.png`) });
  console.log('shot', m);
}
// a song focus + panel
await page.evaluate(() => window.__app.selectSong(window.__app.findSong('Visions of Johanna')));
await settle();
await page.screenshot({ path: join(out, 'song.png') });
await page.evaluate(() => window.__app.setMode('highway'));
await settle();
await page.screenshot({ path: join(out, 'song-highway.png') });
await page.evaluate(() => window.__app.selectAlbum(window.__app.catalog.albums[14]));
await settle();
await page.screenshot({ path: join(out, 'album-highway.png') });
// hover tooltip: move the mouse to the projected position of a song
await page.evaluate(() => window.__app.clearSelection());
await page.evaluate(() => window.__app.setMode('records'));
await settle();
const pt = await page.evaluate(() => {
  const app = window.__app; const s = app.findSong('Like a Rolling Stone');
  const v = app.stage.camera; const p = new (Object.getPrototypeOf(app.stage.camera.position).constructor)();
  const L = app.layouts.records.targets; p.set(L[s.index * 3], L[s.index * 3 + 1], L[s.index * 3 + 2]).project(v);
  return [(p.x * 0.5 + 0.5) * innerWidth, (1 - (p.y * 0.5 + 0.5)) * innerHeight];
});
await page.mouse.move(pt[0], pt[1]);
await page.waitForTimeout(900);
await page.screenshot({ path: join(out, 'hover.png') });
// era filter + tour
await page.mouse.move(5, 5);
await page.evaluate(() => window.__app.clearSelection());
await page.evaluate(() => window.__app.setMode('highway'));
await page.click('.era[data-era="electric"]');
await settle();
await page.screenshot({ path: join(out, 'filter.png') });
await page.keyboard.press('Escape');
await page.evaluate(() => window.__app.toggleTour());
await settle();
await page.screenshot({ path: join(out, 'tour.png') });
await page.evaluate(() => window.__app.toggleTour());
await page.evaluate(() => window.__app.clearSelection());
await page.setViewportSize({ width: 390, height: 844 });
await page.evaluate(() => window.__app.setMode('galaxy'));
await settle();
await page.screenshot({ path: join(out, 'mobile.png') });
await browser.close();
