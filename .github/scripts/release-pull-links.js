'use strict';

// Release Please links every #N as /issues/N. GitHub redirects those to
// /pull/N on a full page load, but its in-page navigation shows "Unable to
// load page" for a PR, so PR references are rewritten to /pull/N.
const PENDING_LABEL = 'autorelease: pending';

function escapeRegExp(text) {
  return text.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

function issueLinkPattern(owner, repo) {
  return new RegExp(
    `(https://github\\.com/${escapeRegExp(owner)}/${escapeRegExp(repo)}/)issues/(\\d+)\\b`,
    'gi',
  );
}

function linkedNumbers(body, owner, repo) {
  const numbers = new Set();
  for (const match of (body || '').matchAll(issueLinkPattern(owner, repo))) {
    numbers.add(Number(match[2]));
  }
  return [...numbers];
}

function rewritePullLinks(body, owner, repo, pullNumbers) {
  return body.replace(issueLinkPattern(owner, repo), (link, prefix, number) => (
    pullNumbers.has(Number(number)) ? `${prefix}pull/${number}` : link
  ));
}

// Uses the pulls API so the lookup needs only the pull-requests permission
// Release Please already has; a 404 there means the number is an issue.
async function isPullRequest(github, owner, repo, number, cache) {
  if (!cache.has(number)) {
    try {
      await github.rest.pulls.get({ owner, repo, pull_number: number });
      cache.set(number, true);
    } catch (error) {
      if (error.status !== 404) throw error;
      cache.set(number, false);
    }
  }
  return cache.get(number);
}

async function pullNumbersIn(github, owner, repo, body, cache) {
  const pulls = new Set();
  for (const number of linkedNumbers(body, owner, repo)) {
    if (await isPullRequest(github, owner, repo, number, cache)) pulls.add(number);
  }
  return pulls;
}

// Looks the listed body's numbers up first, then rewrites a freshly read body
// through the same cache, so only numbers added in between are looked up
// before writing and a concurrent Release Please run's entries are kept.
// Returns null when nothing needs writing.
async function relinkFresh(github, owner, repo, listed, readFresh, cache) {
  await pullNumbersIn(github, owner, repo, listed, cache);
  const fresh = await readFresh();
  if (!fresh) return null;
  const pulls = await pullNumbersIn(github, owner, repo, fresh, cache);
  const body = rewritePullLinks(fresh, owner, repo, pulls);
  return body === fresh ? null : body;
}

async function linkReleasePullRequests({ github, context, core, tag }) {
  const { owner, repo } = context.repo;
  const cache = new Map();

  // A published release is permanent, so it goes before the release PR,
  // whose body the next run relinks again if this one fails.
  if (tag) {
    const { data: listed } = await github.rest.repos.getReleaseByTag({ owner, repo, tag });
    const body = await relinkFresh(github, owner, repo, listed.body, async () => (
      (await github.rest.repos.getRelease({ owner, repo, release_id: listed.id })).data.body
    ), cache);
    if (body) {
      await github.rest.repos.updateRelease({ owner, repo, release_id: listed.id, body });
      core.info(`Linked PR references in release ${tag}`);
    }
  }

  const openPulls = await github.paginate(github.rest.pulls.list, {
    owner, repo, state: 'open', per_page: 100,
  });
  const releasePulls = openPulls.filter((pull) => (
    pull.labels.some((label) => label.name === PENDING_LABEL)
  ));
  for (const { number, body: listed } of releasePulls) {
    const body = await relinkFresh(github, owner, repo, listed, async () => (
      (await github.rest.pulls.get({ owner, repo, pull_number: number })).data.body
    ), cache);
    if (body) {
      await github.rest.pulls.update({ owner, repo, pull_number: number, body });
      core.info(`Linked PR references in release PR #${number}`);
    }
  }
}

module.exports = {
  linkReleasePullRequests,
  linkedNumbers,
  rewritePullLinks,
};
