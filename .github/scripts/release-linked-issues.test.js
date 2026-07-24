'use strict';

const assert = require('node:assert/strict');
const test = require('node:test');
const {
  commitsBetween,
  isReleasePleasePullRequest,
  markIssuesAwaitingRelease,
  previousPublishedRelease,
  releasePullRequests,
  shouldCloseIssue,
  uniqueOpenIssues,
} = require('./release-linked-issues');

test('ordinary PRs are distinct from Release Please PRs', () => {
  assert.equal(isReleasePleasePullRequest(['bug']), false);
  assert.equal(isReleasePleasePullRequest(['autorelease: pending']), true);
  assert.equal(isReleasePleasePullRequest(['autorelease: tagged']), true);
});

test('issue selection is open-only and deduplicated', () => {
  const issues = uniqueOpenIssues([
    { number: 1, state: 'OPEN' },
    { number: 1, state: 'OPEN' },
    { number: 2, state: 'CLOSED' },
  ]);
  assert.deepEqual(issues.map((issue) => issue.number), [1]);
  assert.equal(shouldCloseIssue({
    state: 'OPEN', labels: { nodes: [{ name: 'awaiting-release' }] },
  }), true);
  assert.equal(shouldCloseIssue({
    state: 'CLOSED', labels: { nodes: [{ name: 'awaiting-release' }] },
  }), false);
  assert.equal(shouldCloseIssue({ state: 'OPEN', labels: { nodes: [] } }), false);
});

test('marking is idempotent when label and comment already exist', async () => {
  let mutations = 0;
  const github = {
    graphql: async () => ({ repository: { pullRequest: {
      closingIssuesReferences: {
        nodes: [{
          number: 1,
          state: 'OPEN',
          labels: { nodes: [{ name: 'awaiting-release' }] },
        }],
        pageInfo: { hasNextPage: false },
      },
    } } }),
    paginate: async () => [{ body: '<!-- awaiting-release -->' }],
    rest: { issues: {
      listComments() {},
      addLabels: async () => { mutations += 1; },
      createComment: async () => { mutations += 1; },
    } },
  };
  await markIssuesAwaitingRelease({
    github,
    context: {
      repo: { owner: 'o', repo: 'r' },
      payload: { pull_request: { number: 9, labels: [] } },
    },
    core: { info() {} },
  });
  assert.equal(mutations, 0);
});

test('release PR selection requires a range commit to be the merge commit', () => {
  const pulls = releasePullRequests(['a', 'b'], {
    a: [
      { number: 10, merged_at: '2026-01-01', merge_commit_sha: 'a' },
      { number: 11, merged_at: null, merge_commit_sha: 'a' },
      {
        number: 13,
        merged_at: '2026-01-01',
        merge_commit_sha: 'a',
        labels: [{ name: 'autorelease: tagged' }],
      },
    ],
    b: [{ number: 10, merged_at: '2026-01-01', merge_commit_sha: 'a' }],
    c: [{ number: 12, merged_at: '2026-01-01', merge_commit_sha: 'c' }],
  });
  assert.deepEqual(pulls.map((pull) => pull.number), [10]);
});

test('tag comparison collects every page', async () => {
  const calls = [];
  const github = {
    rest: { repos: { compareCommitsWithBasehead: async ({ page }) => {
      calls.push(page);
      return { data: {
        status: 'ahead', total_commits: 3,
        commits: page === 1 ? [{ sha: 'a' }, { sha: 'b' }] : [{ sha: 'c' }],
      } };
    } } },
  };
  assert.deepEqual(await commitsBetween(github, 'o', 'r', '1', '2'), ['a', 'b', 'c']);
  assert.deepEqual(calls, [1, 2]);
});

test('tag comparison fails safely when results are truncated', async () => {
  const github = {
    rest: { repos: { compareCommitsWithBasehead: async ({ page }) => ({ data: {
      status: 'ahead', total_commits: 2, commits: page === 1 ? [{ sha: 'a' }] : [],
    } }) } },
  };
  await assert.rejects(
    commitsBetween(github, 'o', 'r', '1', '2'),
    /refusing partial processing/,
  );
});

test('previous release selection uses publication time and fails safely', async () => {
  const releases = [
    { tag_name: '2', draft: false, published_at: '2026-03-03T00:00:00Z' },
    { tag_name: 'old', draft: false, published_at: '2026-01-01T00:00:00Z' },
    { tag_name: '1', draft: false, published_at: '2026-02-02T00:00:00Z' },
    { tag_name: 'draft', draft: true, published_at: '2026-02-03T00:00:00Z' },
  ];
  const github = {
    paginate: async () => releases,
    rest: { repos: { listReleases() {} } },
  };
  assert.equal((await previousPublishedRelease(github, 'o', 'r', '2')).tag_name, '1');
  assert.equal(await previousPublishedRelease(github, 'o', 'r', 'missing'), null);
});
