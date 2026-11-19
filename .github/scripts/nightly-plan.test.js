'use strict';

const assert = require('node:assert/strict');
const test = require('node:test');
const { planNightly, publishDecision } = require('./nightly-plan');

const SHA = 'a'.repeat(40);
const OLD = 'b'.repeat(40);

function notFound(status = 404) {
  const error = new Error('Not Found');
  error.status = status;
  return error;
}

// `tags` maps tag names to the commits they point to. `runs` is what the
// Tests workflow reports for the commit, newest first.
// `ahead` lists the commits that already contain the commit being built.
function fakeGithub({
  tags = { '3.22.0': OLD }, latest = '3.22.0', runs = [{ status: 'completed', conclusion: 'success' }],
  calls = [], ahead = [],
} = {}) {
  return {
    rest: {
      repos: {
        compareCommitsWithBasehead: async ({ basehead }) => {
          calls.push(['compare', basehead]);
          const head = basehead.split('...')[1];
          return { data: { status: ahead.includes(head) ? 'ahead' : 'behind' } };
        },
        getCommit: async ({ ref }) => {
          calls.push(['commit', ref]);
          const tag = ref.replace(/^tags\//, '');
          if (!ref.startsWith('tags/') || !tags[tag]) throw notFound(422);
          return { data: { sha: tags[tag] } };
        },
        getLatestRelease: async () => {
          if (!latest) throw notFound();
          return { data: { tag_name: latest } };
        },
      },
      actions: {
        listWorkflowRuns: async (input) => {
          calls.push(['runs', input]);
          return { data: { workflow_runs: runs } };
        },
      },
    },
  };
}

async function plan({ eventName = 'schedule', ref = 'refs/heads/master', github, ...options } = {}) {
  const outputs = {};
  const messages = [];
  const result = await planNightly({
    github: github || fakeGithub(),
    context: { repo: { owner: 'o', repo: 'r' }, eventName, ref },
    core: {
      setOutput: (name, value) => { outputs[name] = value; },
      notice: (message) => messages.push(['notice', message]),
      warning: (message) => messages.push(['warning', message]),
    },
    sha: SHA,
    hasDockerCredentials: true,
    ...options,
  });
  return { result, outputs, messages };
}

test('a scheduled run on master builds and publishes new commits', async () => {
  const calls = [];
  const { outputs } = await plan({ github: fakeGithub({ calls }) });
  assert.deepEqual(outputs, { build: 'true', publish: 'true', release_tag: '3.22.0' });
  const runs = calls.find(([kind]) => kind === 'runs')[1];
  assert.deepEqual(
    { workflow: runs.workflow_id, sha: runs.head_sha, event: runs.event },
    { workflow: 'tests.yml', sha: SHA, event: 'push' },
  );
});

test('nothing is built when the last nightly was made from this commit', async () => {
  const github = fakeGithub({ tags: { '3.22.0': OLD, nightly: SHA } });
  const { outputs, messages } = await plan({ github });
  assert.deepEqual(outputs, { build: 'false', publish: 'false', release_tag: '3.22.0' });
  assert.match(messages.at(-1)[1], /last nightly build was already made/);
});

test('nothing is built when master is exactly the latest release', async () => {
  const github = fakeGithub({ tags: { '3.22.0': SHA, nightly: OLD } });
  const { outputs, messages } = await plan({ github });
  assert.equal(outputs.build, 'false');
  assert.match(messages.at(-1)[1], /is release 3\.22\.0/);
});

test('force builds an unchanged commit again', async () => {
  for (const tags of [{ '3.22.0': OLD, nightly: SHA }, { '3.22.0': SHA }]) {
    const { outputs } = await plan({
      eventName: 'workflow_dispatch', publishRequested: true, force: true, github: fakeGithub({ tags }),
    });
    assert.deepEqual([outputs.build, outputs.publish], ['true', 'true']);
  }
});

test('a re-run of an earlier night never moves nightly backwards', async () => {
  const NEWER = 'c'.repeat(40);
  for (const force of [false, true]) {
    const calls = [];
    const github = fakeGithub({ tags: { '3.22.0': OLD, nightly: NEWER }, ahead: [NEWER], calls });
    const { outputs, messages } = await plan({ github, force });
    assert.deepEqual([outputs.build, outputs.publish], ['false', 'false']);
    assert.match(messages.at(-1)[1], /is newer than/);
    assert.deepEqual(calls.find(([kind]) => kind === 'compare'), ['compare', `${SHA}...${NEWER}`]);
  }
});

test('a commit whose tests did not pass is never published, even when forced', async () => {
  const cases = [
    [[{ status: 'completed', conclusion: 'failure' }], /is failure/],
    [[{ status: 'in_progress', conclusion: null }], /is in_progress/],
    [[], /is missing/],
  ];
  for (const [runs, expected] of cases) {
    const { outputs, messages } = await plan({ force: true, github: fakeGithub({ runs }) });
    assert.deepEqual([outputs.build, outputs.publish], ['false', 'false']);
    assert.equal(messages[0][0], 'warning');
    assert.match(messages[0][1], expected);
  }
});

test('the first nightly and the first release need no existing tags', async () => {
  const { outputs } = await plan({ github: fakeGithub({ tags: {}, latest: null }) });
  assert.deepEqual(outputs, { build: 'true', publish: 'true', release_tag: '' });
});

test('a manual run is a dry run unless publishing is requested', async () => {
  const calls = [];
  const { outputs, messages } = await plan({
    eventName: 'workflow_dispatch', github: fakeGithub({ calls, runs: [] }),
  });
  assert.deepEqual([outputs.build, outputs.publish], ['true', 'false']);
  assert.match(messages[0][1], /^Dry run: .*not requested/);
  // A dry run doesn't depend on the tests or on what was built before.
  assert.deepEqual(calls, []);
});

test('a pull request run is always a dry run', async () => {
  const { outputs } = await plan({
    eventName: 'pull_request', ref: 'refs/pull/7/merge', publishRequested: true,
  });
  assert.deepEqual([outputs.build, outputs.publish], ['true', 'false']);
});

test('a manual run from another branch never publishes', async () => {
  const { outputs, messages } = await plan({
    eventName: 'workflow_dispatch', ref: 'refs/heads/feature', publishRequested: true,
  });
  assert.deepEqual([outputs.build, outputs.publish], ['true', 'false']);
  assert.match(messages[0][1], /only refs\/heads\/master is published/);
});

test('without Docker Hub credentials a scheduled run does nothing', async () => {
  const { outputs, messages } = await plan({ hasDockerCredentials: false });
  assert.deepEqual([outputs.build, outputs.publish], ['false', 'false']);
  assert.match(messages[0][1], /^Nothing to do: Docker Hub credentials/);
  const manual = await plan({
    eventName: 'workflow_dispatch', publishRequested: true, hasDockerCredentials: false,
  });
  assert.deepEqual([manual.outputs.build, manual.outputs.publish], ['true', 'false']);
});

test('an API failure is not mistaken for a missing tag or release', async () => {
  const github = fakeGithub();
  github.rest.repos.getCommit = async () => { throw notFound(500); };
  await assert.rejects(plan({ github }), /Not Found/);
  const broken = fakeGithub();
  broken.rest.repos.getLatestRelease = async () => { throw notFound(503); };
  await assert.rejects(plan({ github: broken }), /Not Found/);
});

test('publishing needs the schedule or a request, master, and credentials', () => {
  const base = {
    eventName: 'schedule', ref: 'refs/heads/master', publishRequested: false, hasDockerCredentials: true,
  };
  assert.equal(publishDecision(base).publish, true);
  assert.equal(publishDecision({ ...base, eventName: 'push' }).publish, false);
  assert.equal(publishDecision({ ...base, eventName: 'workflow_dispatch' }).publish, false);
  assert.equal(
    publishDecision({ ...base, eventName: 'workflow_dispatch', publishRequested: true }).publish,
    true,
  );
});
