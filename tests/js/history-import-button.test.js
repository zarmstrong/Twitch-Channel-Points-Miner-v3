'use strict';

// The Config tab's Import button for the analytics data (assets/history.js).
// Run with: node tests/js/history-import-button.test.js

const assert = require('node:assert/strict');
const test = require('node:test');
const path = require('node:path');
const { createImportControl, importControlState, importPollDelay } = require(
  path.join(__dirname, '..', '..', 'assets', 'history.js'),
);

const status = (fields) => ({
  boundary: 1767225600,
  imported: false,
  streamers_done: 0,
  streamers_waiting: 2,
  automatic: false,
  requested: false,
  complete: false,
  ...fields,
});

test('an import not done shows the button', () => {
  const view = importControlState(status());
  assert.equal(view.showButton, true);
  assert.equal(view.polling, false);
  assert.match(view.text, /2 streamers have not been imported/);
  assert.match(importControlState(status({ streamers_waiting: 1 })).text, /1 streamer has/);
});

test('streamers not listed yet still show the button', () => {
  const view = importControlState(status({ streamers_waiting: null }));
  assert.equal(view.showButton, true);
  assert.match(view.text, /has not been imported/);
});

test('a finished import hides the button', () => {
  for (const automatic of [false, true]) {
    const view = importControlState(status({ complete: true, streamers_waiting: 0, automatic }));
    assert.equal(view.showButton, false);
    assert.equal(view.polling, false);
    assert.match(view.text, /has been imported/);
  }
});

test('a running import hides the button and keeps polling', () => {
  for (const fields of [{ requested: true }, { automatic: true }]) {
    const view = importControlState(status(fields));
    assert.equal(view.showButton, false);
    assert.equal(view.polling, true);
    assert.match(view.text, /Importing.*2 streamers left/);
  }
});

test('a requested import before its first step says it is importing', () => {
  // The boundary is saved by the import's first step, not the request.
  const view = importControlState(status({ requested: true, boundary: null }));
  assert.equal(view.showButton, false);
  assert.equal(view.polling, true);
  assert.match(view.text, /^Importing/);
});

test('no status (History not running) offers no button', () => {
  const view = importControlState(null);
  assert.equal(view.showButton, false);
  assert.equal(view.polling, false);
  assert.match(view.text, /not running/);
});

test('the status is read again: soon while importing, slowly otherwise', () => {
  assert.equal(importPollDelay({ polling: true }, false), 5000);
  assert.equal(importPollDelay({ polling: false }, false), 30000);
  assert.equal(importPollDelay(null, true), 15000);
});

// A control whose requests and timers the test settles by hand.
function harness() {
  const calls = { fetch: [], post: [], renders: [], timers: new Map(), nextTimer: 1 };
  const pending = (list) => () => {
    let settle;
    const promise = new Promise((resolve, reject) => { settle = { resolve, reject }; });
    list.push(settle);
    promise.abort = () => calls.abortNext && calls.abortNext();
    return promise;
  };
  const control = createImportControl({
    fetchStatus: pending(calls.fetch),
    postImport: pending(calls.post),
    render: (view) => calls.renders.push(view),
    setTimer: (fn, ms) => {
      const id = calls.nextTimer++;
      calls.timers.set(id, { fn, ms });
      return id;
    },
    clearTimer: (id) => calls.timers.delete(id),
  });
  const timer = () => {
    assert.equal(calls.timers.size, 1, 'one timer pending');
    return [...calls.timers.values()][0];
  };
  const fire = () => {
    const [[id, entry]] = calls.timers;
    calls.timers.delete(id);
    entry.fn();
  };
  const last = () => calls.renders[calls.renders.length - 1];
  return { calls, control, timer, fire, last };
}

