'use strict';

// The History tab's note about days imported from the analytics data
// (assets/history.js). Run with: node tests/js/history-import-note.test.js

const assert = require('node:assert/strict');
const test = require('node:test');
const path = require('node:path');
const { importNoteText } = require(
  path.join(__dirname, '..', '..', 'assets', 'history.js'),
);

const day = (timestamp) => `day ${timestamp}`;

test('nothing imported shows no note', () => {
  for (const overview of [{}, { imported_before: null }, { imported_before: 0 }, null]) {
    assert.equal(importNoteText(overview, day), '');
  }
});

test('imported days are named with what they lack', () => {
  const text = importNoteText({ imported_before: 1767225600 }, day);
  assert.match(text, /^History before day 1767225600 was imported/);
  assert.match(text, /points and predictions only/);
  assert.match(text, /without watch time or live sessions/);
});
