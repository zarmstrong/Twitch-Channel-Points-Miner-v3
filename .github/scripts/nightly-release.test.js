'use strict';

const assert = require('node:assert/strict');
const test = require('node:test');
const {
  NIGHTLY_ASSET,
  assertPublishing,
  beginPublishing,
  finishPublishing,
  isComplete,
  nightlyState,
  publishingCommit,
  releaseNotes,
} = require('./nightly-release');

// Three commits on master, oldest first.
const [C1, C2, C3] = ['1', '2', '3'].map((digit) => digit.repeat(40));
const HISTORY = [C1, C2, C3];
const ZIP = { name: NIGHTLY_ASSET, state: 'uploaded' };

function apiError(status) {
  const error = new Error(`HTTP ${status}`);
  error.status = status;
  return error;
}

// A repository whose tags and nightly release the functions really change,
// so a test can run publishing steps one after another, as jobs would.
function fakeRepository({ tag = null, release = null } = {}) {
  const repository = { tag, release, calls: [] };
  repository.github = {
    rest: {
      repos: {
        getCommit: async ({ ref }) => {
          if (ref !== 'tags/nightly' || !repository.tag) throw apiError(422);
          return { data: { sha: repository.tag } };
        },
        compareCommitsWithBasehead: async ({ basehead }) => {
          const [base, head] = basehead.split('...');
          const behind = HISTORY.indexOf(base) <= HISTORY.indexOf(head);
          return { data: { status: behind ? 'ahead' : 'behind' } };
        },
        getReleaseByTag: async ({ tag: name }) => {
          if (name !== 'nightly' || !repository.release) throw apiError(404);
          return { data: structuredClone(repository.release) };
        },
        createRelease: async (input) => {
          repository.calls.push(['create', input]);
          repository.release = { id: 7, assets: [], ...input };
          // GitHub creates the tag with the release when it doesn't exist.
          if (!repository.tag) repository.tag = input.target_commitish;
        },
        updateRelease: async ({ release_id: id, ...input }) => {
          repository.calls.push(['update', input]);
          assert.equal(id, repository.release.id);
          Object.assign(repository.release, input);
        },
      },
      git: {
        updateRef: async ({ ref, sha, force }) => {
          repository.calls.push(['move', sha]);
          assert.deepEqual([ref, force], ['tags/nightly', true]);
          if (!repository.tag) throw apiError(422);
          repository.tag = sha;
        },
        createRef: async ({ ref, sha }) => {
          assert.equal(ref, 'refs/tags/nightly');
          repository.tag = sha;
        },
      },
    },
  };
  return repository;
}

// Each test's steps belong to workflow run 100 unless it says otherwise.
function run(step, repository, sha, { runId = 100, ...extra } = {}) {
  const failures = [];
  return step({
    github: repository.github,
    context: { repo: { owner: 'o', repo: 'r' }, serverUrl: 'https://github.com', runId },
    core: { setFailed: (message) => failures.push(message), info() {} },
    sha,
    ...extra,
  }).then((ok) => ({ ok, failures }));
}

const DETAILS = {
  version: '3.22.0+nightly.20261119.2222222', date: '20261119', releaseTag: '3.22.0', image: 'ns/miner',
};

// What nightly.yml does between the first and the last step.
function upload(repository) {
  repository.release.assets = [ZIP];
}

async function publish(repository, sha, runId = 100) {
  assert.equal((await run(beginPublishing, repository, sha, { runId })).ok, true);
  assert.equal((await run(assertPublishing, repository, sha, { runId })).ok, true);
  upload(repository);
  assert.equal((await run(finishPublishing, repository, sha, { ...DETAILS, runId })).ok, true);
}

async function state(repository) {
  return nightlyState(repository.github, 'o', 'r');
}

