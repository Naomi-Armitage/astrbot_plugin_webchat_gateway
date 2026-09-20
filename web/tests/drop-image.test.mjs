import assert from 'node:assert/strict';
import test from 'node:test';
import { isDropImage } from '../src/chat_client/drop-image.ts';

test('legacy JPEG files and uploads without a browser MIME still request previews', () => {
  for (const filename of ['photo.jpeg', 'PHOTO.JPG', 'photo.jpe', 'photo.jepg']) {
    for (const mime of [undefined, '', 'application/octet-stream', 'image/jpg']) {
      assert.equal(isDropImage(mime, filename), true, `${mime}: ${filename}`);
    }
  }
  assert.equal(isDropImage(' IMAGE/JPEG; charset=binary ', 'blob'), true);
  assert.equal(isDropImage('application/octet-stream', 'photo.png'), true);
  assert.equal(isDropImage('image/webp'), true);
});

test('ordinary files and executable image formats remain file bubbles', () => {
  for (const filename of ['archive.zip', 'photo.jpg.exe', 'image.svg', 'README', 'photo.jpeg.html']) {
    assert.equal(isDropImage('application/octet-stream', filename), false);
  }
  assert.equal(isDropImage('image/svg+xml', 'image.svg'), false);
});
