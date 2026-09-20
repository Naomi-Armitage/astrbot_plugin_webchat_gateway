import assert from 'node:assert/strict';
import test from 'node:test';
import { PictureWindow } from '../src/chat_client/picture-window.ts';

const size = { width: 460, height: 680 };
function installApi(t, requestWindow) {
  const previous = globalThis.window;
  globalThis.window = requestWindow ? { documentPictureInPicture: { requestWindow } } : {};
  t.after(() => { if (previous === undefined) delete globalThis.window; else globalThis.window = previous; });
}
function deferred() {
  let resolve, reject;
  const promise = new Promise((ok, fail) => { resolve = ok; reject = fail; });
  return { promise, resolve, reject };
}

test('unsupported or denied PiP requests allow the in-page fallback', async t => {
  installApi(t);
  const picture = new PictureWindow(() => assert.fail('Should not close the conversation'), () => {});
  assert.equal(await picture.open({}, size), false);
  window.documentPictureInPicture = { requestWindow: async () => { throw new Error('NotAllowedError'); } };
  assert.equal(await picture.open({}, size), false);
  assert.equal(picture.window, null);
});

test('repeated clicks share a pending window request and closing cancels its result', async t => {
  const request = deferred();
  let calls = 0, closed = 0;
  installApi(t, () => { calls++; return request.promise; });
  const picture = new PictureWindow(() => assert.fail('Unexpected close callback'), () => assert.fail('Cancelled window mounted'));
  const pending = picture.open({}, size);
  assert.equal(picture.open({}, size), pending);
  assert.equal(calls, 1);
  picture.close();
  request.resolve({ close() { closed++; } });
  assert.equal(await pending, false);
  assert.equal(closed, 1);
  assert.equal(picture.window, null);
});

test('a late failure cannot cancel a newer window request', async t => {
  const first = deferred(), second = deferred();
  let calls = 0;
  installApi(t, () => (++calls === 1 ? first.promise : second.promise));
  const picture = new PictureWindow(() => {}, () => assert.fail('Cancelled window mounted'));
  const old = picture.open({}, size);
  picture.close();
  const current = picture.open({}, size);
  first.reject(new Error('Old request denied'));
  assert.equal(await old, false);
  assert.equal(picture.open({}, size), current);
  assert.equal(calls, 2);
  picture.close();
  second.resolve({ close() {} });
  assert.equal(await current, false);
});
