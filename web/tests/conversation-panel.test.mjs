import assert from 'node:assert/strict';
import test from 'node:test';
import { effectivePanelLayout, fitPanelGeometry, parsePanelGeometry } from '../src/chat_client/conversation-panel.ts';

test('compact viewports fall back to full without discarding the preferred layout', () => {
  const preferred = 'dock';
  assert.deepEqual([1920, 900, 390, 1920].map(width => effectivePanelLayout(preferred, width)),
    ['dock', 'full', 'full', 'dock']);
  assert.equal(effectivePanelLayout('float', 719), 'full');
  assert.equal(effectivePanelLayout('float', 720), 'float');
  assert.equal(effectivePanelLayout('dock', 1100), 'dock');
});

test('dragging, resizing and stale saved positions keep the whole panel in the viewport', () => {
  for (const width of [1, 320, 390, 720, 1100, 1920, 3840]) {
    for (const height of [1, 200, 480, 1080]) {
      for (const left of [-5000, 0, 350, 5000]) {
        for (const size of [1, 320, 8000]) {
          const geometry = fitPanelGeometry({ left, top: left, width: size, height: size }, { width, height });
          assert.ok(geometry.width > 0 && geometry.height > 0);
          assert.ok(geometry.left >= 0 && geometry.top >= 0);
          assert.ok(geometry.left + geometry.width <= width);
          assert.ok(geometry.top + geometry.height <= height);
          assert.deepEqual(fitPanelGeometry(geometry, { width, height }), geometry);
        }
      }
    }
  }
});

test('saved geometry rejects malformed values and accepts legacy pixel coordinates', () => {
  for (const raw of [null, '', 'null', '{', '[]', 'true', '{"left":0,"top":0,"width":-2,"height":300}',
    '{"left":"0","top":0,"width":400,"height":300}', '{"left":0,"top":0,"width":1e999,"height":300}']) {
    assert.equal(parsePanelGeometry(raw), null);
  }
  const saved = { left: 100, top: 50, width: 460, height: 680 };
  assert.deepEqual(parsePanelGeometry(JSON.stringify(saved)), saved);
});
