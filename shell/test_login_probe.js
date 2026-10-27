'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const html = fs.readFileSync(path.join(__dirname, 'renderer', 'index.html'), 'utf8');
assert.ok(html.includes('src="/workspace"'));
assert.ok(!/executeJavaScript|runBatch|<webview|persist:boss/.test(html));
console.log('PASS: desktop hosts the same-origin kernel workspace without platform automation');
