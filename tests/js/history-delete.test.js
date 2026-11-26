'use strict';

// The History tab's Delete history flow (assets/history.js), driven without a
// browser: the DELETE, the 30-second timer and the page are all fakes.
// Run with: node tests/js/history-delete.test.js

const assert = require('node:assert/strict');
const test = require('node:test');
const path = require('node:path');
const {
  REQUEST_TIMEOUT_MS,
  deleteButtonState,
  detailMessageFor,
  runDelete,
} = require(path.join(__dirname, '..', '..', 'assets', 'history.js'));

// The parts of the tab's state the flow reads and changes.
function tabState(detailStreamer = '') {
  return {
    deleting: null,
    lastDelete: null,
    detailMessage: null,
    detailStreamer,
    notice: '',
  };
}

// A page whose DELETEs answer when the test says so.  `calls` records what
// the flow did to the page, in order.
function fakePage(s) {
  const calls = [];
  const sent = [];
  const timers = new Map();
  let nextTimer = 1;
  const ui = {
    send(streamer) {
      calls.push(['send', streamer]);
      let answer;
      let fail;
      const promise = new Promise((resolve, reject) => {
        answer = resolve;
        fail = reject;
      });
      sent.push({ streamer, answer, fail });
      return promise;
    },
    renderHead() { calls.push(['renderHead']); },
    status() { calls.push(['status', s.notice]); },
    reload() { calls.push(['reload']); },
    // Like the page: forgetting the streamer the drill-down shows closes it.
    forget(streamer) {
      calls.push(['forget', streamer]);
      if (s.detailStreamer.toLowerCase() === streamer.toLowerCase()) {
        s.detailStreamer = '';
        s.detailMessage = null;
      }
    },
    setTimeout(fn, ms) {
      const id = nextTimer++;
      timers.set(id, { fn, ms });
      return id;
    },
    clearTimeout(id) { timers.delete(id); },
  };
  return {
    ui,
    calls,
    sent,
    timers,
    // Runs every timer still waiting, as if REQUEST_TIMEOUT_MS passed.
    expire() {
      for (const [id, timer] of [...timers]) {
        timers.delete(id);
        timer.fn();
      }
    },
  };
}

// Lets the flow's promise callbacks run.
function settle() {
  return new Promise((resolve) => setImmediate(resolve));
}

const REMOVED = { deleted: { daily_stats: 3, events: 2 } };

test('a delete that succeeds says so and forgets the streamer', async () => {
  const s = tabState('alice');
  const page = fakePage(s);
  const operation = runDelete(s, 'alice', page.ui);
  await settle();
  assert.deepEqual(operation, { streamer: 'alice' });
  assert.equal(s.deleting, operation);
  assert.deepEqual(page.calls, [['renderHead'], ['send', 'alice']]);
  assert.equal(page.timers.size, 1);
  assert.equal([...page.timers.values()][0].ms, REQUEST_TIMEOUT_MS);

  page.sent[0].answer(REMOVED);
  await settle();
  assert.equal(s.deleting, null);
  assert.equal(s.notice, 'Deleted the History for alice.');
  assert.deepEqual(page.calls.slice(2), [
    ['status', 'Deleted the History for alice.'],
    ['forget', 'alice'],
    ['renderHead'],
  ]);
  assert.equal(page.timers.size, 0, 'the 30-second timer is cleared');
});

test('a delete that removed nothing says there was nothing to delete', async () => {
  const s = tabState('alice');
  const page = fakePage(s);
  runDelete(s, 'alice', page.ui);
  await settle();
  page.sent[0].answer({ deleted: { daily_stats: 0, events: 0 } });
  await settle();
  assert.equal(s.notice, 'There was no History for alice to delete.');
  assert.ok(page.calls.some(([name, value]) => name === 'forget' && value === 'alice'));
});

test('one delete runs at a time', async () => {
  const s = tabState('alice');
  const page = fakePage(s);
  runDelete(s, 'alice', page.ui);
  await settle();
  assert.deepEqual(deleteButtonState(s, 'alice'), { disabled: true, loading: true });
  assert.deepEqual(deleteButtonState(s, 'bob'), { disabled: true, loading: false });

  assert.equal(runDelete(s, 'bob', page.ui), null);
  assert.equal(runDelete(s, '', page.ui), null);
  await settle();
  assert.equal(page.sent.length, 1);

  page.sent[0].answer(REMOVED);
  await settle();
  assert.deepEqual(deleteButtonState(s, 'bob'), { disabled: false, loading: false });
});

test('a failure is shown in the drill-down of the streamer being deleted', async () => {
  const s = tabState('Alice');
  s.notice = 'Earlier notice.';
  const page = fakePage(s);
  runDelete(s, 'alice', page.ui);
  await settle();
  page.sent[0].fail(new Error('History is temporarily unavailable.'));
  await settle();
  assert.equal(s.deleting, null);
  assert.equal(
    detailMessageFor(s, 'alice'),
    'Could not delete the History for alice. History is temporarily unavailable.',
  );
  assert.equal(s.notice, 'Earlier notice.', 'the status line is left alone');
  assert.ok(!page.calls.some(([name]) => name === 'forget' || name === 'status'));
  assert.deepEqual(deleteButtonState(s, 'alice'), { disabled: false, loading: false });
  assert.equal(page.timers.size, 0);
});

