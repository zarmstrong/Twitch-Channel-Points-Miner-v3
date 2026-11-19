'use strict';

const assert = require('node:assert/strict');
const test = require('node:test');
const {
  decideLatestTag,
  isNewestStableTag,
  newestStableTag,
} = require('./docker-latest-tag');

// The shape of this repository's tags: old releases, prereleases and a
// legacy prefixed tag.
const TAGS = [
  '3.21.1',
  '3.21.0',
  '3.20.0-post.5',
  '3.20.0',
  '3.9.0',
  'Twitch-Channel-Points-Miner-v2-3.0.0',
  '1.7.0-test',
  '1.3',
  '0.1',
];

test('versions are compared numerically, not as text', () => {
  assert.equal(newestStableTag(['3.9.0', '3.21.1', '3.10.0']), '3.21.1');
  assert.equal(newestStableTag(['10.0.0', '9.99.99']), '10.0.0');
  assert.equal(newestStableTag(['1.0.10', '1.0.9']), '1.0.10');
});

test('prereleases, prefixed and short tags are never the newest stable tag', () => {
  assert.equal(newestStableTag(['3.21.1', '3.22.0-rc.1', 'v4.0.0', '5.0', '4.0.0+build']), '3.21.1');
  assert.equal(newestStableTag(['3.22.0-rc.1', '1.3']), null);
  assert.equal(newestStableTag([]), null);
});

test('leading zeros are not plain versions', () => {
  assert.equal(newestStableTag(['3.21.1', '03.30.0', '3.030.0']), '3.21.1');
});

test('only the newest stable release moves latest', () => {
  assert.equal(isNewestStableTag('3.21.1', TAGS), true);
  assert.equal(isNewestStableTag('3.21.0', TAGS), false);
  assert.equal(isNewestStableTag('3.9.0', TAGS), false);
});

test('a prerelease never moves latest, even when it sorts above every release', () => {
  assert.equal(isNewestStableTag('3.22.0-rc.1', [...TAGS, '3.22.0-rc.1']), false);
  assert.equal(isNewestStableTag('3.20.0-post.5', TAGS), false);
});

test('a version missing from the tag list never moves latest', () => {
  assert.equal(isNewestStableTag('3.22.0', TAGS), false);
  assert.equal(isNewestStableTag('', TAGS), false);
  assert.equal(isNewestStableTag(undefined, TAGS), false);
});

function fakeGithub(pages) {
  const calls = [];
  const listTags = () => {};
  return {
    calls,
    rest: { repos: { listTags } },
    async paginate(method, params, map) {
      assert.equal(method, listTags);
      calls.push(params);
      return pages.flatMap((page) => map({ data: page.map((name) => ({ name })) }));
    },
  };
}

function fakeCore() {
  const outputs = {};
  const infos = [];
  return {
    outputs,
    infos,
    setOutput(name, value) { outputs[name] = value; },
    info(message) { infos.push(message); },
  };
}

const context = { repo: { owner: 'o', repo: 'r' } };

test('every page of tags is read before deciding', async () => {
  const github = fakeGithub([['3.9.0', '3.21.0'], ['3.21.1']]);
  const core = fakeCore();
  assert.equal(await decideLatestTag({ github, context, core, tag: '3.21.0' }), false);
  assert.deepEqual(github.calls, [{ owner: 'o', repo: 'r', per_page: 100 }]);
  assert.equal(core.outputs.move_latest, 'false');
  assert.match(core.infos[0], /newest stable release is 3\.21\.1, not 3\.21\.0/);
});

test('the newest release sets move_latest to true', async () => {
  const core = fakeCore();
  assert.equal(await decideLatestTag({ github: fakeGithub([TAGS]), context, core, tag: '3.21.1' }), true);
  assert.equal(core.outputs.move_latest, 'true');
  assert.match(core.infos[0], /3\.21\.1 is the newest stable release/);
});

test('a prerelease and an unknown tag each say why latest stays', async () => {
  let core = fakeCore();
  await decideLatestTag({ github: fakeGithub([TAGS]), context, core, tag: '3.20.0-post.5' });
  assert.equal(core.outputs.move_latest, 'false');
  assert.match(core.infos[0], /not a plain MAJOR\.MINOR\.PATCH release/);

  core = fakeCore();
  await decideLatestTag({ github: fakeGithub([TAGS]), context, core, tag: '3.22.0' });
  assert.equal(core.outputs.move_latest, 'false');
  assert.match(core.infos[0], /not a tag in this repository/);
});

test('a failed tag listing fails the step instead of deciding', async () => {
  const github = fakeGithub([]);
  github.paginate = async () => { throw new Error('API down'); };
  const core = fakeCore();
  await assert.rejects(decideLatestTag({ github, context, core, tag: '3.21.1' }), /API down/);
  assert.equal(core.outputs.move_latest, undefined);
});
