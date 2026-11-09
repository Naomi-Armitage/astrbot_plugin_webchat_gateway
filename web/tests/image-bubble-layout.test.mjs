// Browser layout regression for single-image bubbles. Renders the real
// styles.css with the markup main.ts produces (--msg-image-ar on the bubble)
// and asserts widths/alignment. Skips when Playwright or a browser binary is
// unavailable, since it is not a declared dependency.
import assert from 'node:assert/strict';
import test from 'node:test';
import { readFileSync } from 'node:fs';

const css = readFileSync(new URL('../src/chat_client/styles.css', import.meta.url), 'utf8');

let chromium = null;
let skipReason = false;
try {
  ({ chromium } = await import('playwright'));
} catch {
  skipReason = 'playwright not installed';
}

const GIF = 'data:image/gif;base64,R0lGODlhAQABAAAAACw=';
const CAPTION = '配文 caption text that is long enough to wrap onto several lines';

// [id, role, ar (w/h), caption?]
const CASES = [
  ['strip-user-cap', 'user', 1 / 20, true],
  ['strip-bot-cap', 'bot', 1 / 20, true],
  ['strip-user-only', 'user', 1 / 20, false],
  ['p916-user-cap', 'user', 9 / 16, true],
  ['p916-bot-only', 'bot', 9 / 16, false],
  ['p34-user-cap', 'user', 3 / 4, true],
  ['land-user-cap', 'user', 16 / 9, true],
];

function markup() {
  const rows = CASES.map(([id, role, ar, cap]) => `
    <div class="msg-row ${role}-row" data-id="${id}">
      <div class="msg ${role} has-image${cap ? '' : ' has-image-only'}" style="--msg-image-ar:${ar}">
        <div class="msg-attachments cnt-1"><img class="msg-image is-loaded" style="aspect-ratio:${ar}" src="${GIF}"></div>
        ${cap ? `<div class="msg-text">${CAPTION}</div>` : ''}
      </div>
    </div>`).join('');
  return `<!doctype html><meta charset="utf-8"><style>${css}</style>
    <body><main style="display:flex;flex-direction:column;height:100vh">
    <div class="message-list" style="display:flex;flex-direction:column">${rows}</div></main></body>`;
}

async function measure(browser, vw) {
  const page = await browser.newPage({ viewport: { width: vw, height: 900 } });
  await page.setContent(markup());
  const out = await page.$$eval('.msg-row', (rows) => Object.fromEntries(rows.map((row) => {
    const bubble = row.querySelector('.msg');
    const img = row.querySelector('img');
    const text = row.querySelector('.msg-text');
    const r = row.getBoundingClientRect();
    const b = bubble.getBoundingClientRect();
    const list = row.parentElement.getBoundingClientRect();
    return [row.dataset.id, {
      row: r.width, bubble: b.width, img: img.getBoundingClientRect().width,
      imgH: img.getBoundingClientRect().height,
      text: text ? text.getBoundingClientRect().width : null,
      leftGap: b.left - list.left, rightGap: list.right - b.right,
      overflow: b.right > list.right + 0.5 || b.left < list.left - 0.5,
    }];
  })));
  await page.close();
  return out;
}

const near = (actual, expected, msg) =>
  assert.ok(Math.abs(actual - expected) <= 1, `${msg}: expected ≈${expected}, got ${actual}`);

test('single-image bubbles size from aspect ratio, with a caption floor', { skip: skipReason }, async (t) => {
  let browser;
  try {
    browser = await chromium.launch();
  } catch (err) {
    t.skip(`chromium unavailable: ${err.message.split('\n')[0]}`);
    return;
  }
  try {
    for (const vw of [1280, 375, 320]) {
      const m = await measure(browser, vw);
      for (const [id, role, ar, cap] of CASES) {
        const s = m[id];
        const label = `${vw}px ${id}`;
        const natural = 320 * Math.min(ar, 1);
        const expected = Math.min(Math.max(natural, cap ? 200 : 0), s.row);
        assert.equal(s.overflow, false, `${label} overflows the list`);
        near(s.bubble, expected, `${label} bubble width`);
        near(s.img, s.bubble, `${label} image fills bubble`);
        assert.ok(s.imgH <= 320.5, `${label} image height ${s.imgH} exceeds cap`);
        if (cap) near(s.text, s.bubble, `${label} caption width`);
        if (role === 'user') assert.ok(s.rightGap < s.leftGap, `${label} user bubble should hug right`);
        else assert.ok(s.leftGap < s.rightGap, `${label} bot bubble should hug left`);
      }
      // Spot-check the concrete numbers the design relies on.
      near(m['p916-user-cap'].bubble, 200, `${vw}px 9:16 captioned gets the floor`);
      near(m['p916-bot-only'].bubble, 180, `${vw}px 9:16 image-only keeps natural width`);
      near(m['p34-user-cap'].bubble, 240, `${vw}px 3:4 above the floor is untouched`);
    }
  } finally {
    await browser.close();
  }
});