test('a failure goes to the status line when another drill-down is open', async () => {
  const s = tabState('alice');
  const page = fakePage(s);
  runDelete(s, 'alice', page.ui);
  await settle();
  s.detailStreamer = 'bob';
  page.sent[0].fail(new Error('HTTP 500'));
  await settle();
  assert.equal(s.detailMessage, null);
  assert.equal(s.notice, 'Could not delete the History for alice. HTTP 500');
  assert.ok(page.calls.some(([name]) => name === 'status'));
});

test('a send that throws is reported like a failed delete', async () => {
  const s = tabState('alice');
  const page = fakePage(s);
  page.ui.send = () => {
    throw new Error('offline');
  };
  runDelete(s, 'alice', page.ui);
  await settle();
  assert.equal(s.deleting, null);
  assert.equal(detailMessageFor(s, 'alice'), 'Could not delete the History for alice. offline');
});

test('starting a delete clears the last message about it', async () => {
  const s = tabState('alice');
  s.detailMessage = { streamer: 'alice', text: 'Could not delete the History for alice. HTTP 500' };
  const page = fakePage(s);
  runDelete(s, 'alice', page.ui);
  assert.equal(detailMessageFor(s, 'alice'), '');
});

test('after 30 s without an answer the tab stops waiting and reloads', async () => {
  const s = tabState('alice');
  const page = fakePage(s);
  runDelete(s, 'alice', page.ui);
  await settle();
  page.expire();
  assert.equal(s.deleting, null);
  assert.match(detailMessageFor(s, 'alice'), /^History has not answered yet\./);
  assert.match(detailMessageFor(s, 'alice'), /You can delete again\.$/);
  assert.deepEqual(page.calls.slice(2), [['renderHead'], ['reload'], ['renderHead']]);
  assert.deepEqual(deleteButtonState(s, 'alice'), { disabled: false, loading: false });

  // The user may delete again.
  assert.notEqual(runDelete(s, 'alice', page.ui), null);
});

test('a delete that answers in time never reports the wait', async () => {
  const s = tabState('alice');
  const page = fakePage(s);
  runDelete(s, 'alice', page.ui);
  await settle();
  const [timer] = [...page.timers.values()];
  page.sent[0].answer(REMOVED);
  await settle();
  const before = page.calls.length;
  timer.fn(); // a timer that fires anyway changes nothing
  assert.equal(page.calls.length, before);
  assert.equal(s.notice, 'Deleted the History for alice.');
});

test('a late success after a newer delete failed keeps the newer failure', async () => {
  const s = tabState('alice');
  const page = fakePage(s);
  runDelete(s, 'alice', page.ui);
  await settle();
  page.expire();
  runDelete(s, 'alice', page.ui);
  await settle();
  page.sent[1].fail(new Error('History is temporarily unavailable.'));
  await settle();
  const failure = 'Could not delete the History for alice. History is temporarily unavailable.';
  assert.equal(detailMessageFor(s, 'alice'), failure);

  page.sent[0].answer(REMOVED);
  await settle();
  assert.ok(page.calls.some(([name, value]) => name === 'forget' && value === 'alice'));
  assert.equal(s.detailStreamer, '', 'the drill-down closed');
  assert.equal(s.notice, failure, 'the newer failure moved to the status line');
  assert.deepEqual(page.calls.slice(-2), [['status', failure], ['renderHead']]);
});

test('a late success while a newer delete still runs leaves that delete running', async () => {
  const s = tabState('alice');
  const page = fakePage(s);
  runDelete(s, 'alice', page.ui);
  await settle();
  page.expire();
  const newer = runDelete(s, 'alice', page.ui);
  await settle();

  page.sent[0].answer(REMOVED);
  await settle();
  assert.equal(s.deleting, newer);
  assert.ok(page.calls.some(([name]) => name === 'forget'));
  assert.equal(s.notice, '', 'the older delete reports nothing');
  assert.deepEqual(deleteButtonState(s, 'alice'), { disabled: true, loading: true });

  page.sent[1].answer({ deleted: { daily_stats: 0 } });
  await settle();
  assert.equal(s.deleting, null);
  assert.equal(s.notice, 'There was no History for alice to delete.');
});

test('a late failure after a newer delete started is not reported', async () => {
  const s = tabState('alice');
  const page = fakePage(s);
  runDelete(s, 'alice', page.ui);
  await settle();
  page.expire();
  const newer = runDelete(s, 'alice', page.ui);
  await settle();

  page.sent[0].fail(new Error('HTTP 500'));
  await settle();
  assert.equal(s.deleting, newer);
  assert.equal(s.detailMessage, null);
  assert.equal(s.notice, '');

  page.sent[1].answer(REMOVED);
  await settle();
  assert.equal(s.notice, 'Deleted the History for alice.');
});

test('the newer delete of another streamer is not disturbed by a late answer', async () => {
  const s = tabState('alice');
  const page = fakePage(s);
  runDelete(s, 'alice', page.ui);
  await settle();
  page.expire();
  s.detailStreamer = 'bob';
  const newer = runDelete(s, 'bob', page.ui);
  await settle();

  page.sent[0].answer(REMOVED);
  await settle();
  assert.equal(s.deleting, newer);
  assert.equal(s.detailStreamer, 'bob', "bob's drill-down stays open");
  assert.deepEqual(deleteButtonState(s, 'bob'), { disabled: true, loading: true });
});
