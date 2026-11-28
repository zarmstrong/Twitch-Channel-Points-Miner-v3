'use strict';

// How the History tab names games and drops (assets/history.js).
// Run with: node tests/js/history-drops.test.js

const assert = require('node:assert/strict');
const test = require('node:test');
const path = require('node:path');
const { dropItemText, gameLabel, gamesNoteText } = require(
  path.join(__dirname, '..', '..', 'assets', 'history.js'),
);

test('a game row is named for its game', () => {
  assert.equal(gameLabel({ game: 'Game A', drops: 1, quests: 0 }), 'Game A');
});

test('drops without a game are quests when they all are', () => {
  assert.equal(gameLabel({ game: null, drops: 2, quests: 2 }), 'Quests (no game)');
  assert.equal(gameLabel({ game: null, drops: 2, quests: 1 }), 'No game');
  assert.equal(gameLabel({ game: null, drops: 0, quests: 0 }), 'No game');
  assert.equal(gameLabel(null), 'No game');
});

test('a drop is described by its rewards', () => {
  assert.equal(dropItemText({ name: 'Badge', benefits: ['Badge'] }), 'Badge');
  assert.equal(
    dropItemText({ name: 'Week 1', benefits: ['Badge', 'Emote'] }),
    'Badge, Emote (Week 1)',
  );
  assert.equal(dropItemText({ name: 'Week 1', benefits: [] }), 'Week 1');
  assert.equal(dropItemText({}), 'Drop');
});

test('the games note shows only for a database that gained games later', () => {
  const day = (timestamp) => `day ${timestamp}`;
  assert.equal(gamesNoteText({ games_since: null }, day), '');
  assert.equal(gamesNoteText({}, day), '');
  assert.equal(gamesNoteText(null, day), '');
  assert.match(gamesNoteText({ games_since: 1767225600 }, day), /from day 1767225600/);
});
