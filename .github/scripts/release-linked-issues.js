'use strict';

const AWAITING_LABEL = 'awaiting-release';
const AWAITING_MARKER = '<!-- awaiting-release -->';
const RELEASE_LABELS = new Set(['autorelease: pending', 'autorelease: tagged']);

function isReleasePleasePullRequest(labels) {
  return labels.some((label) => RELEASE_LABELS.has(label));
}

function uniqueOpenIssues(nodes) {
  const issues = new Map();
  for (const issue of nodes || []) {
    if (issue.state === 'OPEN') issues.set(issue.number, issue);
  }
  return [...issues.values()];
}

function releasePullRequests(commitShas, associations) {
  const shas = new Set(commitShas);
  const pulls = new Map();
  for (const [sha, associatedPulls] of Object.entries(associations)) {
    if (!shas.has(sha)) continue;
    for (const pull of associatedPulls) {
      const labels = (pull.labels || []).map((label) => label.name);
      if (
        pull.merged_at &&
        pull.merge_commit_sha === sha &&
        !isReleasePleasePullRequest(labels)
      ) {
        pulls.set(pull.number, pull);
      }
    }
  }
  return [...pulls.values()];
}

function shouldCloseIssue(issue) {
  return (
    issue.state === 'OPEN' &&
    issue.labels.nodes.some((label) => label.name === AWAITING_LABEL)
  );
}

async function linkedIssues(github, owner, repo, pullNumber) {
  const result = await github.graphql(
    `query($owner: String!, $repo: String!, $number: Int!) {
      repository(owner: $owner, name: $repo) {
        pullRequest(number: $number) {
          closingIssuesReferences(first: 100) {
            nodes {
              number state labels(first: 100) { nodes { name } }
              repository { nameWithOwner }
            }
            pageInfo { hasNextPage }
          }
        }
      }
    }`,
    { owner, repo, number: pullNumber },
  );
  const references = result.repository.pullRequest.closingIssuesReferences;
  if (references.pageInfo.hasNextPage) {
    throw new Error(`PR #${pullNumber} links more than 100 issues; refusing partial processing`);
  }
  // A PR can close issues in other repositories. Every lookup and change here
  // uses this repository, so another repository's #N must not stand for ours.
  const current = `${owner}/${repo}`.toLowerCase();
  return references.nodes.filter(
    (issue) => issue.repository?.nameWithOwner?.toLowerCase() === current,
  );
}

async function hasComment(github, owner, repo, issueNumber, marker) {
  const comments = await github.paginate(github.rest.issues.listComments, {
    owner, repo, issue_number: issueNumber, per_page: 100,
  });
  return comments.some((comment) => comment.body && comment.body.includes(marker));
}

async function markPullIssues(github, owner, repo, pull) {
  const issues = uniqueOpenIssues(await linkedIssues(github, owner, repo, pull.number));
  for (const issue of issues) {
    if (!issue.labels.nodes.some((label) => label.name === AWAITING_LABEL)) {
      await github.rest.issues.addLabels({
        owner, repo, issue_number: issue.number, labels: [AWAITING_LABEL],
      });
    }
    if (!(await hasComment(github, owner, repo, issue.number, AWAITING_MARKER))) {
      await github.rest.issues.createComment({
        owner,
        repo,
        issue_number: issue.number,
        body: `${AWAITING_MARKER}\nThe fix in #${pull.number} was merged and is awaiting release.`,
      });
    }
  }
}

async function pushedCommitShas(github, owner, repo, payload) {
  // The payload's commit list can be truncated, so include the pushed head and
  // every commit in before...after. A force push diverges; keep the payload's.
  const isSha = (sha) => Boolean(sha) && !/^0+$/.test(sha);
  const shas = new Set((payload.commits || []).map((commit) => commit.id));
  if (isSha(payload.after)) {
    shas.add(payload.after);
    if (isSha(payload.before)) {
      try {
        for (const sha of await commitsBetween(github, owner, repo, payload.before, payload.after)) {
          shas.add(sha);
        }
      } catch (error) {
        if (!/have diverged/.test(error.message)) throw error;
      }
    }
  }
  return [...shas];
}

async function markIssuesAwaitingRelease({ github, context, core }) {
  // Runs on push to the default branch, so the merged PR comes from its merge commit.
  const { owner, repo } = context.repo;
  const commitShas = await pushedCommitShas(github, owner, repo, context.payload);

  const associations = {};
  for (const sha of commitShas) {
    associations[sha] = await github.paginate(
      github.rest.repos.listPullRequestsAssociatedWithCommit,
      { owner, repo, commit_sha: sha, per_page: 100 },
    );
  }
  const pulls = releasePullRequests(commitShas, associations);
  if (!pulls.length) {
    core.info('No merged ordinary PR found for the pushed commits');
    return;
  }
  for (const pull of pulls) {
    await markPullIssues(github, owner, repo, pull);
  }
}

