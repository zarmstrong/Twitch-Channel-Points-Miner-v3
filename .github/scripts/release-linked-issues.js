'use strict';

const AWAITING_LABEL = 'awaiting-release';
const AWAITING_MARKER = '<!-- awaiting-release -->';
// Marker comments are workflow state only when this automation wrote them.
const AUTOMATION_LOGIN = 'github-actions[bot]';
const RELEASE_LABELS = new Set(['autorelease: pending', 'autorelease: tagged']);
// Marker comments name the PR whose merge marked the issue.
const MARKER_PULL = /^The fix in #(\d+) was merged/m;

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

// The comment automation posted with exactly this body, if any.
async function automationComment(github, owner, repo, issueNumber, body) {
  const comments = await github.paginate(github.rest.issues.listComments, {
    owner, repo, issue_number: issueNumber, per_page: 100,
  });
  return comments.find((comment) => comment.body === body && isAutomation(comment.user));
}

function isAutomation(user) {
  return user?.login === AUTOMATION_LOGIN && user?.type === 'Bot';
}

function isMarkerComment(comment) {
  return (
    typeof comment.body === 'string' &&
    comment.body.startsWith(AWAITING_MARKER) &&
    isAutomation(comment.user)
  );
}

// True when this release already closed the issue and someone reopened it
// afterwards, so a rerun of release closing must leave it open. When closing
// failed after the release comment was posted, there is no reopen to find
// and the rerun closes it.
async function reopenedAfterRelease(github, owner, repo, issueNumber, releaseComment) {
  const released = await automationComment(github, owner, repo, issueNumber, releaseComment);
  if (!released) return false;
  const events = await github.paginate(github.rest.issues.listEvents, {
    owner, repo, issue_number: issueNumber, per_page: 100,
  });
  return events.some(
    (event) => event.event === 'reopened' && new Date(event.created_at) >= new Date(released.created_at),
  );
}

// A release cycle follows the release `tag`. `since` is the commit time of
// that tag, and `pulls` caches merge commits by PR number.
async function releaseCycle(github, owner, repo, release) {
  if (!release) return null;
  return {
    tag: release.tag_name,
    since: await releaseCutoff(github, owner, repo, release),
    pulls: new Map(),
  };
}

// A marker belongs to the current cycle when the release the cycle follows
// doesn't contain the fix it names. Timing can't tell: a marking run can
// finish after the tag for a fix merged before it. A marker that names no
// merged PR falls back to being posted after the tag's commit.
async function isCurrentMarker(github, owner, repo, comment, cycle) {
  if (!cycle) return true;
  const match = MARKER_PULL.exec(comment.body);
  if (match) {
    const number = Number(match[1]);
    if (!cycle.pulls.has(number)) {
      const { data: pull } = await github.rest.pulls.get({ owner, repo, pull_number: number });
      cycle.pulls.set(number, pull.merged_at ? pull.merge_commit_sha : null);
    }
    const sha = cycle.pulls.get(number);
    if (sha) return !(await releaseContains(github, owner, repo, cycle.tag, sha));
  }
  return new Date(comment.created_at) > new Date(cycle.since);
}

// True when automation marked the issue in the current release cycle. A
// marker from an earlier cycle belongs to a fix that has already shipped.
async function hasCurrentMarker(github, owner, repo, issueNumber, cycle) {
  const comments = await github.paginate(github.rest.issues.listComments, {
    owner, repo, issue_number: issueNumber, per_page: 100,
  });
  for (const comment of comments) {
    if (isMarkerComment(comment) && (await isCurrentMarker(github, owner, repo, comment, cycle))) {
      return true;
    }
  }
  return false;
}

async function markPullIssues(github, owner, repo, pull, cycle) {
  const issues = uniqueOpenIssues(await linkedIssues(github, owner, repo, pull.number));
  for (const issue of issues) {
    // The label goes on before the marker comment, so a current marker
    // without the label means a maintainer removed it to keep the issue
    // open: a rerun must not put it back.
    if (await hasCurrentMarker(github, owner, repo, issue.number, cycle)) continue;
    // A release may have closed the issue since it was listed above.
    const { data: current } = await github.rest.issues.get({
      owner, repo, issue_number: issue.number,
    });
    if (current.state !== 'open') continue;
    if (!issue.labels.nodes.some((label) => label.name === AWAITING_LABEL)) {
      await github.rest.issues.addLabels({
        owner, repo, issue_number: issue.number, labels: [AWAITING_LABEL],
      });
    }
    await github.rest.issues.createComment({
      owner,
      repo,
      issue_number: issue.number,
      body: `${AWAITING_MARKER}\nThe fix in #${pull.number} was merged and is awaiting release.`,
    });
  }
}

