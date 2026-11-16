# Security policy

## Supported versions

Only the [latest release](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/releases/latest) gets security fixes. Please check that a problem still happens on it before reporting.

## Reporting a vulnerability

Please don't open a public issue, pull request or discussion for a security problem.

Report it privately instead: on the [Security tab](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/security), choose **Report a vulnerability**, or go straight to the [new advisory form](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/security/advisories/new). Only the maintainers can see the report.

Include:

- the version, and how you run it (Python, Docker or the Windows app)
- what an attacker can do, and what access they need first
- steps to reproduce, or a proof of concept

Never send real credentials. Replace Twitch passwords, auth tokens, cookie files (`cookies/*.pkl`), the analytics password and notification webhook URLs or tokens with placeholders, in logs and config excerpts too.

This project is maintained by volunteers, so replies are best effort. We'll confirm the report, keep you updated while we work on a fix, and credit you in the advisory unless you'd rather not be named.

## Scope

In scope: this repository's code, including the analytics dashboard, the Windows app and installer, the Docker image, and the release workflows.

Out of scope:

- Twitch's own services and APIs. Report those to Twitch.
- Problems that need someone who can already edit your `config.py` or read your `cookies/` folder. Those files hold your credentials by design.
- Running the analytics dashboard on a public network without HTTPS. Its HTTP Basic authentication isn't encrypted; see [Analytics security](docs/analytics.md#analytics-security-and-https-reverse-proxy) for the supported setup.
