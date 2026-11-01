'use strict';

const assert = require('node:assert/strict');
const test = require('node:test');
const {
  linkReleasePullRequests,
  linkedNumbers,
  rewritePullLinks,
} = require('./release-pull-links');

const BASE = 'https://github.com/o/r';
const NOTES = [
  `* **drops:** fix ([#196](${BASE}/issues/196)) ([abc](${BASE}/commit/abc)), `
    + `closes [#176](${BASE}/issues/176)`,
  `* **drops:** other ([#197](${BASE}/issues/197))`,
  '* upstream ([#7](https://github.com/x/r/issues/7))',
].join('\n');

test('only this repository\'s issue links are collected, once each', () => {
  assert.deepEqual(linkedNumbers(`${NOTES}\n${BASE}/issues/196`, 'o', 'r'), [196, 176, 197]);
  assert.deepEqual(linkedNumbers(null, 'o', 'r'), []);
});

test('PR numbers are relinked while issues and other repositories are kept', () => {
  const body = rewritePullLinks(NOTES, 'o', 'r', new Set([196, 197, 7]));
  assert.match(body, /\[#196\]\(https:\/\/github\.com\/o\/r\/pull\/196\)/);
  assert.match(body, /\[#197\]\(https:\/\/github\.com\/o\/r\/pull\/197\)/);
  assert.match(body, /closes \[#176\]\(https:\/\/github\.com\/o\/r\/issues\/176\)/);
  assert.match(body, /https:\/\/github\.com\/x\/r\/issues\/7/);
  assert.match(body, /commit\/abc/);
});

test('a longer number is not relinked as its PR prefix', () => {
  assert.equal(
    rewritePullLinks(`${BASE}/issues/1960`, 'o', 'r', new Set([196])),
    `${BASE}/issues/1960`,
  );
});

test('only the issues segment changes, keeping the link\'s own casing', () => {
  assert.equal(
    rewritePullLinks('https://github.com/O/R/issues/196', 'o', 'r', new Set([196])),
    'https://github.com/O/R/pull/196',
  );
  assert.equal(
    rewritePullLinks('https://github.com/o/issues/issues/196', 'o', 'issues', new Set([196])),
    'https://github.com/o/issues/pull/196',
  );
});

function fakeGithub({ fresh = {}, pullNumbers = [196, 7], failWith, listFails } = {}) {
  const calls = [];
  const lookups = [];
  const list = () => {};
  const notFound = () => Object.assign(new Error('Not Found'), { status: 404 });
  const github = {
    calls,
    lookups,
    paginate: async (method, params) => {
      assert.equal(method, list);
      assert.equal(params.state, 'open');
      if (listFails) throw Object.assign(new Error('Bad Gateway'), { status: 502 });
      return [
        { number: 198, labels: [{ name: 'autorelease: pending' }], body: NOTES },
        { number: 199, labels: [{ name: 'autorelease: pending' }], body: 'no links' },
        { number: 5, labels: [{ name: 'bug' }], body: NOTES },
      ];
    },
    rest: {
      pulls: {
        list,
        get: async ({ pull_number: number }) => {
          if (number in fresh) return { data: { number, body: fresh[number] } };
          lookups.push(number);
          if (failWith) throw Object.assign(new Error('Forbidden'), { status: failWith });
          if (pullNumbers.includes(number)) return { data: { number } };
          throw notFound();
        },
        update: async (input) => calls.push(['pull', input]),
      },
      repos: {
        getReleaseByTag: async () => ({ data: { id: 3, body: NOTES } }),
        getRelease: async () => ({ data: { id: 3, body: 'release' in fresh ? fresh.release : NOTES } }),
        updateRelease: async (input) => calls.push(['release', input]),
      },
    },
  };
  return github;
}

const run = (github, tag) => linkReleasePullRequests({
  github, context: { repo: { owner: 'o', repo: 'r' } }, core: { info() {} }, tag,
});

test('release PR and new release bodies are relinked, unchanged bodies are left alone', async () => {
  const github = fakeGithub({ fresh: { 198: NOTES, 199: 'no links' } });
  await run(github, '2');

  assert.deepEqual(
    github.calls.map(([kind, input]) => [kind, input.pull_number ?? input.release_id]),
    [['release', 3], ['pull', 198]],
  );
  for (const [, input] of github.calls) {
    assert.match(input.body, /o\/r\/pull\/196\)/);
    assert.match(input.body, /o\/r\/issues\/176\)/);
    assert.match(input.body, /o\/r\/issues\/197\)/);
  }
  // Each number is looked up once across the release PR and the release.
  assert.deepEqual(github.lookups.sort(), [176, 196, 197]);
});

test('the freshly read body is written so concurrent entries are kept', async () => {
  const newer = `${NOTES}\n* added meanwhile ([#200](${BASE}/issues/200))`;
  const github = fakeGithub({ fresh: { 198: newer, 199: 'no links' }, pullNumbers: [196, 200] });
  await run(github);

  assert.equal(github.calls.length, 1);
  const { body } = github.calls[0][1];
  assert.match(body, /o\/r\/pull\/196\)/);
  assert.match(body, /added meanwhile \(\[#200\]\(https:\/\/github\.com\/o\/r\/pull\/200\)\)/);
});

test('releases and release PRs without notes are left alone', async () => {
  const github = fakeGithub({ fresh: { 198: null, 199: null, release: null } });
  await run(github, '2');
  assert.equal(github.calls.length, 0);
});

test('the release is relinked even when listing release PRs fails', async () => {
  const github = fakeGithub({ fresh: { 198: NOTES, 199: 'no links' }, listFails: true });
  await assert.rejects(run(github, '2'), /Bad Gateway/);
  assert.deepEqual(github.calls.map(([kind]) => kind), ['release']);
});

test('API failures other than a missing number are raised', async () => {
  const github = fakeGithub({ fresh: { 198: NOTES, 199: 'no links' }, failWith: 403 });
  await assert.rejects(run(github), /Forbidden/);
  assert.equal(github.calls.length, 0);
});