test('a finished import keeps being read, so a new streamer shows the button', async () => {
  const h = harness();
  const shown = h.control.show();
  h.calls.fetch[0].resolve(status({ complete: true, streamers_waiting: 0 }));
  await shown;
  assert.equal(h.last().showButton, false);
  assert.equal(h.timer().ms, 30000);

  h.fire();
  h.calls.fetch[1].resolve(status({ streamers_waiting: 1 }));
  await Promise.resolve(); await Promise.resolve();
  assert.equal(h.last().showButton, true);
});

test('a failed read is tried again', async () => {
  const h = harness();
  const shown = h.control.show();
  h.calls.fetch[0].reject(new Error('History did not respond'));
  await shown;
  assert.equal(h.last().showButton, false);
  assert.equal(h.last().text, 'History did not respond');
  assert.equal(h.timer().ms, 15000);

  h.fire();
  h.calls.fetch[1].resolve(status({ requested: true }));
  await Promise.resolve(); await Promise.resolve();
  assert.equal(h.last().polling, true);
  assert.equal(h.timer().ms, 5000);
});

test('starting the import shows its progress', async () => {
  const h = harness();
  const started = h.control.start();
  h.calls.post[0].resolve(status({ requested: true }));
  await started;
  assert.equal(h.last().showButton, false);
  assert.match(h.last().text, /^Importing/);
  assert.equal(h.timer().ms, 5000);
});

test('a failed start hides the button until the status is read again', async () => {
  const h = harness();
  const started = h.control.start();
  h.calls.post[0].reject(new Error('History is not running'));
  await started;
  assert.equal(h.last().showButton, false);
  assert.match(h.last().text, /Unable to start the import: History is not running/);
  assert.equal(h.timer().ms, 5000);

  h.fire();
  h.calls.fetch[0].resolve(status());
  await Promise.resolve(); await Promise.resolve(); await Promise.resolve();
  assert.equal(h.last().showButton, true);
});

test('a read that never answers times out, is aborted and tried again', async () => {
  const h = harness();
  let aborted = false;
  h.calls.abortNext = () => { aborted = true; };
  const shown = h.control.show();
  const timeout = h.timer();
  assert.equal(timeout.ms, 30000);
  h.fire();
  await shown;
  assert.equal(aborted, true);
  assert.equal(h.last().text, 'History did not respond in time.');
  assert.equal(h.timer().ms, 15000);

  // The late answer is ignored.
  h.calls.fetch[0].resolve(status({ complete: true, streamers_waiting: 0 }));
  await Promise.resolve(); await Promise.resolve();
  assert.equal(h.calls.renders.length, 1);
});

test('a start that never answers times out without offering the button', async () => {
  const h = harness();
  const started = h.control.start();
  assert.equal(h.timer().ms, 30000);
  h.fire();
  await started;
  assert.equal(h.last().showButton, false);
  assert.match(h.last().text, /did not respond in time/);
  assert.equal(h.timer().ms, 5000);
});

test('only the newest read renders', async () => {
  const h = harness();
  const first = h.control.show();
  const second = h.control.load();
  h.calls.fetch[1].resolve(status({ complete: true, streamers_waiting: 0 }));
  await second;
  h.calls.fetch[0].resolve(status());
  await first;
  assert.equal(h.calls.renders.length, 1);
  assert.equal(h.last().showButton, false);
});

test('leaving the Config tab stops reading', async () => {
  const h = harness();
  const shown = h.control.show();
  h.control.hide();
  h.calls.fetch[0].resolve(status());
  await shown;
  assert.equal(h.calls.renders.length, 0);
  assert.equal(h.calls.timers.size, 0);
  h.control.later(10000);
  assert.equal(h.calls.timers.size, 0);
});

test('a config change reads again later while visible', async () => {
  const h = harness();
  const shown = h.control.show();
  h.calls.fetch[0].resolve(status({ complete: true, streamers_waiting: 0 }));
  await shown;
  h.control.later(10000);
  assert.equal(h.timer().ms, 10000);
});
