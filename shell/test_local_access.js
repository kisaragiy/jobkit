'use strict';
// Run the actual Electron main module with a fake runtime; never starts Electron.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function boot(origin = 'http://127.0.0.1:18770', rejectLoad = false) {
  const ipc = {}, events = {}, requested = [], external = [], loaded = [], timers = [], errors = [];
  let quits = 0;
  let window;
  class FakeWindow {
    constructor() {
      window = this;
      this.webContents = {
        on: (name, fn) => { events[name] = fn; },
        setWindowOpenHandler: fn => { events.open = fn; },
        session: { cookies: { get: async () => [{ value: 'synthetic-session' }] } },
      };
    }
    once() {}
    on(name, fn) { events['window-' + name] = fn; }
    isDestroyed() { return false; }
    loadURL(url) { loaded.push(url); return rejectLoad ? Promise.reject(Object.assign(new Error('private diagnostic must not reach UI'), {code:'ERR_FAILED'})) : Promise.resolve(); }
  }
  const electron = {
    app: {
      commandLine: { appendSwitch() {} }, requestSingleInstanceLock: () => true,
      whenReady: () => ({ then: fn => fn() }), on() {}, quit() { quits++; },
    },
    BrowserWindow: FakeWindow,
    Menu: { buildFromTemplate: x => x, setApplicationMenu() {} },
    shell: { openExternal: url => external.push(url) },
    ipcMain: { handle: (name, fn) => { ipc[name] = fn; } },
    dialog: { showErrorBox: (title, message) => errors.push({title, message}) },
    session: { fromPartition: () => ({
      cookies: { get: async () => [
        { domain: '.zhipin.com', name: 'synthetic', value: 'not-real' },
        { domain: 'zhipin.com.attacker.invalid', name: 'unrelated', value: 'not-real' },
      ] },
      getUserAgent: () => 'synthetic',
    }) },
  };
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, 'main.js'), 'utf8'), {
    require: name => name === 'electron' ? electron : require(name),
    process: { env: { JOBKIT_URL: origin }, platform: 'win32' }, __dirname,
    URL, setTimeout: fn => {timers.push(fn);return timers.length;}, clearTimeout(id) {if(id)timers[id-1]=null;},
    fetch: async (url, options) => { requested.push({ url, options }); return { json: async () => ({ ok: true }) }; },
  });
  return { ipc, events, requested, external, loaded, window, timers, errors, quits:()=>quits };
}

(async () => {
  assert.throws(() => boot('https://audit.invalid'), /loopback/);
  assert.throws(() => boot('http://127.0.0.1:18770/elsewhere'), /loopback/);
  const runtime = boot('http://localhost:19999');
  assert.equal(runtime.loaded[0], 'http://localhost:19999/desktop');
  const handler = runtime.ipc['boss:import-login'];
  let result = await handler({ sender: {}, senderFrame: { url: 'http://localhost:19999/desktop' } });
  assert.equal(result.ok, false);
  result = await handler({ sender: runtime.window.webContents, senderFrame: { url: 'https://www.zhipin.com/' } });
  assert.equal(result.ok, false);
  assert.equal(runtime.requested.length, 0);
  result = await handler({ sender: runtime.window.webContents, senderFrame: { url: 'http://localhost:19999/desktop' } });
  assert.equal(result.ok, false);
  assert.equal(result.code, 'CHANNEL_NOT_REGISTERED');
  assert.equal(runtime.requested.length, 0, 'Retired login bridge must not read or import shared credentials');
  let blocked = false;
  runtime.events['will-navigate']({ preventDefault() { blocked = true; } }, 'https://audit.invalid');
  assert.equal(blocked, true);
  runtime.events.open({ url: 'file:///C:/Windows/system.ini' });
  assert.equal(runtime.external.length, 0);
  runtime.events.open({ url: 'https://example.com/' });
  assert.equal(runtime.external[0], 'https://example.com/');
  const failed = boot('http://localhost:19999', true);
  for(let i=0;i<3;i++) {
    for(let tick=0;tick<5;tick++)await Promise.resolve();
    const retry=failed.timers.shift();if(retry)retry();
  }
  assert.equal(failed.loaded.length,3,'startup failures have a finite retry budget');
  assert.equal(failed.errors.length,1);
  assert.equal(failed.quits(),1);
  assert.ok(!JSON.stringify(failed.errors).includes('private diagnostic'));
  assert.ok(!JSON.stringify(failed.errors).includes('ERR_FAILED'));
  const renderer = boot();
  renderer.events['render-process-gone']({}, {reason:'launch-failed',exitCode:49});
  renderer.events['render-process-gone']({}, {reason:'launch-failed',exitCode:49});
  assert.equal(renderer.errors.length,1,'renderer failure cannot open a burst of dialogs');
  assert.equal(renderer.quits(),1);
  const cleanExit = boot();
  cleanExit.events['render-process-gone']({}, {reason:'clean-exit',exitCode:0});
  assert.equal(cleanExit.errors.length,0,'normal renderer exit must not show a failure alert');
  assert.equal(cleanExit.quits(),0);
  const closed = boot('http://localhost:19999',true);
  for(let tick=0;tick<5;tick++)await Promise.resolve();
  closed.events['window-closed']();
  assert.equal(closed.timers[0],null,'closing the window clears pending retries');
  console.log('PASS: same-origin desktop, custom port, IPC sender checks and external scheme filtering');
})().catch(error => { console.error(error); process.exitCode = 1; });
