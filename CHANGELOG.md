# Changelog

## [3.16.0](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/compare/3.15.2...3.16.0) (2026-08-27)


### Features

* **chat:** prefer TLS for IRC chat, falling back to plaintext ([a58ded5](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/commit/a58ded509f5dd6f722494cb5d6e23626eedffeb3))


### Bug Fixes

* **analytics:** dedupe drops-by-category dashboard rows on drop_id alone ([eda1604](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/commit/eda1604b211ff246a1ff3ba678a2fb4d3803b625))
* **betting:** use combined outcome totals on 3+ option predictions ([a58ded5](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/commit/a58ded509f5dd6f722494cb5d6e23626eedffeb3))
* **chat:** stop spamming errors when IRC chat thread is stopped ([#111](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/issues/111)) ([a7246fa](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/commit/a7246fa55746bf2886b3ab74b13d0c649b17dce9))
* **drops:** avoid misleading "Online for &lt;game&gt; drops" status messages ([eda1604](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/commit/eda1604b211ff246a1ff3ba678a2fb4d3803b625))
* **drops:** drop apostrophes in __slugify instead of hyphenating them ([eda1604](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/commit/eda1604b211ff246a1ff3ba678a2fb4d3803b625))
* **drops:** prune stale games/campaigns from the badge catalog ([a58ded5](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/commit/a58ded509f5dd6f722494cb5d6e23626eedffeb3))
* **drops:** refresh category eligibility for all online streamers, not just watched ones ([#105](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/issues/105)) ([eda1604](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/commit/eda1604b211ff246a1ff3ba678a2fb4d3803b625))
* **gui:** correct word order for game names with leading digits in Drops tab ([#108](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/issues/108)) ([fab4560](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/commit/fab456018f0c346ec532f0864426ea459ab219bf))
* **logger:** fix dead timezone-none check for per-run log filenames ([a58ded5](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/commit/a58ded509f5dd6f722494cb5d6e23626eedffeb3))
* **login:** stop device-code polling from hanging forever ([#107](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/issues/107)) ([a58ded5](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/commit/a58ded509f5dd6f722494cb5d6e23626eedffeb3))
* **shutdown:** bound minute-watcher/sync-campaigns join on Ctrl+C ([a58ded5](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/commit/a58ded509f5dd6f722494cb5d6e23626eedffeb3))
* **startup:** retry once when initial streamer bootstrap hits a GQL RetryError ([#110](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/issues/110)) ([fd4cb7a](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/commit/fd4cb7af5f4523fe67623f2f92380701dbea67df))
* **twitch:** harden minute-watcher and eligibility cache against crashes and races ([a58ded5](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/commit/a58ded509f5dd6f722494cb5d6e23626eedffeb3))
* **watch:** retry once on connection error sending minute watched ([#109](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/issues/109)) ([f255270](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/commit/f255270339af40f65ab4760ea6fe824165c88f08))
* **websocket:** make reconnection check-and-set atomic ([a58ded5](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/commit/a58ded509f5dd6f722494cb5d6e23626eedffeb3))

## [3.15.2](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/compare/3.15.1...3.15.2) (2026-08-15)


### Bug Fixes

* **drops:** always pick the soonest-expiring category campaign ([#103](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/issues/103)) ([07fb863](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/commit/07fb863bd95ba79301654ebf57388f5e07f7e731))

## [3.15.1](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/compare/3.15.0...3.15.1) (2026-08-14)


### Bug Fixes

* **categories:** order campaigns by deadline ([#99](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/issues/99)) ([7320d7c](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/commit/7320d7ca1f30e158a63b89b7c9d7837b9a65ab72))
* **drops:** preserve fallback category eligibility ([7320d7c](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/commit/7320d7ca1f30e158a63b89b7c9d7837b9a65ab72))
* **gql:** throttle channel points startup requests ([#101](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/issues/101)) ([3e3180f](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/commit/3e3180fc29b7f6c0308dc33b8599a1a309ecdb33))

## [3.15.0](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/compare/3.14.2...3.15.0) (2026-08-14)


### Features

* **config:** use config.py as the canonical dashboard configuration ([7d2f137](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/commit/7d2f137389ea2359869bce19428d097de0d37d4e))


### Bug Fixes

* **categories:** preserve points-only forced streams ([bcd7345](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/commit/bcd7345f6f212c8792ed487fb2a6bcb6816e5823))
* **config:** preserve imports for dashboard-generated expressions ([7d2f137](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/commit/7d2f137389ea2359869bce19428d097de0d37d4e))
* **drops:** enforce campaign eligibility, priority, and stall rotation ([#96](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/issues/96)) ([bcd7345](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/commit/bcd7345f6f212c8792ed487fb2a6bcb6816e5823))
* **drops:** include game names in deadline logs ([bcd7345](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/commit/bcd7345f6f212c8792ed487fb2a6bcb6816e5823))
* **drops:** match reused rewards to campaign windows ([bcd7345](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/commit/bcd7345f6f212c8792ed487fb2a6bcb6816e5823))
* **watching:** prioritize active drops across streamer sources ([bcd7345](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/commit/bcd7345f6f212c8792ed487fb2a6bcb6816e5823))


### Performance Improvements

* **config:** migrate legacy streamer settings in one rewrite ([7d2f137](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/commit/7d2f137389ea2359869bce19428d097de0d37d4e))
* **watching:** precompute streamer source groups ([bcd7345](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/commit/bcd7345f6f212c8792ed487fb2a6bcb6816e5823))


### Documentation

* **drops:** clarify campaign inventory merging ([bcd7345](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/commit/bcd7345f6f212c8792ed487fb2a6bcb6816e5823))

## [3.14.2](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/compare/3.14.1...3.14.2) (2026-08-12)


### Bug Fixes

* **categories:** apply refreshed streamer priority ([#93](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/issues/93)) ([2552623](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/commit/255262369b753c9a491c4695bf79cc0ca496fb74))
* **drops:** trust completed inventory campaigns ([2552623](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/commit/255262369b753c9a491c4695bf79cc0ca496fb74))

## [3.14.1](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/compare/3.14.0...3.14.1) (2026-08-12)

Category Drops mining now prioritizes active, incomplete campaigns confirmed by
the authenticated Twitch inventory over campaigns found only in the external
fallback index. Fallback campaigns remain available for games Twitch has not
exposed to the account, without displacing verified campaign progress.


### Bug Fixes

* **categories:** recognize localized Drops tags ([#89](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/issues/89)) ([1bcb560](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/commit/1bcb5606ed59fc305932e757b71467ef759c63b6))
* **categories:** retire stale discovered streamers ([#91](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/issues/91)) ([5933e34](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/commit/5933e348563f01ac58fbe6b18860df799698d816))
* **categories:** stop category pagination at the Drops directory result count ([1bcb560](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/commit/1bcb5606ed59fc305932e757b71467ef759c63b6))
* **categories:** trust restricted campaign channel allowlists ([1bcb560](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/commit/1bcb5606ed59fc305932e757b71467ef759c63b6))
* **drops:** prioritize inventory campaigns ([#92](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/issues/92)) ([f48bf54](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/commit/f48bf544a3741c248533d71692d8b8b247e3a009))
* **logging:** distinguish eligible streams from the configured selection limit ([1bcb560](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/commit/1bcb5606ed59fc305932e757b71467ef759c63b6))

## [3.14.0](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/compare/3.13.5...3.14.0) (2026-08-10)


### Features

* **logging:** add configurable daily log rotation ([#86](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/issues/86)) ([5bf9143](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/commit/5bf914321665aba3c9d0382bbec2c62c2087738c))


### Bug Fixes

* **drops:** recognize awarded fallback rewards ([#87](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/issues/87)) ([5e17feb](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/commit/5e17febb30f267d27bc4ed0571687beb438b0ee7))

## [3.13.5](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/compare/3.13.4...3.13.5) (2026-08-10)


### Bug Fixes

* **drops:** prevent completed fallback resurrection ([#84](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/issues/84)) ([7be8e29](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/commit/7be8e29879ced6ff14dc742bbd341a46b40bbaca))

## [3.13.4](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/compare/3.13.3...3.13.4) (2026-08-10)


### Bug Fixes

* **drops:** retire completed campaign monitors ([#82](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/issues/82)) ([4956ec5](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/commit/4956ec58742996418d43fe8b5efe54ae4f7c4d50))
* **pubsub:** reuse websocket capacity across streamer churn ([80cdaf5](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/commit/80cdaf5beb32ab768676e107d598063b107c8abc))

## [3.13.3](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/compare/3.13.2...3.13.3) (2026-08-09)


### Bug Fixes

* **drops:** prevent stale fallback categories ([#79](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/issues/79)) ([5f3f3b7](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/commit/5f3f3b70c168cc7f6ef95623b869da596c269af6))

## [3.13.2](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/compare/3.13.1...3.13.2) (2026-08-09)


### Bug Fixes

* **drops:** correct fallback campaign eligibility ([#77](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/pull/77)) ([7d945cc](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/commit/7d945cccf0a570ab02360924b32d0173613bdf07))

## [3.13.1](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/compare/3.13.0...3.13.1) (2026-08-08)


### Bug Fixes

* **windows:** restore Inno Setup compiler compatibility ([1918831](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/commit/1918831075912d57912fc7de3e289ddb363e8fe4))

## [3.13.0](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/compare/3.12.0...3.13.0) (2026-08-07)


### Features

* **installer:** prefill initial configuration options and link documentation ([1f6cee1](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/commit/1f6cee1c813e6f31f586003243d44b673e0c0e5e))
* **installer:** publish a per-user Windows installer ([1f6cee1](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/commit/1f6cee1c813e6f31f586003243d44b673e0c0e5e))
* **windows:** add installer and guided first-run setup ([#73](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/issues/73)) ([1f6cee1](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/commit/1f6cee1c813e6f31f586003243d44b673e0c0e5e))


### Bug Fixes

* **windows:** pause after creating the first-run configuration ([1f6cee1](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/commit/1f6cee1c813e6f31f586003243d44b673e0c0e5e))

## [3.12.0](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/compare/3.11.1...3.12.0) (2026-08-06)


### Features

* add message_thread_id support for Telegram notifications ([#70](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/issues/70)) ([0e3a1a0](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/commit/0e3a1a03a4fb706b84ebce21c04f6bfd50e30e7b))
* add Windows installer release artifact ([#71](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/issues/71)) ([b9de9ed](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/commit/b9de9edab6ae037e2365376cdb47a6115222f0a4))


### Bug Fixes

* **config:** restrict nullable notification field ([0e3a1a0](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/commit/0e3a1a03a4fb706b84ebce21c04f6bfd50e30e7b))
* **web:** preserve cleared Telegram topic IDs ([0e3a1a0](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/commit/0e3a1a03a4fb706b84ebce21c04f6bfd50e30e7b))
* **web:** support Telegram topic IDs in dashboard ([0e3a1a0](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/commit/0e3a1a03a4fb706b84ebce21c04f6bfd50e30e7b))

## [3.11.1](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/compare/3.11.0...3.11.1) (2026-07-31)


### Bug Fixes

* **drops:** advance after earning badge drops ([#68](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/issues/68)) ([039e77a](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/commit/039e77adc93c82f5d4719d756ee3f83bccbbb0cd))
* **drops:** preserve badge inventory baseline after transient refresh failures ([039e77a](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/commit/039e77adc93c82f5d4719d756ee3f83bccbbb0cd))

## [3.11.0](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/compare/3.10.1...3.11.0) (2026-07-27)


### Features

* add ntfy notification support ([#66](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/issues/66)) ([c97301e](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/commit/c97301ee96e8839af70d4565a0921d13fca26b06))


### Bug Fixes

* **drops:** recognize campaign-qualified badge rewards ([#64](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/issues/64)) ([21bf277](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/commit/21bf277d61b5c5dff778ac94ee5c74d22fd3e0af))
* follow raids only from watched streams ([#67](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/issues/67)) ([298c830](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/commit/298c830db77f400e3bc05991c20b14a478dbb0b0))
* **logging:** honor boolean notification skip flags consistently ([c97301e](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/commit/c97301ee96e8839af70d4565a0921d13fca26b06))

## [3.10.1](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/compare/3.10.0...3.10.1) (2026-07-26)


### Bug Fixes

* **analytics:** correct points chart month labels ([#62](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/issues/62)) ([9248543](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/commit/9248543613566b354d20361975b99d90b5062ef5))

## [3.10.0](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/compare/3.9.1...3.10.0) (2026-07-25)


### Features

* **reports:** improve Drop progress details ([#60](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/issues/60)) ([d076e03](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/commit/d076e03f207ee0536b08e84159e14ad9cd768f6a))


### Bug Fixes

* **reports:** ignore zero-watch captured drops ([d076e03](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/commit/d076e03f207ee0536b08e84159e14ad9cd768f6a))
* **web:** handle EBUSY for bind-mounted config files ([3b3ee6c](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/commit/3b3ee6cc6e13712c877bb5ec1a44c280edbc9948))
* **web:** persist category edits to config.py ([#59](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/issues/59)) ([3b3ee6c](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/commit/3b3ee6cc6e13712c877bb5ec1a44c280edbc9948))

## [3.9.1](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/compare/3.9.0...3.9.1) (2026-07-24)


### Bug Fixes

* **release:** publish artifacts for every release ([#57](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/issues/57)) ([8d109f3](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/commit/8d109f34e9c2a4a4f02b7e1dfbd6759f403a4326)), closes [#54](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/issues/54)

## [3.9.0](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/compare/3.8.1...3.9.0) (2026-07-24)


### Features

* close linked issues after release ([#55](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/issues/55)) ([6681260](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/commit/668126060a8b6038ba1e293dcab1e04a604bb94e)), closes [#51](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/issues/51)

## [3.8.1](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/compare/3.8.0...3.8.1) (2026-07-24)


### Bug Fixes

* **drops:** handle zero required watch time ([#51](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/issues/51)) ([9648a31](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/commit/9648a31d7b45e973e048dc2a8a22e3f5fda3c4ee))


### Documentation

* **docker:** add Compose and Docker CLI setup instructions ([af87cb0](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/commit/af87cb0935cc34da4b4c77a1e3eeec4854b438b5))
* expand notifications and usage guide ([#53](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/issues/53)) ([af87cb0](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/commit/af87cb0935cc34da4b4c77a1e3eeec4854b438b5))
* **notifications:** document all services and event types ([af87cb0](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/commit/af87cb0935cc34da4b4c77a1e3eeec4854b438b5))
* **usage:** consolidate Docker, Windows, and source quick starts ([af87cb0](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/commit/af87cb0935cc34da4b4c77a1e3eeec4854b438b5))

## [3.8.0](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/compare/3.7.5...3.8.0) (2026-07-24)


### Features

* **config:** add masterslate as the default support streamer ([79ec46c](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/commit/79ec46c86118126c0ccaa5fa5fd69cf82b4c3845))
* **notifications:** add event selectors and test-notification controls ([79ec46c](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/commit/79ec46c86118126c0ccaa5fa5fd69cf82b4c3845))
* notify when a newer release is available ([#43](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/issues/43)) ([9cc00b1](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/commit/9cc00b12cffb46f5d2d203636f5de0c917b3d26c))
* **web:** add config editor ([#40](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/issues/40)) ([79ec46c](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/commit/79ec46c86118126c0ccaa5fa5fd69cf82b4c3845))
* **web:** configure release update checks ([9cc00b1](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/commit/9cc00b12cffb46f5d2d203636f5de0c917b3d26c))
* **web:** show dismissible update banner and version footer ([9cc00b1](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/commit/9cc00b12cffb46f5d2d203636f5de0c917b3d26c))


### Bug Fixes

* **config:** migrate directly bind-mounted files ([#48](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/issues/48)) ([f41bb47](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/commit/f41bb47f5d6c2eb4818d9d9039eeb996abebf74e))
* **config:** validate managed overrides and preserve notification settings ([79ec46c](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/commit/79ec46c86118126c0ccaa5fa5fd69cf82b4c3845))
* **docs:** replace broken last commit badge ([#46](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/issues/46)) ([78d3e43](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/commit/78d3e433a1270e7ae51726eaa829daee39302f0b))
* **miner:** support live streamer removal and PubSub cleanup ([79ec46c](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/commit/79ec46c86118126c0ccaa5fa5fd69cf82b4c3845))
* **notifications:** report detailed delivery failures and improve form layout ([79ec46c](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/commit/79ec46c86118126c0ccaa5fa5fd69cf82b4c3845))
* **tests:** pin running version in update checks ([#49](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/issues/49)) ([a08cc83](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/commit/a08cc83e1b020817e25be42a2a79d5ccad323f4b))
* **update:** handle startup-only intervals and send GitHub API headers ([9cc00b1](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/commit/9cc00b12cffb46f5d2d203636f5de0c917b3d26c))
* **update:** validate release metadata and use the running package version ([9cc00b1](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/commit/9cc00b12cffb46f5d2d203636f5de0c917b3d26c))
* **web:** improve dark-mode contrast and auto-dismiss status messages ([79ec46c](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/commit/79ec46c86118126c0ccaa5fa5fd69cf82b4c3845))
* **web:** recognize math.inf as a startup-only interval ([9cc00b1](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/commit/9cc00b12cffb46f5d2d203636f5de0c917b3d26c))
* **websocket:** synchronize topic updates and prevent duplicate subscriptions ([79ec46c](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/commit/79ec46c86118126c0ccaa5fa5fd69cf82b4c3845))

## [3.7.5](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/compare/3.7.4...3.7.5) (2026-07-23)


### Bug Fixes

* **docs:** replace broken GitHub badges ([#44](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/issues/44)) ([52c0de5](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/commit/52c0de5a62a3383904b97917017aa09471607125))

## [3.7.4](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/compare/3.7.3...3.7.4) (2026-07-23)


### Bug Fixes

* **drops:** include game and campaign in daily progress entries ([bcad721](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/commit/bcad72188e5a2251e8020534efcb75d0d1ee8608))
* improve daily report activity details ([#41](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/issues/41)) ([bcad721](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/commit/bcad72188e5a2251e8020534efcb75d0d1ee8608))

## [3.7.3](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/compare/3.7.2...3.7.3) (2026-07-23)


### Documentation

* fix badges and document release notes ([#38](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/issues/38)) ([46d2f7a](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/commit/46d2f7ae369867c81be11cb8223f5c40f5585374))
* **release:** document published release-note formatting ([46d2f7a](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/commit/46d2f7ae369867c81be11cb8223f5c40f5585374))

## [3.7.2](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/compare/3.7.1...3.7.2) (2026-07-23)


### Bug Fixes

* **auth:** remove unused Twitch passwords ([dd8feb6](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/commit/dd8feb65d4f4311e32470c56cb449b60f291d86e))
* **config:** sanitize interrupted migration backups ([dd8feb6](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/commit/dd8feb65d4f4311e32470c56cb449b60f291d86e))
* **docker:** seed config on fresh installs ([dd8feb6](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/commit/dd8feb65d4f4311e32470c56cb449b60f291d86e))
* **docker:** seed config on fresh installs ([#36](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/issues/36)) ([dd8feb6](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/commit/dd8feb65d4f4311e32470c56cb449b60f291d86e))


### Documentation

* **config:** refine example logging defaults ([dd8feb6](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/commit/dd8feb65d4f4311e32470c56cb449b60f291d86e))
* **docker:** clarify restart policy during first config seed ([dd8feb6](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/commit/dd8feb65d4f4311e32470c56cb449b60f291d86e))

## [3.7.1](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/compare/3.7.0...3.7.1) (2026-07-22)


### Bug Fixes

* format Discord alerts as code ([1840c7e](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/commit/1840c7ed77ca6cf870a8c5cb17d3c42fedd6f55e))
* format Discord alerts as code ([#34](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/issues/34)) ([1840c7e](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/commit/1840c7ed77ca6cf870a8c5cb17d3c42fedd6f55e))

## [3.7.0](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/compare/3.6.0...3.7.0) (2026-07-22)


### Features

* add SMTP alerts and persistent daily reports ([f3cdcbb](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/commit/f3cdcbb0110936a641aee49188874b92182aa6d6))
* add SMTP alerts and persistent daily reports ([#32](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/issues/32)) ([f3cdcbb](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/commit/f3cdcbb0110936a641aee49188874b92182aa6d6))
* **reports:** persist daily report baselines across restarts ([f3cdcbb](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/commit/f3cdcbb0110936a641aee49188874b92182aa6d6))


### Bug Fixes

* **config:** migrate existing configurations to SMTP and daily report settings ([f3cdcbb](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/commit/f3cdcbb0110936a641aee49188874b92182aa6d6))


### Documentation

* document email alerts and daily report configuration ([f3cdcbb](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/commit/f3cdcbb0110936a641aee49188874b92182aa6d6))

## [3.6.0](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/compare/3.5.0...3.6.0) (2026-07-20)


### Features

* **release:** link Docker images from release notes ([be94b99](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/commit/be94b99a4d4fb37f7a1b447d054f3ed5a07cd0b4))
* **release:** link Docker images from release notes ([#30](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/issues/30)) ([be94b99](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/commit/be94b99a4d4fb37f7a1b447d054f3ed5a07cd0b4))


### Documentation

* document multi-entry squash commits ([be94b99](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/commit/be94b99a4d4fb37f7a1b447d054f3ed5a07cd0b4))
* expand historical changelog entries ([be94b99](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/commit/be94b99a4d4fb37f7a1b447d054f3ed5a07cd0b4))

## [3.5.0](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/compare/3.4.0...3.5.0) (2026-07-20)


### Features

* **config:** add schema-versioned configuration migrations ([#28](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/issues/28)) ([064016d](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/commit/064016d561988a2981306a51fb61af38a3ff1bd9))
* **points:** add per-streamer channel points limits ([#28](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/issues/28)) ([064016d](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/commit/064016d561988a2981306a51fb61af38a3ff1bd9))
* **priority:** add favorite streamer priority ([#28](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/issues/28)) ([064016d](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/commit/064016d561988a2981306a51fb61af38a3ff1bd9))
* parallelize streamer startup ([#24](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/issues/24)) ([ee947cd](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/commit/ee947cd999027b5f3da4e0cd116c0dd02108ca15))
* persist watch streak sessions ([#26](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/issues/26)) ([9b43ec0](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/commit/9b43ec01c4971123c338848aeb732434e988b7bf))


### Bug Fixes

* **drops:** refresh eligibility and skip collected fallback campaigns ([#28](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/issues/28)) ([064016d](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/commit/064016d561988a2981306a51fb61af38a3ff1bd9))
* resolve fallback Twitch category names ([#27](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/issues/27)) ([5f2b6d5](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/commit/5f2b6d54cd7b114940ad8682eb2e655f8cab669e))
* retry transient release please failures ([#29](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/issues/29)) ([5fd85d4](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/commit/5fd85d41682fe866b4cc46f23b3f600903e30e8d))


### Documentation

* document favorite priority, points limits, and migrated configuration ([#28](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/issues/28)) ([064016d](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/commit/064016d561988a2981306a51fb61af38a3ff1bd9))

## [3.4.0](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/compare/3.3.0...3.4.0) (2026-07-19)


### Features

* **drops:** report session progress during graceful shutdown ([#21](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/issues/21)) ([02aa6d6](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/commit/02aa6d647b07681991d56126aeb4e9ce5671ff03))


### Bug Fixes

* **analytics:** restore dashboards and bound log responses ([#21](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/issues/21)) ([02aa6d6](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/commit/02aa6d647b07681991d56126aeb4e9ce5671ff03))
* harden GQL request retries ([#23](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/issues/23)) ([4fbced7](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/commit/4fbced7164f3796a62ffd58bca4cbf28dbfaea95))


### Performance Improvements

* **analytics:** replace pandas chart filtering with lightweight processing ([#21](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/issues/21)) ([02aa6d6](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/commit/02aa6d647b07681991d56126aeb4e9ce5671ff03))


### Documentation

* **docker:** document reliable graceful shutdown settings ([#21](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/issues/21)) ([02aa6d6](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/commit/02aa6d647b07681991d56126aeb4e9ce5671ff03))

## [3.3.0](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/compare/3.2.0...3.3.0) (2026-07-18)


### Features

* **priority:** add followers to streamer source priority ([#18](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/issues/18)) ([160678a](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/commit/160678a75313912b1e232d439cc7f99c3c6f51b0))


### Bug Fixes

* **config:** restore missing `StreamerSource` imports in migrated configurations ([#18](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/issues/18)) ([160678a](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/commit/160678a75313912b1e232d439cc7f99c3c6f51b0))
* **discovery:** preserve configured and followed streamer source precedence ([#18](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/issues/18)) ([160678a](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/commit/160678a75313912b1e232d439cc7f99c3c6f51b0))
* make streamer source priority default immutable ([619d76d](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/commit/619d76d87d75baa8372a72ebd23bc035148db55e))
* switch away from completed drop campaigns ([9dc4a6d](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/commit/9dc4a6d2472d3a2fcd6d7fc99ca8cc90243443e6))
* switch away from completed drop campaigns ([#17](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/issues/17)) ([7644156](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/commit/7644156144c47b8ed19a8725d6d90c6d2a81d6d4))


### Documentation

* document combined release workflow ([#13](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/issues/13)) ([0fd98e2](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/commit/0fd98e2b680c15703411fe9e9c8d092f1d9eb0f9))
* document streamer source order and analytics configuration ([#18](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/issues/18)) ([160678a](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/commit/160678a75313912b1e232d439cc7f99c3c6f51b0))
* update Docker Hub repository info ([#16](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/issues/16)) ([d94c3f8](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/commit/d94c3f87fb052487c3b1fad3fcbced4f301b4f8e))
* update project ownership and Docker Hub links ([#15](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/issues/15)) ([ae31c29](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/commit/ae31c29b18a10c441a39b5605315cf06339fe12c))

## [3.2.0](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/compare/3.1.1...3.2.0) (2026-07-17)


### Features

* **drops:** batch restricted-channel discovery ([#10](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/issues/10)) ([5d06130](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/commit/5d0613025858a88b544635fcbfbe2b59b9334587))
* **drops:** load campaign and badge catalogs from shared gists ([#10](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/issues/10)) ([5d06130](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/commit/5d0613025858a88b544635fcbfbe2b59b9334587))
* **drops:** support cross-category Special Events campaigns ([#10](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/issues/10)) ([5d06130](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/commit/5d0613025858a88b544635fcbfbe2b59b9334587))


### Performance Improvements

* **drops:** cache gist data and batch catalog persistence ([#10](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/issues/10)) ([5d06130](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/commit/5d0613025858a88b544635fcbfbe2b59b9334587))


### Documentation

* **termux:** mark legacy setup instructions as outdated ([#11](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/issues/11)) ([e57bff9](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/commit/e57bff9f0e5acfd05f5fcce3ffd8109a3a69f468))
* **windows:** expand executable setup and troubleshooting ([#11](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/issues/11)) ([e57bff9](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/commit/e57bff9f0e5acfd05f5fcce3ffd8109a3a69f468))


### Continuous Integration

* avoid duplicate feature-branch test runs ([#11](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/issues/11)) ([e57bff9](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/commit/e57bff9f0e5acfd05f5fcce3ffd8109a3a69f468))

## [3.1.1](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/compare/3.1.0...3.1.1) (2026-07-17)


### Bug Fixes

* use bare release tags on master ([#8](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/issues/8)) ([415591c](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/commit/415591c37e1e87e15e63d3faeacb70b8ff5af163))

## [3.1.0](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/compare/Twitch-Channel-Points-Miner-v2-3.0.0...Twitch-Channel-Points-Miner-v2-3.1.0) (2026-07-16)


### Features

* discover and schedule upcoming Twitch drops ([f0f0a4d](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/commit/f0f0a4dd6afd5d989f949403d55d75d94a1a472a))


### Bug Fixes

* allow Release Please to calculate future versions ([a9b2489](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/commit/a9b24896903ef4bed2319f43da397f57d471442e))
* discover channel-advertised drop campaigns ([77e6382](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/commit/77e63820ae91dd1b773bb6c17d8eacdefb60f0ee))
* skip category discovery when drops inventory is unavailable ([54410df](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/commit/54410dfb28c1cbbd252534fdba2b37c6759c46a7))

## [3.0.0](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/compare/Twitch-Channel-Points-Miner-v2-3.0.0...Twitch-Channel-Points-Miner-v2-3.0.0) (2026-07-16)


### Bug Fixes

* discover channel-advertised drop campaigns ([77e6382](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/commit/77e63820ae91dd1b773bb6c17d8eacdefb60f0ee))

## [3.0.0](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/compare/Twitch-Channel-Points-Miner-v2-2.0.5...Twitch-Channel-Points-Miner-v2-3.0.0) (2026-07-15)


### Features

* add category-based Twitch Drops mining ([222f91a](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/commit/222f91a03136a17024da4a2073a99de5d000f08e))
* add configurable date format for logs and analytics ([a310678](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/commit/a310678f6263e797e6b65d3efba90553513d2a4a)), closes [#818](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/issues/818)
* add persistent Docker configuration with live reload ([0c40ee7](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/commit/0c40ee7f2b21061046574f4dc202eef428e123d1))
* add Python 3.14 support ([7619e1c](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/commit/7619e1c4d9fd2b556baf3b2378ea90e62914659f))
* add typed GQL integration layer ([6cd56bb](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/commit/6cd56bb407fdf2a03688ba230dd8274562e65cf8))
* add typed GQL integration with branch compatibility ([552771f](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/commit/552771f7cca7c86c7d40959b098e73d18acb624b))
* add Windows executable packaging ([8e09e37](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/commit/8e09e37ab830d7eb567aa4acd2800fb42bd3a2a6))
* add Windows releases and improve Drops campaign detection ([42c9757](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/commit/42c97578fc221f7c9b85be95c21a83f00902ddef))
* **analytics:** add bulk streamer data deletion ([815dddc](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/commit/815dddcc2405caf77d275630e741bac62cd7aa3c))
* **analytics:** improve log polling and dashboard refresh ([a6fe140](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/commit/a6fe140c32c2c31e609420caf525a4c11ad83e75))
* configure concurrent streams watched ([#787](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/issues/787)) ([45ac808](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/commit/45ac80808bc5291041588394115824c56ae6b810))


### Bug Fixes

* add Windows release asset packaging to Release Please ([80f2417](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/commit/80f2417aada7f1b208c867aaba3762ccded4fa2c))
* address drop parser review feedback ([d92f832](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/commit/d92f832e66f6bdc5b0de07893096d28dfc6049c7))
* address typed GQL review feedback ([5d490fa](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/commit/5d490fab2497fe92aca6b00b68a41527100b1077))
* associate unlabeled watch drops with single campaigns ([121e329](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/commit/121e3298eb16a15fcaaf690c24d35614490d601d))
* associate unlabeled watch drops with single campaigns ([da7ae56](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/commit/da7ae56b70ebcbca8c08ba2646701426f0f7e865))
* avoid Actions Secrets API in badge workflows ([8c81aed](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/commit/8c81aed3213d6df2b746b2fce8730637c2533ac0))
* clear stale drop eligibility during category refresh ([6f2af06](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/commit/6f2af0609df11e05db48459d4a62bc27a1ed664b))
* **config:** report missing imports clearly ([26a1579](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/commit/26a1579b8e14d830a1691b148183423a5e4b9f4f))
* **dashboard:** improve drop cards and theme restoration ([5465066](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/commit/546506687e08e05852a1d5ea1b185c4c1766d550))
* detect all earned badges in drop campaigns ([d72c704](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/commit/d72c7042f717dbbb3fd38fa2b3105b19ff4ea6c3))
* discover new drops in completed campaigns ([dca34ba](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/commit/dca34ba74d33195e8e857ad77275898ec24cbd0c))
* exclude completed campaigns from drop category discovery ([efe5aa4](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/commit/efe5aa45e1b8cafde31b382e3bb9b6b79f4da468))
* exclude subscriber-only rewards from watch drop campaigns ([e92acf4](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/commit/e92acf40ac1424e12e517fc51988e9bf04159f2c))
* extend watch streak tracking window ([0f6e2b7](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/commit/0f6e2b7fa803dc1c687b08a230ce17074dcbe5df))
* **followers:** restore follower loading support ([ce8043f](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/commit/ce8043f9fa72ca26cd0d8ce4c9caeaf7cbc406b1))
* forward CLI arguments from Windows launcher ([12879ec](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/commit/12879ecc4c8c522221a4e52cfcf7e08199ddde4b))
* grant badge workflows permission to push ([5de0275](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/commit/5de0275abbe067a6106e3df2bae251171dae6778))
* handle unavailable stream info gracefully ([b8818a3](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/commit/b8818a39f18a6105ff851dbc9fe3267ed4d0bc01))
* harden typed GQL response handling ([7e25de4](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/commit/7e25de47edc532bbb6d8e78d57d6516fc6e2e202))
* only report newer GitHub versions ([5754a01](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/commit/5754a01266bd6ff5b37560c5caf02ceabcf9ed9f))
* preserve branch features across typed GQL integration ([eba01df](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/commit/eba01df8ad822bc54826de12e52dc8fde49a19e2))
* preserve drop eligibility during category refresh ([f41bfb6](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/commit/f41bfb692eea6638f5b530171425581e3e10d063))
* preserve transient channel ID lookup failures ([e18b1f6](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/commit/e18b1f695a8a563efb1c77add25e72d37f2ebed3))
* prevent concurrent badge workflow push conflicts ([a9a28a0](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/commit/a9a28a02b30e30b9f9d15dc649802767504d1b0a))
* recover from invalid Twitch auth and partial point data ([3c45414](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/commit/3c45414b17fdf61861464fe4a8b307d03e38adef))
* refresh badge inventory and pin PyInstaller ([a9fc1b1](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/commit/a9fc1b197f0cf3edcf0c29e1cf99f51233629587))
* remove obsolete m3u8 stream playback simulation ([#791](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/issues/791)) ([5733820](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/commit/5733820905577555a1a7a9275dc2463096c47389))
* replace stale Twitch GraphQL operations ([924838e](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/commit/924838e6dc5e8a394ef18e2c57000bfedf582c57))
* retry badge checks and use Python 3 cookie viewer ([feb4714](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/commit/feb4714c8bdd15adb01a0e73f12e3387c1d3764d))
* **security:** harden analytics and remove obsolete dependencies ([8041681](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/commit/80416811bc37447cd19d6ee35f91bb5fb819c027))
* **security:** harden authentication and session storage ([d36b2ee](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/commit/d36b2ee7bcc04964a3f4d4ae4f8071c525eb3b70))
* **security:** harden external requests and analytics logs ([b02cc85](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/commit/b02cc85636eb237e2fcad80a65af9a724b8857bc))
* stop watching completed drop campaigns immediately ([b46a0be](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/commit/b46a0be77e174b8ae191402beaaf40b56f073abd))
* target dispatched branch in release workflow ([cf9e114](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/commit/cf9e11476b695df466cd5cedd449bedd17a92067))
* throttle watch events and update persisted query hash ([995d254](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/commit/995d2544118d07cbc795acffb26d03cb3c26e575))
* use category campaign eligibility for drop selection ([381568e](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/commit/381568e8d4b2ee897bccfb9123ee824c2f14c848))


### Documentation

* add build and testing contribution guides ([9904e4b](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/commit/9904e4b110f6e5c31604e586eebf4b9977bcade7))
* clarify configuration reload limitations ([5cc363f](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/commit/5cc363fba3d2dcb27677cc936bb459df0a58e664))
* document Docker TZ environment setting ([4c047fc](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/commit/4c047fcc019d6f4e5a15e917c4fb152955028094))
* overhaul README for config-based workflow ([1b2742d](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/commit/1b2742d4706d4098e536d567331735e446a2918c))
* update category input and Python guidance ([2f8bb83](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/commit/2f8bb83e6e3b127a03dcbe96e0bc71dc49f53b1e))
