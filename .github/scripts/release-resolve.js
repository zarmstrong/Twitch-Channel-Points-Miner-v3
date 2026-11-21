'use strict';

const fs = require('node:fs');

// Release Please can create a release and then fail, for example while
// commenting on or relabelling the release PR. Its next attempt sees no
// pending release PR and reports no release, so the Docker, Windows and
// issue jobs would never run for it. After a failed or repeated attempt,
// this looks for a published release of the manifest version on the commit
// being built, and hands that release to the publishing jobs instead.

// SemVer 2.0.0: numeric prerelease identifiers have no leading zeros. Build
// metadata (`+...`) isn't accepted: Docker tags drop it, so two such
// versions would share one image tag and the second would reuse the first.
const PRERELEASE_ID = '(?:0|[1-9]\\d*|\\d*[A-Za-z-][0-9A-Za-z-]*)';
const RELEASE_TAG = new RegExp(
  '^(0|[1-9]\\d*)\\.(0|[1-9]\\d*)\\.(0|[1-9]\\d*)'
  + `(?:-${PRERELEASE_ID}(?:\\.${PRERELEASE_ID})*)?$`,
);

// This repository's tags are the bare manifest version (no `v`, no component).
function manifestTag(text) {
  let manifest;
  try {
    manifest = JSON.parse(text);
  } catch {
    return null;
  }
  const version = manifest && typeof manifest === 'object' ? manifest['.'] : null;
  return typeof version === 'string' && RELEASE_TAG.test(version) ? version : null;
}

// A missing release or tag reads as "nothing to publish"; anything else fails.
// getCommit answers 422 for a ref that doesn't exist, so it passes [404, 422].
async function notFoundAsNull(request, notFound = [404]) {
  try {
    return (await request()).data;
  } catch (error) {
    if (notFound.includes(error.status)) return null;
    throw error;
  }
}

// The Docker workflow writes this block only after every Docker step
// succeeded, and the Windows job uploads these files, so a release with all
// of them was published to the end. GitHub can leave an empty asset behind
// from a failed upload (state "starter"), so only a finished upload counts.
// Before this script, the block was written before `latest` and the Docker
// Hub description. Only the manifest's release is checked here, and the one
// current when that changed (3.21.1) finished all of its Docker steps, so
// the block means the same for every release this script sees.
const DOCKER_NOTES_MARKER = '<!-- docker-image:start -->';

function dockerPublished(release) {
  return String(release.body || '').includes(DOCKER_NOTES_MARKER);
}

function windowsPublished(release, tag) {
  const assets = new Set((release.assets || [])
    .filter((asset) => asset.state === 'uploaded' && asset.size > 0)
    .map((asset) => asset.name));
  return assets.has(`TwitchChannelPointsMiner-Portable-${tag}.zip`)
    && assets.has(`TwitchChannelPointsMiner-${tag}-Setup.exe`);
}

function publishingFinished(release, tag) {
  return dockerPublished(release) && windowsPublished(release, tag);
}

// Only the missing parts are published again: a Docker image that already
// published (its notes block is written last, after latest moved) isn't
// rebuilt over the same version tag when only the Windows files are missing.
function missingParts(release, tag) {
  return { docker: !dockerPublished(release), windows: !windowsPublished(release, tag) };
}

function describeParts(parts) {
  const names = [];
  if (parts.docker) names.push('the Docker image');
  if (parts.windows) names.push('the Windows files');
  return names.join(' and ');
}

// Release Please reads master's state, not the commit being built, so a run
// for an earlier commit can create the release of a release PR merged while
// it ran. If that run then fails, the release commit's own run sees no new
// release from Release Please; it publishes the release here instead.
// Returns the parts to publish, or null when there is nothing to do.
async function findUnpublishedRelease({
  github, context, core, tag, recheck, rerun,
}) {
  const { owner, repo } = context.repo;
  const release = await notFoundAsNull(() => github.rest.repos.getReleaseByTag({ owner, repo, tag }));
  if (!release || release.draft) {
    core.info(`No published release ${tag}; nothing to publish.`);
    return null;
  }
  const commit = await notFoundAsNull(() => github.rest.repos.getCommit({
    owner, repo, ref: `refs/tags/${tag}`,
  }), [404, 422]);
  if (!commit) {
    // A published release always has its tag, so this needs a person to look.
    core.warning(`Release ${tag} exists but its tag is missing; Docker and Windows files are not published.`);
    return null;
  }
  const finished = publishingFinished(release, tag);
  if ((commit.sha === context.sha && recheck) || rerun) {
    // A failed attempt of the release commit's run, or any re-run (which
    // may be of a later commit's run that recovered this release): publish
    // what's missing and close the released issues again.
    const missing = missingParts(release, tag);
    const parts = describeParts(missing);
    core.notice(parts
      ? `Release ${tag} is being published again; publishing ${parts}.`
      : `Release ${tag} is being published again; nothing is missing, so only its issues are closed again.`);
    return missing;
  }
  // The release commit's own run can be replaced while it waits (one
  // pending run per concurrency group), so the run that created the release
  // may be an earlier commit's run that then failed. Any later run publishes
  // it while it is unfinished. A part whose publishing keeps failing is
  // therefore tried again on each push until it succeeds, or until the next
  // release replaces this one in the manifest.
  if (!finished) {
    // A warning, so a part that keeps failing (for Windows, a full build on
    // every push) shows on each run's summary.
    const missing = missingParts(release, tag);
    core.warning(`Release ${tag} on ${commit.sha} is missing ${describeParts(missing)}; publishing again. This repeats on every push until it succeeds.`);
    return missing;
  }
  // Expected on every push after a release: the manifest still names the
  // latest release, which its own commit's run published.
  core.info(`Release ${tag} is already published; nothing to publish.`);
  return null;
}

// `recheck` is true when Release Please failed in any attempt of this job, or
// when the run itself is a re-run; `rerun` only for a re-run.
async function resolveRelease({
  github, context, core, releaseCreated, tagName, recheck, rerun = false, manifestText,
}) {
  let tag = '';
  let parts = { docker: false, windows: false };
  if (releaseCreated === 'true' && tagName) {
    // The tag feeds checkout refs, shell environments and gh release upload.
    if (!RELEASE_TAG.test(tagName)) {
      throw new Error(`Release Please reported tag ${JSON.stringify(tagName)}, which is not a bare SemVer version.`);
    }
    tag = tagName;
    parts = { docker: true, windows: true };
    core.info(`Release Please created ${tag}.`);
  } else {
    if (!recheck) core.info('Release Please created no release.');
    const candidate = manifestTag(manifestText);
    if (!candidate) {
      core.warning('The release manifest has no usable version; nothing to publish.');
    } else {
      const missing = await findUnpublishedRelease({
        github, context, core, tag: candidate, recheck, rerun,
      });
      if (missing) {
        tag = candidate;
        parts = missing;
      }
    }
  }
  core.setOutput('release_ready', tag ? 'true' : 'false');
  core.setOutput('tag_name', tag);
  core.setOutput('publish_docker', parts.docker ? 'true' : 'false');
  core.setOutput('publish_windows', parts.windows ? 'true' : 'false');
  return tag;
}

function readManifest(path = '.release-please-manifest.json') {
  try {
    return fs.readFileSync(path, 'utf8');
  } catch {
    return '';
  }
}

module.exports = {
  manifestTag,
  publishingFinished,
  readManifest,
  resolveRelease,
};
