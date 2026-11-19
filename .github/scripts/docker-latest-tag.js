'use strict';

// Docker's `latest` tag and the Docker Hub description follow the newest
// stable release. A prerelease, or an older release published again by hand,
// must not move them. Only plain MAJOR.MINOR.PATCH tags count as stable.
const STABLE_TAG = /^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$/;

function stableVersion(tag) {
  const match = STABLE_TAG.exec(tag || '');
  return match ? match.slice(1).map(BigInt) : null;
}

function compareVersions(a, b) {
  for (let i = 0; i < 3; i += 1) {
    if (a[i] !== b[i]) return a[i] < b[i] ? -1 : 1;
  }
  return 0;
}

// The newest stable tag among the names, or null when there is none.
function newestStableTag(names) {
  let newest = null;
  let newestVersion = null;
  for (const name of names) {
    const version = stableVersion(name);
    if (version && (!newestVersion || compareVersions(version, newestVersion) > 0)) {
      newest = name;
      newestVersion = version;
    }
  }
  return newest;
}

// A tag that isn't in the repository's tag list never moves `latest`.
function isNewestStableTag(tag, names) {
  return stableVersion(tag) !== null && names.includes(tag) && newestStableTag(names) === tag;
}

async function decideLatestTag({ github, context, core, tag }) {
  const { owner, repo } = context.repo;
  const names = await github.paginate(
    github.rest.repos.listTags,
    { owner, repo, per_page: 100 },
    (response) => response.data.map((entry) => entry.name),
  );
  const move = isNewestStableTag(tag, names);
  const newest = newestStableTag(names);
  if (move) {
    core.info(`${tag} is the newest stable release; latest will point to it.`);
  } else if (stableVersion(tag) === null) {
    core.info(`${tag} is not a plain MAJOR.MINOR.PATCH release; latest stays where it is.`);
  } else if (!names.includes(tag)) {
    core.info(`${tag} is not a tag in this repository; latest stays where it is.`);
  } else {
    core.info(`The newest stable release is ${newest}, not ${tag}; latest stays where it is.`);
  }
  core.setOutput('move_latest', move ? 'true' : 'false');
  return move;
}

module.exports = {
  decideLatestTag,
  isNewestStableTag,
  newestStableTag,
};