test('the first nightly build creates the release as a prerelease and finishes complete', async () => {
  const repository = fakeRepository();
  await publish(repository, C2);

  const created = repository.calls.find(([kind]) => kind === 'create')[1];
  assert.deepEqual(
    [created.tag_name, created.prerelease, created.draft, created.make_latest],
    ['nightly', true, false, 'false'],
  );
  assert.equal(repository.tag, C2);
  assert.equal(repository.release.name, `Nightly build ${DETAILS.version}`);
  assert.equal(publishingCommit(repository.release), null);
  assert.equal(isComplete(await state(repository), C2), true);
});

test('a later build keeps the old notes under the note until it finishes', async () => {
  const repository = fakeRepository();
  await publish(repository, C1);
  const finished = repository.release.body;

  await run(beginPublishing, repository, C2);
  assert.equal(publishingCommit(repository.release), C2);
  assert.ok(repository.release.body.endsWith(finished));
  assert.equal(repository.tag, C1);
  // Not complete for either commit while the note is there.
  assert.equal(isComplete(await state(repository), C1), false);
  assert.equal(isComplete(await state(repository), C2), false);

  upload(repository);
  await run(finishPublishing, repository, C2, DETAILS);
  assert.equal(repository.tag, C2);
  assert.doesNotMatch(repository.release.body, /nightly-publishing/);
  assert.match(repository.release.body, new RegExp(`commit/${C2}`));
});

test('every release write keeps it a prerelease that is never latest', async () => {
  const repository = fakeRepository();
  await publish(repository, C1);
  await publish(repository, C2);
  const writes = repository.calls.filter(([kind]) => kind === 'create' || kind === 'update');
  assert.equal(writes.length, 4);
  for (const [, input] of writes) {
    assert.deepEqual([input.prerelease, input.make_latest], [true, 'false']);
  }
});

