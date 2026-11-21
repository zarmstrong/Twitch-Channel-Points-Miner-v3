'use strict';

// Publishing a nightly build takes several steps in several jobs: the Docker
// push, the Windows upload, the release notes and the `nightly` tag. Any of
// them can fail, and a failed run can be re-run nights later. The nightly
// release records which run is publishing which commit, in a block at the
// top of its notes that is written before anything is pushed and removed by
// the last step. That block is what keeps runs from publishing over each
// other, and what tells a later run that the last one didn't finish. It
// names the run as well as the commit, because two runs can build the same
// commit into different builds: the version carries the build date.

const NIGHTLY_TAG = 'nightly';
// The file nightly.yml uploads to the nightly release.
const NIGHTLY_ASSET = 'TwitchChannelPointsMiner-Portable-nightly.zip';
const PUBLISHING_BLOCK =
  /<!-- nightly-publishing:start ([0-9a-f]{40}) (\d+) -->[\s\S]*?<!-- nightly-publishing:end -->\n*/;

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

// True when `sha` is part of the history of `head`.
async function contains(github, owner, repo, head, sha) {
  const { data } = await github.rest.repos.compareCommitsWithBasehead({
    owner, repo, basehead: `${sha}...${head}`, per_page: 1, page: 1,
  });
  return data.status === 'ahead' || data.status === 'identical';
}

// True when the build at `other` already contains `sha` and more.
async function isOlderThan(github, owner, repo, sha, other) {
  return Boolean(other) && other !== sha && (await contains(github, owner, repo, other, sha));
}

// The published nightly release, or null when there is none.
async function nightlyRelease(github, owner, repo) {
  try {
    const { data } = await github.rest.repos.getReleaseByTag({ owner, repo, tag: NIGHTLY_TAG });
    return data;
  } catch (error) {
    if (error.status === 404) return null;
    throw error;
  }
}

// The build a run has started publishing and not finished, as its commit
// and the id of the workflow run, or null. A re-run keeps its run's id.
function publishingBuild(release) {
  const match = PUBLISHING_BLOCK.exec(release?.body || '');
  return match ? { sha: match[1], run: match[2] } : null;
}

// The commit a run has started publishing and not finished, or null.
function publishingCommit(release) {
  return publishingBuild(release)?.sha || null;
}

// True when the release records this run as the one publishing `sha`.
function isPublishing(release, sha, run) {
  const build = publishingBuild(release);
  return Boolean(build) && build.sha === sha && build.run === String(run);
}

// Where nightly publishing stands: the commit the tag is on, the commit a
// run started publishing and didn't finish, and whether the download exists.
async function nightlyState(github, owner, repo) {
  const release = await nightlyRelease(github, owner, repo);
  return {
    release,
    tag: await tagCommit(github, owner, repo, NIGHTLY_TAG),
    publishing: publishingCommit(release),
    hasDownload: Boolean(release) && (release.assets || []).some(
      (asset) => asset.name === NIGHTLY_ASSET && asset.state === 'uploaded',
    ),
  };
}

// True when the nightly build of `sha` was published completely.
function isComplete(state, sha) {
  return state.tag === sha && state.hasDownload && !state.publishing;
}

// The newer commit that makes publishing `sha` a step backwards, or null:
// the finished nightly build, or one another run has started publishing.
async function newerBuild(github, owner, repo, state, sha) {
  for (const other of [state.tag, state.publishing]) {
    if (await isOlderThan(github, owner, repo, sha, other)) return other;
  }
  return null;
}

function publishingBlock(sha, run) {
  return [
    `<!-- nightly-publishing:start ${sha} ${run} -->`,
    `> **A new nightly build (\`${sha.slice(0, 7)}\`) is being published.** Until this note goes away, the download below and the Docker image may come from different builds. If it is still here hours later, that run failed and the next nightly run publishes again.`,
    '<!-- nightly-publishing:end -->',
    '',
    '',
  ].join('\n');
}

