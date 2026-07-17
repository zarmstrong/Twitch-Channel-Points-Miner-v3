# Changelog

## [3.2.0](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/compare/3.1.1...3.2.0) (2026-07-17)


### Features

* load Twitch Drops from shared gists ([#10](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/issues/10)) ([5d06130](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/commit/5d0613025858a88b544635fcbfbe2b59b9334587))


### Documentation

* improve platform setup and test workflow ([#11](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/issues/11)) ([e57bff9](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/commit/e57bff9f0e5acfd05f5fcce3ffd8109a3a69f468))

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