test('a re-run of an older run cannot publish over a newer finished build', async () => {
  const repository = fakeRepository();
  await publish(repository, C1);
  // Run A (C2) began and failed; run B (C3) then published completely.
  await run(beginPublishing, repository, C2);
  await publish(repository, C3);

  // "Re-run failed jobs" on A re-runs only its publishing jobs.
  const rerun = await run(assertPublishing, repository, C2);
  assert.equal(rerun.ok, false);
  assert.match(rerun.failures[0], /doesn't record this run as publishing 2{40}/);
  // "Re-run all jobs" on A starts publishing again.
  const restart = await run(beginPublishing, repository, C2);
  assert.equal(restart.ok, false);
  assert.match(restart.failures[0], /newer nightly build \(3{40}\)/);
  assert.equal(repository.tag, C3);
  assert.equal(isComplete(await state(repository), C3), true);
});

test('a re-run of an older run cannot publish over a newer build that failed part-way', async () => {
  const repository = fakeRepository();
  await publish(repository, C1);
  // Run A (C2) began and failed. Run B (C3) pushed its Docker image and
  // failed before it moved the tag, which is still on C1.
  await run(beginPublishing, repository, C2);
  await run(beginPublishing, repository, C3);
  assert.equal(repository.tag, C1);

  for (const step of [assertPublishing, beginPublishing]) {
    const rerun = await run(step, repository, C2);
    assert.equal(rerun.ok, false);
    assert.match(rerun.failures[0], /3{40}/);
  }
  const finish = await run(finishPublishing, repository, C2, DETAILS);
  assert.equal(finish.ok, false);
  assert.equal(publishingCommit(repository.release), C3);
  // B's own re-run still goes through.
  assert.equal((await run(assertPublishing, repository, C3)).ok, true);
});

test('a re-run of an older run cannot finish over a newer run of the same commit', async () => {
  const repository = fakeRepository();
  await publish(repository, C1);
  // Run 100 pushed its Docker image of C2 and failed before the upload. The
  // next night run 200 built C2 again, under a new dated version, pushed
  // its Docker image and failed in the same place.
  await run(beginPublishing, repository, C2, { runId: 100 });
  await run(beginPublishing, repository, C2, { runId: 200 });

  // "Re-run failed jobs" on run 100 would upload its older Windows build.
  const rerun = await run(assertPublishing, repository, C2, { runId: 100 });
  assert.equal(rerun.ok, false);
  assert.match(rerun.failures[0], /Run 200 is publishing 2{40}/);
  upload(repository);
  const finish = await run(finishPublishing, repository, C2, { ...DETAILS, runId: 100 });
  assert.equal(finish.ok, false);
  assert.equal(isComplete(await state(repository), C2), false);

  // Run 200's own re-run finishes it.
  assert.equal((await run(assertPublishing, repository, C2, { runId: 200 })).ok, true);
  assert.equal((await run(finishPublishing, repository, C2, { ...DETAILS, runId: 200 })).ok, true);
  assert.equal(isComplete(await state(repository), C2), true);
});

test('a rebuild of the same commit that fails part-way is not taken for finished', async () => {
  const repository = fakeRepository();
  await publish(repository, C2);
  assert.equal(isComplete(await state(repository), C2), true);

  // A forced rebuild pushes Docker, then fails before the upload. The tag
  // and the older download are still in place.
  await run(beginPublishing, repository, C2);
  const after = await state(repository);
  assert.deepEqual([after.tag, after.hasDownload, after.publishing], [C2, true, C2]);
  assert.equal(isComplete(after, C2), false);
});

test('finishing refuses a release whose download is missing', async () => {
  const repository = fakeRepository();
  await run(beginPublishing, repository, C2);
  const finish = await run(finishPublishing, repository, C2, DETAILS);
  assert.equal(finish.ok, false);
  assert.match(finish.failures[0], /upload did not finish/);
  assert.equal(publishingCommit(repository.release), C2);
});

test('finishing creates the tag when the release lost it', async () => {
  const repository = fakeRepository();
  await run(beginPublishing, repository, C2);
  upload(repository);
  repository.tag = null;
  assert.equal((await run(finishPublishing, repository, C2, DETAILS)).ok, true);
  assert.equal(repository.tag, C2);
});

test('a failure while finishing leaves the build unfinished', async () => {
  const repository = fakeRepository();
  await publish(repository, C1);
  await run(beginPublishing, repository, C2);
  upload(repository);
  repository.github.rest.repos.updateRelease = async () => { throw apiError(502); };
  await assert.rejects(run(finishPublishing, repository, C2, DETAILS), /HTTP 502/);
  // The tag moved, but the note is still there, so the build is not complete.
  assert.equal(repository.tag, C2);
  assert.equal(isComplete(await state(repository), C2), false);
});

test('API failures are not mistaken for a missing tag or release', async () => {
  const repository = fakeRepository();
  repository.github.rest.repos.getReleaseByTag = async () => { throw apiError(500); };
  await assert.rejects(state(repository), /HTTP 500/);
  const other = fakeRepository();
  other.github.rest.repos.getCommit = async () => { throw apiError(503); };
  await assert.rejects(state(other), /HTTP 503/);
});

test('release notes link the commit, the changes and the downloads', () => {
  const notes = releaseNotes({ repoUrl: 'https://github.com/o/r', sha: C2, ...DETAILS });
  assert.match(notes, /Version: `3\.22\.0\+nightly\.20261119\.2222222`/);
  assert.match(notes, /\[`2222222`\]\(https:\/\/github\.com\/o\/r\/commit\/2{40}\)/);
  assert.match(notes, /Built: 2026-11-19 \(UTC\)/);
  assert.match(notes, /compare\/3\.22\.0\.\.\.2{40}/);
  assert.match(notes, /docker pull ns\/miner:nightly/);
  assert.ok(notes.includes(NIGHTLY_ASSET));
  // Before the first release there is nothing to compare with.
  const first = releaseNotes({ repoUrl: 'https://github.com/o/r', sha: C2, ...DETAILS, releaseTag: '' });
  assert.doesNotMatch(first, /compare/);
});
