'use strict';

// The Config tab's Import button for the analytics data (assets/history.js).
// Run with: node tests/js/history-import-button.test.js

const assert = require('node:assert/strict');
const test = require('node:test');
const path = require('node:path');
const { importControlState } = require(
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

test('an import waiting for the first journal record says so', () => {
  const view = importControlState(status({ requested: true, boundary: null }));
  assert.equal(view.showButton, false);
  assert.equal(view.polling, true);
  assert.match(view.text, /once History has recorded its first entry/);
});

test('no status (History not running) offers no button', () => {
  const view = importControlState(null);
  assert.equal(view.showButton, false);
  assert.equal(view.polling, false);
  assert.match(view.text, /not running/);
});
