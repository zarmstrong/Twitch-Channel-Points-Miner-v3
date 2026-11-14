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

const BOT = { login: 'github-actions[bot]', type: 'Bot' };
// Releases 1 and 2; the current cycle starts when 2 was published.
const RELEASES = [
  { tag_name: '2', draft: false, published_at: '2026-02-01T00:00:00Z' },
  { tag_name: '1', draft: false, published_at: '2026-01-01T00:00:00Z' },
];

function marker(createdAt = '2026-02-10T00:00:00Z', user = BOT) {
  return {
    body: '<!-- awaiting-release -->\nThe fix in #9 was merged and is awaiting release.',
    user,
    created_at: createdAt,
  };
}

function markingGithub({ pulls, mutations, comments = [], compare, releases = RELEASES }) {
  const listPullRequestsAssociatedWithCommit = () => {};
  const listComments = () => {};
  const listReleases = () => {};
  return {
    graphql: async () => ({ repository: { pullRequest: {
      closingIssuesReferences: {
        nodes: [{
          repository: { nameWithOwner: 'o/r' },
          number: 1,
          state: 'OPEN',
          labels: { nodes: [{ name: 'awaiting-release' }] },
        }],
        pageInfo: { hasNextPage: false },
      },
    } } }),
    paginate: async (method, ...args) => {
      if (method === listPullRequestsAssociatedWithCommit) {
        return typeof pulls === 'function' ? pulls(args[0].commit_sha) : pulls;
      }
      if (method === listComments) return comments;
      if (method === listReleases) return releases;
      throw new Error('Unexpected paginated method');
    },
    rest: {
      repos: {
        listReleases,
        listPullRequestsAssociatedWithCommit,
        compareCommitsWithBasehead: async (input) => {
          if (!compare) throw new Error('Unexpected compare');
          return compare(input);
        },
      },
      issues: {
        listComments,
        addLabels: async (input) => mutations.push(['label', input]),
        createComment: async (input) => mutations.push(['comment', input]),
      },
    },
  };
}

test('marking is idempotent when label and comment already exist', async () => {
  const mutations = [];
  await markIssuesAwaitingRelease({
    github: markingGithub({
      pulls: [{ number: 9, merged_at: '2026-01-01', merge_commit_sha: 'abc', labels: [] }],
      mutations,
      comments: [marker()],
    }),
    context: {
      repo: { owner: 'o', repo: 'r' },
      payload: { after: 'abc', commits: [{ id: 'abc' }] },
    },
    core: { info() {} },
  });
  assert.deepEqual(mutations, []);
});

