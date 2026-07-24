'use strict';

const assert = require('node:assert/strict');
const test = require('node:test');
const {
  closeReleasedIssues,
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

test('release orchestration comments, closes, and tolerates an already removed label', async () => {
  const calls = [];
  const listReleases = () => {};
  const listPullRequestsAssociatedWithCommit = () => {};
  const listComments = () => {};
  const github = {
    graphql: async () => ({ repository: { pullRequest: {
      closingIssuesReferences: {
        nodes: [{
          number: 42,
          state: 'OPEN',
          labels: { nodes: [{ name: 'awaiting-release' }] },
        }],
        pageInfo: { hasNextPage: false },
      },
    } } }),
    paginate: async (method) => {
      if (method === listReleases) {
        return [
          { tag_name: '2', draft: false, published_at: '2026-02-01T00:00:00Z' },
          { tag_name: '1', draft: false, published_at: '2026-01-01T00:00:00Z' },
        ];
      }
      if (method === listPullRequestsAssociatedWithCommit) {
        return [{
          number: 9,
          merged_at: '2026-01-15T00:00:00Z',
          merge_commit_sha: 'abc',
          labels: [],
        }];
      }
      if (method === listComments) return [];
      throw new Error('Unexpected paginated method');
    },
    rest: {
      repos: {
        listReleases,
        listPullRequestsAssociatedWithCommit,
        compareCommitsWithBasehead: async () => ({ data: {
          status: 'ahead', total_commits: 1, commits: [{ sha: 'abc' }],
        } }),
      },
      issues: {
        listComments,
        createComment: async (input) => calls.push(['comment', input]),
        update: async (input) => calls.push(['update', input]),
        removeLabel: async (input) => {
          calls.push(['remove', input]);
          const error = new Error('Label does not exist');
          error.status = 404;
          throw error;
        },
      },
    },
  };
  const info = [];
  await closeReleasedIssues({
    github,
    context: { repo: { owner: 'o', repo: 'r' } },
    core: { info: (message) => info.push(message) },
    tag: '2',
  });

  assert.equal(calls[0][0], 'comment');
  assert.equal(calls[0][1].body, 'Released in 2.');
  assert.deepEqual(
    { state: calls[1][1].state, reason: calls[1][1].state_reason },
    { state: 'closed', reason: 'completed' },
  );
  assert.equal(calls[2][0], 'remove');
  assert.match(info.at(-1), /no longer has awaiting-release/);
});
