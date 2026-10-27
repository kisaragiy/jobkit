'use strict';
const assert = require('node:assert/strict');
const delivery = require('./renderer/delivery-engine.js');
(async () => {
  for (const dryRun of [true, false]) {
    let calls = 0;
    const deps = new Proxy({}, { get() { return () => { calls++; throw new Error('Legacy loop must not invoke any tool'); }; } });
    const result = await delivery.runBatch(deps, { dryRun, allowJobIds: ['synthetic'] });
    assert.equal(result.reason, 'kernel-required');
    assert.equal(result.delivered, 0);
    assert.equal(calls, 0);
  }
  console.log('PASS: legacy browser batch loop cannot execute in preview or live mode');
})().catch(error => { console.error(error); process.exitCode = 1; });
