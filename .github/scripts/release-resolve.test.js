'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const test = require('node:test');
const {
  manifestTag, publishingFinished, readManifest, resolveRelease,
} = require('./release-resolve');

const SHA = 'a'.repeat(40);
const context = { repo: { owner: 'o', repo: 'r' }, sha: SHA };
const MANIFEST = '{\n  ".": "3.21.2"\n}\n';

function httpError(status) {
  return Object.assign(new Error(`HTTP ${status}`), { status });
}

// release: the getReleaseByTag answer (or an error); commit: the getCommit
// answer (or an error).
function fakeGithub({ release = { draft: false }, commit = { sha: SHA } } = {}) {
  const calls = [];
  const answer = (value) => {
    if (value instanceof Error) throw value;
    return { data: value };
  };
  return {
    calls,
    rest: {
      repos: {
        async getReleaseByTag(params) {
          calls.push(['getReleaseByTag', params]);
          return answer(release);
        },
        async getCommit(params) {
          calls.push(['getCommit', params]);
          return answer(commit);
        },
      },
    },
  };
}

function fakeCore() {
  const outputs = {};
  const messages = [];
  const log = (level) => (message) => messages.push([level, message]);
  return {
    outputs,
    messages,
    setOutput(name, value) { outputs[name] = value; },
    info: log('info'),
    notice: log('notice'),
    warning: log('warning'),
  };
}

function publishedRelease(tag = '3.21.2') {
  return {
    draft: false,
    body: 'notes\n\n<!-- docker-image:start -->\n### Docker image\n<!-- docker-image:end -->\n',
    assets: [
      { name: `TwitchChannelPointsMiner-Portable-${tag}.zip`, state: 'uploaded', size: 10 },
      { name: `TwitchChannelPointsMiner-${tag}-Setup.exe`, state: 'uploaded', size: 10 },
    ],
  };
}

function resolve(github, core, overrides = {}) {
  return resolveRelease({
    github,
    context,
    core,
    releaseCreated: '',
    tagName: '',
    recheck: true,
    manifestText: MANIFEST,
    ...overrides,
  });
}

test('a release Release Please reports is used without any lookup', async () => {
  const github = fakeGithub();
  const core = fakeCore();
  assert.equal(await resolve(github, core, { releaseCreated: 'true', tagName: '3.21.2' }), '3.21.2');
  assert.deepEqual(core.outputs, { release_ready: 'true', tag_name: '3.21.2' });
  assert.deepEqual(github.calls, []);
});

test('without a failure or re-run, a published release means nothing to publish', async () => {
  for (const commit of [{ sha: SHA }, { sha: 'b'.repeat(40) }]) {
    const github = fakeGithub({ release: publishedRelease(), commit });
    const core = fakeCore();
    assert.equal(await resolve(github, core, { recheck: false }), '');
    assert.deepEqual(core.outputs, { release_ready: 'false', tag_name: '' });
  }
});

test('without a failure, an unfinished release on this commit is published', async () => {
  // Another run's Release Please created it from master's state and failed.
  const core = fakeCore();
  assert.equal(await resolve(fakeGithub(), core, { recheck: false }), '3.21.2');
  assert.deepEqual(core.outputs, { release_ready: 'true', tag_name: '3.21.2' });
  assert.equal(core.messages.at(-1)[0], 'notice');
});

test('without a failure, an unfinished release on another commit is left alone', async () => {
  const core = fakeCore();
  const github = fakeGithub({ commit: { sha: 'b'.repeat(40) } });
  assert.equal(await resolve(github, core, { recheck: false }), '');
  assert.equal(core.outputs.release_ready, 'false');
});

test('after a failure, a finished release on this commit is published again', async () => {
  const core = fakeCore();
  assert.equal(await resolve(fakeGithub({ release: publishedRelease() }), core), '3.21.2');
});

test('after a failure, a published release on this commit is published', async () => {
  const github = fakeGithub();
  const core = fakeCore();
  assert.equal(await resolve(github, core), '3.21.2');
  assert.deepEqual(core.outputs, { release_ready: 'true', tag_name: '3.21.2' });
  assert.deepEqual(github.calls, [
    ['getReleaseByTag', { owner: 'o', repo: 'r', tag: '3.21.2' }],
    ['getCommit', { owner: 'o', repo: 'r', ref: 'refs/tags/3.21.2' }],
  ]);
  assert.deepEqual(core.messages.at(-1), [
    'notice',
    'Release 3.21.2 was created by an earlier Release Please attempt; publishing it now.',
  ]);
});

test('a published release on another commit is not published again', async () => {
  const core = fakeCore();
  const github = fakeGithub({ release: publishedRelease(), commit: { sha: 'b'.repeat(40) } });
  assert.equal(await resolve(github, core), '');
  assert.equal(core.outputs.release_ready, 'false');
  assert.match(core.messages.at(-1)[1], /on another commit/);
  // Every push after a release lands here, so it is not a warning.
  assert.equal(core.messages.at(-1)[0], 'info');
});