test('marking finds the merged PR from the pushed merge commit', async () => {
  const mutations = [];
  await markIssuesAwaitingRelease({
    github: markingGithub({
      pulls: [{ number: 9, merged_at: '2026-01-01', merge_commit_sha: 'abc', labels: [] }],
      mutations,
    }),
    context: {
      repo: { owner: 'o', repo: 'r' },
      payload: { after: 'abc', commits: [{ id: 'abc' }] },
    },
    core: { info() {} },
  });
  assert.equal(mutations.length, 1);
  assert.equal(mutations[0][0], 'comment');
  assert.match(mutations[0][1].body, /The fix in #9 was merged/);
});

test('marking includes the pushed head when the commit list is truncated', async () => {
  const mutations = [];
  await markIssuesAwaitingRelease({
    github: markingGithub({
      pulls: [{ number: 9, merged_at: '2026-01-01', merge_commit_sha: 'abc', labels: [] }],
      mutations,
    }),
    context: {
      repo: { owner: 'o', repo: 'r' },
      payload: { after: 'abc', commits: [{ id: 'older' }] },
    },
    core: { info() {} },
  });
  assert.equal(mutations.length, 1);
  assert.match(mutations[0][1].body, /The fix in #9 was merged/);
});

test('marking skips direct pushes and Release Please merges', async () => {
  for (const pulls of [
    [],
    [{ number: 9, merged_at: '2026-01-01', merge_commit_sha: 'other', labels: [] }],
    [{
      number: 9,
      merged_at: '2026-01-01',
      merge_commit_sha: 'abc',
      labels: [{ name: 'autorelease: pending' }],
    }],
  ]) {
    const mutations = [];
    const info = [];
    await markIssuesAwaitingRelease({
      github: markingGithub({ pulls, mutations }),
      context: {
        repo: { owner: 'o', repo: 'r' },
        payload: { after: 'abc', commits: [{ id: 'abc' }] },
      },
      core: { info: (message) => info.push(message) },
    });
    assert.deepEqual(mutations, []);
    assert.match(info.at(-1), /No merged ordinary PR/);
  }
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
          repository: { nameWithOwner: 'o/r' },
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

function pullFor(sha) {
  const numbers = { m1: 11, m2: 12 };
  return numbers[sha]
    ? [{ number: numbers[sha], merged_at: '2026-01-01', merge_commit_sha: sha, labels: [] }]
    : [];
}

test('marking recovers merges the push payload left out', async () => {
  const mutations = [];
  const compared = [];
  await markIssuesAwaitingRelease({
    github: markingGithub({
      pulls: pullFor,
      mutations,
      compare: async (input) => {
        compared.push(input.basehead);
        return { data: { status: 'ahead', total_commits: 3, commits: [
          { sha: 'm1' }, { sha: 'm2' }, { sha: 'direct' },
        ] } };
      },
    }),
    context: {
      repo: { owner: 'o', repo: 'r' },
      // The truncated payload lists only the last commit, a direct one.
      payload: { before: 'b0', after: 'direct', commits: [{ id: 'direct' }] },
    },
    core: { info() {} },
  });
  assert.deepEqual(compared, ['b0...direct']);
  const commented = mutations.filter(([kind]) => kind === 'comment').map(([, input]) => input.body);
  assert.equal(commented.length, 2);
  assert.match(commented[0], /The fix in #11 was merged/);
  assert.match(commented[1], /The fix in #12 was merged/);
});

test('marking falls back to the payload when a force push diverges', async () => {
  const mutations = [];
  await markIssuesAwaitingRelease({
    github: markingGithub({
      pulls: pullFor,
      mutations,
      compare: async () => ({ data: { status: 'diverged', total_commits: 0, commits: [] } }),
    }),
    context: {
      repo: { owner: 'o', repo: 'r' },
      payload: { before: 'b0', after: 'm1', commits: [{ id: 'm1' }] },
    },
    core: { info() {} },
  });
  assert.equal(mutations.filter(([kind]) => kind === 'comment').length, 1);
});

test('marking skips the compare for a new branch or deleted head', async () => {
  for (const payload of [
    { before: '0000000000000000000000000000000000000000', after: 'm1', commits: [{ id: 'm1' }] },
    { before: 'b0', after: '0000000000000000000000000000000000000000', commits: [] },
  ]) {
    const mutations = [];
    await markIssuesAwaitingRelease({
      github: markingGithub({ pulls: pullFor, mutations }),
      context: { repo: { owner: 'o', repo: 'r' }, payload },
      core: { info() {} },
    });
    assert.equal(mutations.length, payload.after === 'm1' ? 1 : 0);
  }
});

function closingGithub({ issue, issues, comments = [], calls }) {
  const listReleases = () => {};
  const listPullRequestsAssociatedWithCommit = () => {};
  const listComments = () => {};
  return {
    graphql: async () => ({ repository: { pullRequest: {
      closingIssuesReferences: {
        nodes: (issues || [issue]).map((node) => ({ repository: { nameWithOwner: 'o/r' }, ...node })),
        pageInfo: { hasNextPage: false },
      },
    } } }),
    paginate: async (method) => {
      if (method === listReleases) {
        return [
          { tag_name: '3', draft: false, published_at: '2026-03-01T00:00:00Z' },
          ...RELEASES,
        ];
      }
      if (method === listPullRequestsAssociatedWithCommit) {
        return [{ number: 9, merged_at: '2026-01-15T00:00:00Z', merge_commit_sha: 'abc', labels: [] }];
      }
      if (method === listComments) return comments;
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
}

async function closeWith({ tag = '2', ...options }) {
  const calls = [];
  await closeReleasedIssues({
    github: closingGithub({ ...options, calls }),
    context: { repo: { owner: 'o', repo: 'r' } },
    core: { info() {} },
    tag,
  });
  return calls;
}

test('release closes an issue whose marking run never happened', async () => {
  // A merge pushed with [skip ci], or released before its marking run finished.
  const calls = await closeWith({
    issue: { number: 42, state: 'OPEN', labels: { nodes: [] } },
  });
  assert.deepEqual(calls.map(([kind]) => kind), ['comment', 'update', 'remove']);
  assert.equal(calls[0][1].body, 'Released in 2.');
  assert.equal(calls[1][1].state, 'closed');
});

test('release leaves open an issue whose label was removed after marking', async () => {
  const calls = await closeWith({
    issue: { number: 42, state: 'OPEN', labels: { nodes: [] } },
    comments: [marker('2026-01-20T00:00:00Z')],
  });
  assert.deepEqual(calls, []);
});

test('release ignores an unlabelled issue that is already closed', async () => {
  const calls = await closeWith({
    issue: { number: 42, state: 'CLOSED', labels: { nodes: [] } },
  });
  assert.deepEqual(calls, []);
});

test('release never touches a same-numbered local issue for an external reference', async () => {
  for (const labels of [[], [{ name: 'awaiting-release' }]]) {
    const calls = await closeWith({
      issue: {
        number: 42,
        state: 'OPEN',
        labels: { nodes: labels },
        repository: { nameWithOwner: 'other/repo' },
      },
    });
    assert.deepEqual(calls, []);
  }
});

test('marking never labels a same-numbered local issue for an external reference', async () => {
  const mutations = [];
  const github = markingGithub({
    pulls: [{ number: 9, merged_at: '2026-01-01', merge_commit_sha: 'abc', labels: [] }],
    mutations,
  });
  github.graphql = async () => ({ repository: { pullRequest: {
    closingIssuesReferences: {
      nodes: [
        { number: 1, state: 'OPEN', labels: { nodes: [] }, repository: { nameWithOwner: 'other/repo' } },
        { number: 2, state: 'OPEN', labels: { nodes: [] }, repository: { nameWithOwner: 'O/R' } },
      ],
      pageInfo: { hasNextPage: false },
    },
  } } });
  await markIssuesAwaitingRelease({
    github,
    context: { repo: { owner: 'o', repo: 'r' }, payload: { after: 'abc', commits: [{ id: 'abc' }] } },
    core: { info() {} },
  });
  assert.deepEqual(
    mutations.map(([kind, input]) => [kind, input.issue_number]),
    [['label', 2], ['comment', 2]],
  );
});

test('a marking rerun keeps the opt-out of a maintainer who removed the label', async () => {
  // Marked once, then the label was removed by hand to keep the issue open.
  // Marked after release 2, which marking treats as the latest one.
  const current = marker('2026-02-10T00:00:00Z');
  const mutations = [];
  const github = markingGithub({
    pulls: [{ number: 9, merged_at: '2026-01-01', merge_commit_sha: 'abc', labels: [] }],
    mutations,
    comments: [current],
  });
  github.graphql = async () => ({ repository: { pullRequest: {
    closingIssuesReferences: {
      nodes: [{ number: 42, state: 'OPEN', labels: { nodes: [] }, repository: { nameWithOwner: 'o/r' } }],
      pageInfo: { hasNextPage: false },
    },
  } } });
  await markIssuesAwaitingRelease({
    github,
    context: { repo: { owner: 'o', repo: 'r' }, payload: { after: 'abc', commits: [{ id: 'abc' }] } },
    core: { info() {} },
  });
  assert.deepEqual(mutations, []);

  // Release 3, the one that follows, leaves the issue open as well.
  const calls = await closeWith({
    issue: { number: 42, state: 'OPEN', labels: { nodes: [] } },
    comments: [current],
    tag: '3',
  });
  assert.deepEqual(calls, []);
});

test('marking adds the label and comment to an issue marked for the first time', async () => {
  const mutations = [];
  const github = markingGithub({
    pulls: [{ number: 9, merged_at: '2026-01-01', merge_commit_sha: 'abc', labels: [] }],
    mutations,
  });
  github.graphql = async () => ({ repository: { pullRequest: {
    closingIssuesReferences: {
      nodes: [{ number: 42, state: 'OPEN', labels: { nodes: [] }, repository: { nameWithOwner: 'o/r' } }],
      pageInfo: { hasNextPage: false },
    },
  } } });
  await markIssuesAwaitingRelease({
    github,
    context: { repo: { owner: 'o', repo: 'r' }, payload: { after: 'abc', commits: [{ id: 'abc' }] } },
    core: { info() {} },
  });
  assert.deepEqual(mutations.map(([kind]) => kind), ['label', 'comment']);
});

function issueGraphql(issue) {
  return async () => ({ repository: { pullRequest: {
    closingIssuesReferences: {
      nodes: [{ repository: { nameWithOwner: 'o/r' }, ...issue }],
      pageInfo: { hasNextPage: false },
    },
  } } });
}

async function markWith(options, issue) {
  const mutations = [];
  const github = markingGithub({
    pulls: [{ number: 9, merged_at: '2026-01-01', merge_commit_sha: 'abc', labels: [] }],
    mutations,
    ...options,
  });
  github.graphql = issueGraphql(issue);
  await markIssuesAwaitingRelease({
    github,
    context: { repo: { owner: 'o', repo: 'r' }, payload: { after: 'abc', commits: [{ id: 'abc' }] } },
    core: { info() {} },
  });
  return mutations.map(([kind]) => kind);
}

const UNLABELLED = { number: 42, state: 'OPEN', labels: { nodes: [] } };

test('a reopened issue is marked and closed again for its next fix', async () => {
  // Marked before release 2 shipped it; then reopened and fixed again.
  const shipped = marker('2026-01-20T00:00:00Z');
  assert.deepEqual(await markWith({ comments: [shipped] }, UNLABELLED), ['label', 'comment']);
  // If that marking run was skipped, release 3 still closes it.
  const calls = await closeWith({ issue: UNLABELLED, comments: [shipped], tag: '3' });
  assert.deepEqual(calls.map(([kind]) => kind), ['comment', 'update', 'remove']);
});

test('a marker posted by someone else is not workflow state', async () => {
  for (const user of [{ login: 'someone', type: 'User' }, { login: 'github-actions[bot]', type: 'User' }]) {
    const forged = marker('2026-02-10T00:00:00Z', user);
    assert.deepEqual(await markWith({ comments: [forged] }, UNLABELLED), ['label', 'comment']);
    const calls = await closeWith({ issue: UNLABELLED, comments: [marker('2026-01-20T00:00:00Z', user)] });
    assert.deepEqual(calls.map(([kind]) => kind), ['comment', 'update', 'remove']);
  }
});

test('a bot comment that only quotes the marker is not workflow state', async () => {
  const quoted = { ...marker(), body: '> <!-- awaiting-release -->' };
  assert.deepEqual(await markWith({ comments: [quoted] }, UNLABELLED), ['label', 'comment']);
});

test('marking with no published release yet trusts any automation marker', async () => {
  assert.deepEqual(
    await markWith({ comments: [marker('2025-01-01T00:00:00Z')], releases: [] }, UNLABELLED),
    [],
  );
});
