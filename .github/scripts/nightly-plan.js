'use strict';

// Decides what a run of the nightly workflow does: build or not, and whether
// the build is published (Docker's `nightly` tag and the rolling `nightly`
// prerelease) or is only a dry run.

const PUBLISH_REF = 'refs/heads/master';
const NIGHTLY_TAG = 'nightly';
// The file nightly.yml uploads to the nightly release.
const NIGHTLY_ASSET = 'TwitchChannelPointsMiner-Portable-nightly.zip';
const TESTS_WORKFLOW = 'tests.yml';

// The commit a tag points to, or null when the tag doesn't exist.
async function tagCommit(github, owner, repo, tag) {
  try {
    const { data } = await github.rest.repos.getCommit({ owner, repo, ref: `tags/${tag}` });
    return data.sha;
  } catch (error) {
    // 422 is how the API answers for a ref that doesn't resolve to a commit.
    if (error.status === 404 || error.status === 422) return null;
    throw error;
  }
}

// True when the nightly release is published and has its download. The tag
// alone can't tell: a run that failed while replacing the file leaves the
// tag on its commit with nothing to download.
async function nightlyReleaseComplete(github, owner, repo) {
  try {
    // Drafts are not found by tag, so a half-created release counts as missing.
    const { data } = await github.rest.repos.getReleaseByTag({ owner, repo, tag: NIGHTLY_TAG });
    return (data.assets || []).some(
      (asset) => asset.name === NIGHTLY_ASSET && asset.state === 'uploaded',
    );
  } catch (error) {
    if (error.status === 404) return false;
    throw error;
  }
}

// The tag of the latest full release, or null before the first one. GitHub
// never reports a prerelease here, so the nightly release itself can't be it.
async function latestReleaseTag(github, owner, repo) {
  try {
    const { data } = await github.rest.repos.getLatestRelease({ owner, repo });
    return data.tag_name || null;
  } catch (error) {
    if (error.status === 404) return null;
    throw error;
  }
}

// How the push that put `sha` on master did in the Tests workflow: 'success',
// another conclusion, the status of a run still going, or 'missing'.
async function testsOutcome(github, owner, repo, sha) {
  const { data } = await github.rest.actions.listWorkflowRuns({
    owner, repo, workflow_id: TESTS_WORKFLOW, head_sha: sha, event: 'push', per_page: 1,
  });
  const run = (data.workflow_runs || [])[0];
  if (!run) return 'missing';
  return run.status === 'completed' ? run.conclusion || 'unknown' : run.status;
}

// True when `sha` is part of the history of `head`.
async function contains(github, owner, repo, head, sha) {
  const { data } = await github.rest.repos.compareCommitsWithBasehead({
    owner, repo, basehead: `${sha}...${head}`, per_page: 1, page: 1,
  });
  return data.status === 'ahead' || data.status === 'identical';
}

// True when the nightly build at `nightly` already contains `sha` and more.
async function isOlderThan(github, owner, repo, sha, nightly) {
  return Boolean(nightly) && nightly !== sha && (await contains(github, owner, repo, nightly, sha));
}

// Whether this run publishes, and why not when it doesn't.
function publishDecision({ eventName, ref, publishRequested, hasDockerCredentials }) {
  if (eventName !== 'schedule' && eventName !== 'workflow_dispatch') {
    return { publish: false, reason: `a ${eventName} run never publishes` };
  }
  if (eventName === 'workflow_dispatch' && !publishRequested) {
    return { publish: false, reason: 'publishing was not requested for this manual run' };
  }
  if (ref !== PUBLISH_REF) {
    return { publish: false, reason: `only ${PUBLISH_REF} is published, not ${ref}` };
  }
  if (!hasDockerCredentials) {
    return { publish: false, reason: 'Docker Hub credentials are not configured here' };
  }
  return { publish: true, reason: '' };
}

async function planNightly({
  github, context, core, sha, publishRequested = false, force = false, hasDockerCredentials = false,
}) {
  const { owner, repo } = context.repo;
  const releaseTag = await latestReleaseTag(github, owner, repo);
  const { publish, reason: dryRunReason } = publishDecision({
    eventName: context.eventName, ref: context.ref, publishRequested, hasDockerCredentials,
  });

  let build = true;
  let reason;
  if (!publish) {
    // A scheduled run exists to publish. One that can't (a fork that turned
    // schedules on) has nothing to do; a manual or pull request run is a
    // dry run that still builds.
    build = context.eventName !== 'schedule';
    reason = build
      ? `Dry run: building without publishing, because ${dryRunReason}.`
      : `Nothing to do: ${dryRunReason}.`;
  } else {
    const tests = await testsOutcome(github, owner, repo, sha);
    const nightly = tests === 'success' ? await tagCommit(github, owner, repo, NIGHTLY_TAG) : null;
    if (tests !== 'success') {
      build = false;
      reason = `Skipped: the Tests run for ${sha} is ${tests}, not success.`;
      core.warning(reason);
    } else if (nightly === sha && !force && (await nightlyReleaseComplete(github, owner, repo))) {
      build = false;
      reason = `Skipped: the last nightly build was already made from ${sha}.`;
    } else if (await isOlderThan(github, owner, repo, sha, nightly)) {
      // A re-run of an earlier night's run must not move nightly backwards.
      build = false;
      reason = `Skipped: the last nightly build (${nightly}) is newer than ${sha}.`;
    } else if (!force && releaseTag && (await tagCommit(github, owner, repo, releaseTag)) === sha) {
      build = false;
      reason = `Skipped: ${sha} is release ${releaseTag}, with nothing merged since.`;
    } else {
      reason = `Building and publishing ${sha}.`;
    }
  }

  core.notice(reason);
  core.setOutput('build', build ? 'true' : 'false');
  core.setOutput('publish', build && publish ? 'true' : 'false');
  core.setOutput('release_tag', releaseTag || '');
  return { build, publish: build && publish, reason, releaseTag };
}

// Run by each job that publishes, just before it does. The plan's answer
// can be stale by then: "Re-run failed jobs" on an earlier night's run keeps
// the plan job's outputs, so the plan's own check doesn't run again.
async function assertNotSuperseded({ github, context, core, sha }) {
  const { owner, repo } = context.repo;
  const nightly = await tagCommit(github, owner, repo, NIGHTLY_TAG);
  if (await isOlderThan(github, owner, repo, sha, nightly)) {
    core.setFailed(
      `The nightly build is already at ${nightly}, which is newer than ${sha}. ` +
      'This run is out of date and publishes nothing.',
    );
    return false;
  }
  return true;
}

module.exports = {
  assertNotSuperseded,
  planNightly,
  publishDecision,
};
