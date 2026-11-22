'use strict';

// Decides what a run of the nightly workflow does: build or not, and whether
// the build is published (Docker's `nightly` tag and the rolling `nightly`
// prerelease) or is only a dry run.

const { isComplete, nightlyState, outOfDateReason, tagCommit } = require('./nightly-release');

const PUBLISH_REF = 'refs/heads/master';
const TESTS_WORKFLOW = 'tests.yml';

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

// Why a run that would publish `sha` must not, or null when it should.
async function skipReason({ github, owner, repo, sha, run, force, releaseTag }) {
  const tests = await testsOutcome(github, owner, repo, sha);
  if (tests !== 'success') {
    return { failedTests: true, reason: `the Tests run for ${sha} is ${tests}, not success` };
  }
  const state = await nightlyState(github, owner, repo);
  if (!force && isComplete(state, sha)) {
    return { reason: `the last nightly build was already made from ${sha}` };
  }
  // A re-run of an earlier night's run must not move nightly backwards.
  const outOfDate = await outOfDateReason(github, owner, repo, state, sha, run);
  if (outOfDate) return { reason: outOfDate };
  // A run that started publishing and failed is finished first, whatever
  // the commit: its note would otherwise stay on the release.
  if (
    !force && !state.publishing && releaseTag &&
    (await tagCommit(github, owner, repo, releaseTag)) === sha
  ) {
    return { reason: `${sha} is release ${releaseTag}, with nothing merged since` };
  }
  return null;
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
    const skip = await skipReason({
      github, owner, repo, sha, run: context.runId, force, releaseTag,
    });
    build = !skip;
    reason = skip ? `Skipped: ${skip.reason}.` : `Building and publishing ${sha}.`;
    if (skip?.failedTests) core.warning(reason);
  }

  core.notice(reason);
  core.setOutput('build', build ? 'true' : 'false');
  core.setOutput('publish', build && publish ? 'true' : 'false');
  core.setOutput('release_tag', releaseTag || '');
  return { build, publish: build && publish, reason, releaseTag };
}

module.exports = {
  planNightly,
  publishDecision,
};