// First publishing step: records this run and `sha` in the release before
// anything is pushed, creating the release on the first night. Fails when a newer build
// is published or being published, which a re-run of an earlier run meets.
async function beginPublishing({ github, context, core, sha }) {
  const { owner, repo } = context.repo;
  const state = await nightlyState(github, owner, repo);
  const newer = await newerBuild(github, owner, repo, state, sha);
  if (newer) {
    core.setFailed(`A newer nightly build (${newer}) is published or being published; ${sha} is out of date.`);
    return false;
  }
  const previous = (state.release?.body || '').replace(PUBLISHING_BLOCK, '');
  const body = publishingBlock(sha, context.runId) + previous;
  if (state.release) {
    await github.rest.repos.updateRelease({
      owner, repo, release_id: state.release.id, body, prerelease: true, make_latest: 'false',
    });
  } else {
    // Created without its file, so it is never a draft. A prerelease is
    // never the latest release, which is what the miner's update check reads.
    await github.rest.repos.createRelease({
      owner, repo, tag_name: NIGHTLY_TAG, target_commitish: sha, name: 'Nightly build',
      body, prerelease: true, draft: false, make_latest: 'false',
    });
  }
  core.info(`Publishing ${sha} as the nightly build.`);
  return true;
}

// Run by each publishing job right before it publishes. The plan's answer
// and beginPublishing can both be stale by then: "Re-run failed jobs" on an
// earlier run re-runs neither. Only the run recorded in the release may go on.
async function assertPublishing({ github, context, core, sha }) {
  const { owner, repo } = context.repo;
  const release = await nightlyRelease(github, owner, repo);
  if (!isPublishing(release, sha, context.runId)) {
    const build = publishingBuild(release);
    core.setFailed(
      build
        ? `Run ${build.run} is publishing ${build.sha} as the nightly build; this run is out of date.`
        : `The nightly release doesn't record this run as publishing ${sha}; it is out of date.`,
    );
    return false;
  }
  return true;
}

function releaseNotes({ repoUrl, sha, version, date, releaseTag, image }) {
  const built = `${date.slice(0, 4)}-${date.slice(4, 6)}-${date.slice(6, 8)}`;
  const lines = [
    `An automatic build of the newest code on \`master\`. It passed the automated tests, but nobody has tried it against Twitch. Expect bugs, and use the [latest release](${repoUrl}/releases/latest) unless you want to test a fix early.`,
    '',
    `* Version: \`${version}\``,
    `* Commit: [\`${sha.slice(0, 7)}\`](${repoUrl}/commit/${sha})`,
    `* Built: ${built} (UTC)`,
  ];
  if (releaseTag) {
    lines.push(`* [Changes since ${releaseTag}](${repoUrl}/compare/${releaseTag}...${sha})`);
  }
  lines.push(
    '',
    '### Windows',
    '',
    `Download \`${NIGHTLY_ASSET}\` below. Only the portable build is published nightly.`,
    '',
    '### Docker image',
    '',
    '```bash',
    `docker pull ${image}:nightly`,
    '```',
    '',
  );
  return lines.join('\n');
}

// Last publishing step, after the Docker push and the Windows upload: moves
// the tag, then replaces the notes, which removes the publishing block. A
// failure before that leaves the block in place, so the next run publishes
// again instead of taking the build for finished.
async function finishPublishing({
  github, context, core, sha, version, date, releaseTag, image,
}) {
  const { owner, repo } = context.repo;
  const release = await nightlyRelease(github, owner, repo);
  if (!isPublishing(release, sha, context.runId)) {
    core.setFailed(`The nightly release no longer records this run as publishing ${sha}.`);
    return false;
  }
  if (!(release.assets || []).some((asset) => asset.name === NIGHTLY_ASSET && asset.state === 'uploaded')) {
    core.setFailed(`The nightly release has no ${NIGHTLY_ASSET}; the upload did not finish.`);
    return false;
  }
  try {
    await github.rest.git.updateRef({ owner, repo, ref: `tags/${NIGHTLY_TAG}`, sha, force: true });
  } catch (error) {
    // 422 is how the API answers for a tag that doesn't exist.
    if (error.status !== 404 && error.status !== 422) throw error;
    await github.rest.git.createRef({ owner, repo, ref: `refs/tags/${NIGHTLY_TAG}`, sha });
  }
  const repoUrl = `${context.serverUrl}/${owner}/${repo}`;
  await github.rest.repos.updateRelease({
    owner,
    repo,
    release_id: release.id,
    name: `Nightly build ${version}`,
    body: releaseNotes({ repoUrl, sha, version, date, releaseTag, image }),
    prerelease: true,
    make_latest: 'false',
  });
  core.info(`Published ${version} (${sha}) as the nightly build.`);
  return true;
}

module.exports = {
  NIGHTLY_ASSET,
  assertPublishing,
  beginPublishing,
  finishPublishing,
  isComplete,
  newerBuild,
  nightlyState,
  publishingCommit,
  releaseNotes,
  tagCommit,
};