test('a missing or draft release means nothing to publish', async () => {
  for (const github of [
    fakeGithub({ release: httpError(404) }),
    fakeGithub({ release: { draft: true } }),
  ]) {
    const core = fakeCore();
    assert.equal(await resolve(github, core), '');
    assert.deepEqual(core.outputs, { release_ready: 'false', tag_name: '' });
    assert.equal(core.messages.at(-1)[0], 'info');
  }
});

test('a published release whose tag is missing warns and publishes nothing', async () => {
  for (const github of [
    fakeGithub({ commit: httpError(404) }),
    fakeGithub({ commit: httpError(422) }),
  ]) {
    const core = fakeCore();
    assert.equal(await resolve(github, core), '');
    assert.deepEqual(core.outputs, { release_ready: 'false', tag_name: '' });
    assert.equal(core.messages.at(-1)[0], 'warning');
    assert.match(core.messages.at(-1)[1], /tag is missing/);
  }
});

test('a reported tag that is not bare SemVer fails without setting outputs', async () => {
  for (const tagName of ['v3.21.2', '3.21', '3.21.2; rm -rf /', '3.21.2\n', 'refs/tags/3.21.2', ' 3.21.2']) {
    const github = fakeGithub();
    const core = fakeCore();
    await assert.rejects(resolve(github, core, { releaseCreated: 'true', tagName }), /not a bare SemVer/, tagName);
    assert.deepEqual(core.outputs, {});
    assert.deepEqual(github.calls, []);
  }
});

test('other API errors fail the step without setting outputs', async () => {
  for (const github of [
    fakeGithub({ release: httpError(500) }),
    fakeGithub({ release: httpError(422) }),
    fakeGithub({ commit: httpError(403) }),
  ]) {
    const core = fakeCore();
    await assert.rejects(resolve(github, core), /HTTP/);
    assert.deepEqual(core.outputs, {});
  }
});

test('an unusable manifest warns and publishes nothing', async () => {
  for (const manifestText of ['', 'not json', '{}', '{".": 3}', '{".": "v3.21.2"}', 'null']) {
    const github = fakeGithub();
    const core = fakeCore();
    assert.equal(await resolve(github, core, { manifestText }), '');
    assert.equal(core.outputs.release_ready, 'false');
    assert.equal(core.messages.at(-1)[0], 'warning');
    assert.deepEqual(github.calls, []);
  }
});

test('the manifest tag is the bare root version, prereleases included', () => {
  assert.equal(manifestTag(MANIFEST), '3.21.2');
  assert.equal(manifestTag('{".": "3.22.0-rc.1"}'), '3.22.0-rc.1');
  assert.equal(manifestTag('{"packages/x": "1.0.0"}'), null);
  assert.equal(manifestTag('{".": "3.21"}'), null);
});

test('manifest versions must be valid SemVer', () => {
  for (const version of ['3.22.0-0', '3.22.0-rc.1', '3.22.0-0a', '3.22.0-post.5', '3.22.0+001', '3.22.0-rc.1+build.7']) {
    assert.equal(manifestTag(JSON.stringify({ '.': version })), version, version);
  }
  for (const version of ['3.22.0-01', '3.22.0-rc.01', '03.22.0', '3.22.0-', '3.22.0-rc..1', '3.22.0+', 'v3.22.0']) {
    assert.equal(manifestTag(JSON.stringify({ '.': version })), null, version);
  }
});

test('readManifest reads the file and returns an empty string when it is missing', () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'release-resolve-'));
  try {
    const file = path.join(dir, 'manifest.json');
    fs.writeFileSync(file, MANIFEST);
    assert.equal(readManifest(file), MANIFEST);
    assert.equal(readManifest(path.join(dir, 'missing.json')), '');
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

test('the repository manifest yields a tag', () => {
  const text = readManifest(path.join(__dirname, '..', '..', '.release-please-manifest.json'));
  assert.match(manifestTag(text), /^\d+\.\d+\.\d+/);
});

test('an unfinished release on another commit is published', async () => {
  const other = 'b'.repeat(40);
  const finished = publishedRelease();
  for (const release of [
    { draft: false },
    { ...finished, body: 'notes without the Docker block' },
    { ...finished, assets: finished.assets.slice(0, 1) },
    { ...finished, assets: finished.assets.slice(1) },
    { ...finished, assets: [{ ...finished.assets[0], state: 'starter' }, finished.assets[1]] },
    { ...finished, assets: [finished.assets[0], { ...finished.assets[1], size: 0 }] },
  ]) {
    const core = fakeCore();
    assert.equal(await resolve(fakeGithub({ release, commit: { sha: other } }), core), '3.21.2');
    assert.deepEqual(core.outputs, { release_ready: 'true', tag_name: '3.21.2' });
    assert.equal(core.messages.at(-1)[0], 'notice');
  }
});

test('publishing is finished only with the Docker block and both Windows files', () => {
  assert.equal(publishingFinished(publishedRelease('3.22.0'), '3.22.0'), true);
  assert.equal(publishingFinished(publishedRelease('3.21.2'), '3.22.0'), false);
  assert.equal(publishingFinished({ body: null, assets: null }, '3.22.0'), false);
});
