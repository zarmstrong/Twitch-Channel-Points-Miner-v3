'use strict';

const fs = require('node:fs');

// Release Please can create a release and then fail, for example while
// commenting on or relabelling the release PR. Its next attempt sees no
// pending release PR and reports no release, so the Docker, Windows and
// issue jobs would never run for it. After a failed or repeated attempt,
// this looks for a published release of the manifest version on the commit
// being built, and hands that release to the publishing jobs instead.

// SemVer 2.0.0: numeric prerelease identifiers have no leading zeros.
const PRERELEASE_ID = '(?:0|[1-9]\\d*|\\d*[A-Za-z-][0-9A-Za-z-]*)';
const RELEASE_TAG = new RegExp(
  '^(0|[1-9]\\d*)\\.(0|[1-9]\\d*)\\.(0|[1-9]\\d*)'
  + `(?:-${PRERELEASE_ID}(?:\\.${PRERELEASE_ID})*)?`
  + '(?:\\+[0-9A-Za-z-]+(?:\\.[0-9A-Za-z-]+)*)?$',
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
async function notFoundAsNull(request) {
  try {
    return (await request()).data;
  } catch (error) {
    if (error.status === 404 || error.status === 422) return null;
    throw error;
  }
}

async function findEarlierRelease({ github, context, core, tag }) {
  const { owner, repo } = context.repo;
  const release = await notFoundAsNull(() => github.rest.repos.getReleaseByTag({ owner, repo, tag }));
  if (!release || release.draft) {
    core.info(`No published release ${tag}; nothing to publish.`);
    return false;
  }
  const commit = await notFoundAsNull(() => github.rest.repos.getCommit({
    owner, repo, ref: `refs/tags/${tag}`,
  }));
  if (!commit || commit.sha !== context.sha) {
    core.info(`Release ${tag} is on another commit than ${context.sha}; nothing to publish.`);
    return false;
  }
  core.notice(`Release ${tag} was created by an earlier Release Please attempt; publishing it now.`);
  return true;
}

// `recheck` is true when Release Please failed in any attempt of this job, or
// when the run itself is a re-run.
async function resolveRelease({
  github, context, core, releaseCreated, tagName, recheck, manifestText,
}) {
  let tag = '';
  if (releaseCreated === 'true' && tagName) {
    tag = tagName;
    core.info(`Release Please created ${tag}.`);
  } else if (!recheck) {
    core.info('Release Please created no release.');
  } else {
    const candidate = manifestTag(manifestText);
    if (!candidate) {
      core.warning('The release manifest has no usable version; nothing to publish.');
    } else if (await findEarlierRelease({ github, context, core, tag: candidate })) {
      tag = candidate;
    }
  }
  core.setOutput('release_ready', tag ? 'true' : 'false');
  core.setOutput('tag_name', tag);
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
  readManifest,
  resolveRelease,
};
