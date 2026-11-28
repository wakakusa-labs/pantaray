const assert = require('assert');
const fs = require('fs');
const path = require('path');
const { test } = require('node:test');

const { build } = require('../package.json');

test('electron-builder config avoids maximum compression for bundled local backend runtime', () => {
  assert.equal(build.compression, 'normal');
});

test('electron-builder config includes macOS tray status icons', () => {
  assert.ok(build.files.includes('assets/icons/Pantaray_tray_*.png'));
});

test('macOS build declares the screen recording purpose string and entitlement', () => {
  // Without both, macOS refuses the capture and `capture_screen` can only report
  // a missing permission the user has no way to grant.
  assert.equal(
    build.mac.extendInfo.NSScreenCaptureUsageDescription,
    'Pantaray captures your screen only when you ask an Action to look at it.'
  );
  const entitlements = fs.readFileSync(path.join(__dirname, '..', build.mac.entitlements), 'utf8');
  assert.ok(entitlements.includes('<key>com.apple.security.device.screen-capture</key>'));
});

test('macOS build bundles the transcription helper and declares its two purpose strings', () => {
  // macOS asks for the helper's microphone and system audio in Pantaray's name, with these.
  assert.match(build.mac.extendInfo.NSMicrophoneUsageDescription, /call/);
  assert.match(build.mac.extendInfo.NSAudioCaptureUsageDescription, /call/);
  // Beside the recorder, where its presence says the recorder can host transcription.
  assert.ok(build.extraResources.some(({ from, to }) => to === 'zanei/bin/zanei-meeting' &&
    from === '.generated/zanei/Zanei.app/Contents/MacOS/zanei-meeting'));
});