async function pushedCommitShas(github, owner, repo, payload) {
  // The payload's commit list can be truncated, so include the pushed head and
  // every commit in before...after. After a force push the comparison diverges
  // or fails (the old head may be gone); keep the payload's commits then.
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
        const comparable = error.status === 404 || error.status === 422;
        if (!comparable && !/have diverged/.test(error.message)) throw error;
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
  const latest = (await publishedReleases(github, owner, repo))[0];
  const cycle = await releaseCycle(github, owner, repo, latest);
  for (const pull of pulls) {
    // A rerun of an old push can come after the release that shipped it.
    // Its marker then belongs to an earlier cycle, so without this check an
    // issue kept open by removing the label would be marked again.
    if (latest && (await releaseContains(github, owner, repo, latest.tag_name, pull.merge_commit_sha))) {
      core.info(`PR #${pull.number} already shipped in ${latest.tag_name}; not marking its issues`);
      continue;
    }
    await markPullIssues(github, owner, repo, pull, cycle);
  }
}

// True when `sha` is part of the history of `tag`.
async function releaseContains(github, owner, repo, tag, sha) {
  const { data } = await github.rest.repos.compareCommitsWithBasehead({
    owner, repo, basehead: `${sha}...${tag}`, per_page: 1, page: 1,
  });
  return data.status === 'ahead' || data.status === 'identical';
}

// Published releases, newest first. Prereleases are left out unless asked
// for. A release cycle runs from one full release to the next: a prerelease,
// such as the rolling nightly build, neither ends one nor closes issues, so
// the fixes it contains are still awaiting release.
async function publishedReleases(github, owner, repo, { prereleases = false } = {}) {
  const releases = await github.paginate(github.rest.repos.listReleases, {
    owner, repo, per_page: 100,
  });
  return releases
    .filter((release) => !release.draft && release.published_at)
    .filter((release) => prereleases || !release.prerelease)
    .sort((left, right) => new Date(right.published_at) - new Date(left.published_at));
}

// The start of the release cycle that follows `release`: the commit time of
// its tag. Release Please pushes the tag before it publishes the release, so
// a fix merged in between belongs to the next cycle, not this one.
async function releaseCutoff(github, owner, repo, release) {
  const { data } = await github.rest.repos.getCommit({
    owner, repo, ref: release.tag_name,
  });
  return data.commit.committer.date;
}

// The newest full release published before `newTag`, or null when `newTag`
// isn't a published release.
async function previousPublishedRelease(github, owner, repo, newTag) {
  const published = await publishedReleases(github, owner, repo, { prereleases: true });
  const current = published.find((release) => release.tag_name === newTag);
  if (!current) return null;
  return published.find(
    (release) =>
      !release.prerelease &&
      new Date(release.published_at) < new Date(current.published_at),
  ) || null;
}

// True when `tag` is a published prerelease.
async function isPrerelease(github, owner, repo, tag) {
  const published = await publishedReleases(github, owner, repo, { prereleases: true });
  return published.some((release) => release.tag_name === tag && release.prerelease);
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
  // Closing here would leave the cycle open: the markers of the fixes a
  // prerelease closed would still count as current, so a later fix for a
  // reopened issue would never be marked.
  if (await isPrerelease(github, owner, repo, tag)) {
    core.info(`${tag} is a prerelease; its issues stay open until a full release ships them`);
    return;
  }
  const previous = await previousPublishedRelease(github, owner, repo, tag);
  if (!previous) throw new Error(`Cannot determine a previous published release before ${tag}`);

  const cycle = await releaseCycle(github, owner, repo, previous);
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
  // for it in this release cycle: a push skipped with [skip ci], or one still
  // being marked when the release was published. An issue marked in this
  // cycle whose label was removed by hand stays open. An issue this release
  // already closed that was reopened afterwards is left alone entirely, label
  // included, since a later fix may have marked it again.
  const releaseComment = `Released in ${tag}.`;
  const candidates = new Map();
  const unmarked = new Set();
  const seen = new Set();
  for (const pull of pulls) {
    const issues = await linkedIssues(github, owner, repo, pull.number);
    for (const issue of issues) {
      if (seen.has(issue.number)) continue;
      seen.add(issue.number);
      if (
        issue.state === 'OPEN' &&
        (await reopenedAfterRelease(github, owner, repo, issue.number, releaseComment))
      ) {
        core.info(`Issue #${issue.number} was reopened after ${tag} closed it; leaving it open`);
        continue;
      }
      if (issue.labels.nodes.some((label) => label.name === AWAITING_LABEL)) {
        candidates.set(issue.number, issue);
      } else if (
        issue.state === 'OPEN' &&
        !(await hasCurrentMarker(github, owner, repo, issue.number, cycle))
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
      if (!(await automationComment(github, owner, repo, issue.number, releaseComment))) {
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