async function previousPublishedRelease(github, owner, repo, newTag) {
  const releases = await github.paginate(github.rest.repos.listReleases, {
    owner, repo, per_page: 100,
  });
  const published = releases.filter((release) => !release.draft && release.published_at);
  const current = published.find((release) => release.tag_name === newTag);
  if (!current) return null;
  return published
    .filter((release) => new Date(release.published_at) < new Date(current.published_at))
    .sort((left, right) => new Date(right.published_at) - new Date(left.published_at))[0] || null;
}

async function commitsBetween(github, owner, repo, previousTag, newTag) {
  const first = await github.rest.repos.compareCommitsWithBasehead({
    owner, repo, basehead: `${previousTag}...${newTag}`, per_page: 100, page: 1,
  });
  if (first.data.status === 'diverged') {
    throw new Error(`Release tags ${previousTag} and ${newTag} have diverged`);
  }
  const commits = [...first.data.commits];
  for (let page = 2; commits.length < first.data.total_commits; page += 1) {
    const response = await github.rest.repos.compareCommitsWithBasehead({
      owner, repo, basehead: `${previousTag}...${newTag}`, per_page: 100, page,
    });
    if (!response.data.commits.length) {
      throw new Error('Release comparison was truncated; refusing partial processing');
    }
    commits.push(...response.data.commits);
  }
  return commits.map((commit) => commit.sha);
}

async function closeReleasedIssues({ github, context, core, tag }) {
  const { owner, repo } = context.repo;
  const previous = await previousPublishedRelease(github, owner, repo, tag);
  if (!previous) throw new Error(`Cannot determine a previous published release before ${tag}`);

  const commitShas = await commitsBetween(github, owner, repo, previous.tag_name, tag);
  const associations = {};
  for (const sha of commitShas) {
    associations[sha] = await github.paginate(
      github.rest.repos.listPullRequestsAssociatedWithCommit,
      { owner, repo, commit_sha: sha, per_page: 100 },
    );
  }
  const pulls = releasePullRequests(commitShas, associations);
  core.info(`Found ${pulls.length} merged PR(s) in ${previous.tag_name}...${tag}`);

  // An issue is closed when it carries the label, or when marking never ran
  // for it: a push skipped with [skip ci], or one still being marked when the
  // release was published. A marked issue whose label was removed by hand
  // keeps its marker comment, so it stays open.
  const candidates = new Map();
  const unmarked = new Set();
  for (const pull of pulls) {
    const issues = await linkedIssues(github, owner, repo, pull.number);
    for (const issue of issues) {
      if (candidates.has(issue.number)) continue;
      if (issue.labels.nodes.some((label) => label.name === AWAITING_LABEL)) {
        candidates.set(issue.number, issue);
      } else if (
        issue.state === 'OPEN' &&
        !(await hasComment(github, owner, repo, issue.number, AWAITING_MARKER))
      ) {
        candidates.set(issue.number, issue);
        unmarked.add(issue.number);
      }
    }
  }

  for (const issue of candidates.values()) {
    if (unmarked.has(issue.number)) {
      core.info(`Issue #${issue.number} was never marked ${AWAITING_LABEL}; closing it`);
    }
    if (shouldCloseIssue(issue) || unmarked.has(issue.number)) {
      const releaseComment = `Released in ${tag}.`;
      if (!(await hasComment(github, owner, repo, issue.number, releaseComment))) {
        await github.rest.issues.createComment({
          owner, repo, issue_number: issue.number, body: releaseComment,
        });
      }
      await github.rest.issues.update({
        owner, repo, issue_number: issue.number, state: 'closed', state_reason: 'completed',
      });
    }
    try {
      await github.rest.issues.removeLabel({
        owner, repo, issue_number: issue.number, name: AWAITING_LABEL,
      });
    } catch (error) {
      if (error.status !== 404) throw error;
      core.info(`Issue #${issue.number} no longer has ${AWAITING_LABEL}`);
    }
  }
}

module.exports = {
  closeReleasedIssues,
  commitsBetween,
  isReleasePleasePullRequest,
  markIssuesAwaitingRelease,
  previousPublishedRelease,
  releasePullRequests,
  shouldCloseIssue,
  uniqueOpenIssues,
};
