# Changelog — GridFleet Agent

All notable changes to the GridFleet host agent (`gridfleet-agent` on PyPI) are documented here.

## [0.26.1](https://github.com/quidow/gridfleet/compare/gridfleet-agent-v0.26.0...gridfleet-agent-v0.26.1) (2026-06-10)


### Bug Fixes

* **agent:** raise uvicorn keep-alive above backend http pool idle ceiling ([aeee570](https://github.com/quidow/gridfleet/commit/aeee57096fb8c5dc22387b2a0e49fd41ea8f48a3))
* **backend:** resolve open grid-findings review issues (round 2) ([fdfc4bd](https://github.com/quidow/gridfleet/commit/fdfc4bd7f1c24d40bf53f271e9292afd33bb6949))

## [0.26.0](https://github.com/quidow/gridfleet/compare/gridfleet-agent-v0.25.0...gridfleet-agent-v0.26.0) (2026-06-09)


### Features

* **backend:** tvos appium_env rename and prefer_devicectl toggle ([2e52296](https://github.com/quidow/gridfleet/commit/2e5229642f69d13e5ba32510d3aacab434718cb4))
* **main:** add runtime_packages manifest field for required appium deps ([01383cd](https://github.com/quidow/gridfleet/commit/01383cd4b7d725572fe4c4056b70bca228797d97))


### Bug Fixes

* **agent:** keep runtime-id stable for packs without runtime_packages ([06ab1d4](https://github.com/quidow/gridfleet/commit/06ab1d47fe069fb64d3104be7f948655b45cd80a))

## [0.25.0](https://github.com/quidow/gridfleet/compare/gridfleet-agent-v0.24.0...gridfleet-agent-v0.25.0) (2026-06-07)


### Features

* **agent:** carry generic recommended_action through pack health contract ([70291b5](https://github.com/quidow/gridfleet/commit/70291b5102a2d607040018b53282a72f113c93d6))


### Bug Fixes

* wave-5 review hardening for the grid router migration ([e56ff27](https://github.com/quidow/gridfleet/commit/e56ff2705aa6099beaf070391c519092de82304b))

## [0.24.0](https://github.com/quidow/gridfleet/compare/gridfleet-agent-v0.23.1...gridfleet-agent-v0.24.0) (2026-06-06)


### ⚠ BREAKING CHANGES

* **agent:** the agent no longer runs a Grid relay node and ignores AGENT_GRID_* environment variables.
* **backend:** drop relay fields from agent node-start contract

### Features

* **agent:** remove grid relay; agent manages appium only ([3f91751](https://github.com/quidow/gridfleet/commit/3f9175154a625b29ea396aa3f65cfc37d90c8e28))
* **backend:** drop relay fields from agent node-start contract ([e41a50e](https://github.com/quidow/gridfleet/commit/e41a50e6d3ea98866dd762b5ea70afc3c6025a24))


### Bug Fixes

* **agent:** re-emit has_active_session in node snapshot via localhost Appium ([00de983](https://github.com/quidow/gridfleet/commit/00de983a380caf0547c73743fe7002bb62a87973))


### Documentation

* **docs:** grid router architecture replaces selenium hub ([f24d872](https://github.com/quidow/gridfleet/commit/f24d8723e6f87fdc1f7280da0bdac7f38ca998e9))

## [0.23.1](https://github.com/quidow/gridfleet/compare/gridfleet-agent-v0.23.0...gridfleet-agent-v0.23.1) (2026-06-05)


### Bug Fixes

* **agent:** add hub-registration reconciler with converge matrix ([1b9b815](https://github.com/quidow/gridfleet/commit/1b9b815d1b914d4e947c2cb99a3365d0239bdb14))
* **agent:** add structured hub-node observation ([19a58ba](https://github.com/quidow/gridfleet/commit/19a58ba7adaa047376430eba6d23676a164bf8ae))
* **agent:** flush terminal grid events on bus close ([6200e48](https://github.com/quidow/gridfleet/commit/6200e48ed1d242d930793b67dcaa33af918b5fcd))
* **agent:** reconcile relay hub registration; remove drain self-stop race ([a429452](https://github.com/quidow/gridfleet/commit/a429452c3a41c8eedbcd5a8c1bf09022f654cdb1))
* **agent:** record launch spec before applying reconfigure ([6877bdc](https://github.com/quidow/gridfleet/commit/6877bdcb766890d7b9ba403bf79834001dd38418))
* **agent:** single-owner hub registration; remove drain self-stop ([93003f3](https://github.com/quidow/gridfleet/commit/93003f30f9ad99ef20c613382c11962809ba3c64))
* **agent:** supervisor no longer self-stops the relay on drain-complete ([f4114c9](https://github.com/quidow/gridfleet/commit/f4114c90570b38cec3fbd6df0ce7dede2ec1fb1c))
* **agent:** sweep stray hub registrations on relay start ([06aca14](https://github.com/quidow/gridfleet/commit/06aca14ae2267cf60eb2e75837fdbf9680352c3d))


### Dependencies

* **deps:** bump ruff in /agent in the python-dependencies group ([#515](https://github.com/quidow/gridfleet/issues/515)) ([6907e89](https://github.com/quidow/gridfleet/commit/6907e8909852bd15394518948978131a4ef770e5))

## [0.23.0](https://github.com/quidow/gridfleet/compare/gridfleet-agent-v0.22.3...gridfleet-agent-v0.23.0) (2026-06-05)


### Features

* **agent:** add keep-alive upstream micro-pool for the grid relay ([ca60260](https://github.com/quidow/gridfleet/commit/ca60260e2e7ccb82281dfe51d81171e02265bb0c))
* **agent:** add single-flight async ttl cache utility ([d9999d1](https://github.com/quidow/gridfleet/commit/d9999d195af945efae7ae769e976bd1444d4e20f))
* **agent:** collapse concurrent discovery sweep fallbacks with a ttl cache ([cfffc83](https://github.com/quidow/gridfleet/commit/cfffc83c3d108f642f3f7f295aca013a310c4d2f))
* **agent:** enforce per-request deadline and error mapping in upstream pool ([c55fbfc](https://github.com/quidow/gridfleet/commit/c55fbfce87e4b669b7de982befd7908b60fa0d4b))
* **agent:** flatten per-node background costs and make device queries direct-first ([8b93025](https://github.com/quidow/gridfleet/commit/8b930259c56b4d825ad103de6f344f74e9e46db6))
* **agent:** pass expected device identity through pack health checks ([e5f5be4](https://github.com/quidow/gridfleet/commit/e5f5be4a2482282c356daa1f4d2505a495f3015d))
* **agent:** replace httpx with a keep-alive upstream micro-pool on the relay hot path ([7079860](https://github.com/quidow/gridfleet/commit/70798600d0effd881d3e63984b3b6d5f4e2a55c3))
* **agent:** replace httpx with upstream micro-pool on relay hot path ([8537679](https://github.com/quidow/gridfleet/commit/85376797e93b728732c4aeec196607ed2d0847c4))
* **agent:** resolve pack device properties via direct adapter query before sweep ([eae7cab](https://github.com/quidow/gridfleet/commit/eae7cabf1ed401687ccbc5a8714e13877d921863))
* **agent:** retry stale reused upstream connections once ([9b1754e](https://github.com/quidow/gridfleet/commit/9b1754ea203a477b1b499e82c471cd6814c1cd96))
* **agent:** share one hub status fetch across node registration probes ([27dc652](https://github.com/quidow/gridfleet/commit/27dc65252070719952d9c5065336c8e5c43a575a))


### Bug Fixes

* **agent:** confirm node absence with a fresh hub status fetch before re-registering ([135925e](https://github.com/quidow/gridfleet/commit/135925e79d887e7b4934a3a99b47a58e39322944))

## [0.22.3](https://github.com/quidow/gridfleet/compare/gridfleet-agent-v0.22.2...gridfleet-agent-v0.22.3) (2026-06-04)


### Bug Fixes

* **agent:** adopt completed runtimes from disk on reconcile instead of reinstalling ([#505](https://github.com/quidow/gridfleet/issues/505)) ([b947337](https://github.com/quidow/gridfleet/commit/b947337b2611ef3daf51c946e3ebaf5e438d87d0))

## [0.22.2](https://github.com/quidow/gridfleet/compare/gridfleet-agent-v0.22.1...gridfleet-agent-v0.22.2) (2026-06-04)


### Bug Fixes

* **agent:** drain update on active sessions instead of running nodes ([#501](https://github.com/quidow/gridfleet/issues/501)) ([ddf427f](https://github.com/quidow/gridfleet/commit/ddf427f6b27532e4b737824472a5d959d5ebe302))


### Dependencies

* **deps:** bump starlette from 1.0.0 to 1.0.1 in /agent ([#503](https://github.com/quidow/gridfleet/issues/503)) ([c188ba2](https://github.com/quidow/gridfleet/commit/c188ba2c052dda420ed4e621df697cf656f651dd))


### Documentation

* **docs:** align all docs with the actual implementation state ([#499](https://github.com/quidow/gridfleet/issues/499)) ([1d7a4ea](https://github.com/quidow/gridfleet/commit/1d7a4ea2afafbd5872856a01a9f73792c9ce5f7f))

## [0.22.1](https://github.com/quidow/gridfleet/compare/gridfleet-agent-v0.22.0...gridfleet-agent-v0.22.1) (2026-06-04)


### Performance Improvements

* **agent:** declare sniffio so httpcore stops failing imports per call ([9f873dc](https://github.com/quidow/gridfleet/commit/9f873dc9f37e5388867738ad0370acaecf0ecb07))

## [0.22.0](https://github.com/quidow/gridfleet/compare/gridfleet-agent-v0.21.2...gridfleet-agent-v0.22.0) (2026-06-04)


### Features

* **agent:** add on-demand appium log file helpers ([c989572](https://github.com/quidow/gridfleet/commit/c989572e868151094554ae14b72f10f59e7f8726))
* **agent:** redirect appium output to per-port log files read on demand ([07c49cf](https://github.com/quidow/gridfleet/commit/07c49cffc79fed1d6684b0d146cc399456904a59))
* **agent:** redirect appium output to per-port log files read on demand ([5244b2d](https://github.com/quidow/gridfleet/commit/5244b2d1797dc897de797415c412c45333ec0fb9))
* **agent:** sweep and size-cap appium log files ([ef7d984](https://github.com/quidow/gridfleet/commit/ef7d984143e542ba6f62f8d22f10f4f6ad9681e2))


### Bug Fixes

* **agent:** align relay node-API routes with Selenium 4.43 node protocol ([465ec76](https://github.com/quidow/gridfleet/commit/465ec76c8cb1bfb022da2be0356de401cc88d8fd))
* **agent:** answer hub node-status checks in the relay ([5014fb9](https://github.com/quidow/gridfleet/commit/5014fb997715fa7f42ae86f5003775f5da8fee20))
* **agent:** stop relay servers from capturing process signal handlers ([6a2be9a](https://github.com/quidow/gridfleet/commit/6a2be9a7c1152485d2efc24704ea7ce28557ff64))
* **agent:** stop the relay reservation reaper racing in-flight creates ([34c813c](https://github.com/quidow/gridfleet/commit/34c813ca5c3b116125cdc93d4d3a1811ec20e570))


### Dependencies

* **deps:** bump uvicorn[standard] ([#491](https://github.com/quidow/gridfleet/issues/491)) ([bb1b1aa](https://github.com/quidow/gridfleet/commit/bb1b1aae4f5a4d04c6c796785f2798e92bfc5aa2))

## [0.21.2](https://github.com/quidow/gridfleet/compare/gridfleet-agent-v0.21.1...gridfleet-agent-v0.21.2) (2026-06-03)


### Bug Fixes

* **agent:** default grid_hub_url to localhost for the host-run agent ([b87d3eb](https://github.com/quidow/gridfleet/commit/b87d3eb7d6ac2bcaeaac1c05bd23a5022451cff5))
* **agent:** match platformName case-insensitively in relay slot matching (F3) ([b6ffa52](https://github.com/quidow/gridfleet/commit/b6ffa52e2e0bd8982075b3be145a0e2ef8801661))
* **agent:** probe Appium port with SO_REUSEADDR to match libuv bind ([8557597](https://github.com/quidow/gridfleet/commit/855759788545cbbe27a3bbde4bda5d4be11e2507))
* **agent:** self-heal dropped grid relay hub registrations ([ce99987](https://github.com/quidow/gridfleet/commit/ce99987477f7391ce00c2c173ebf034ba891b497))
* **backend:** device operational-state lifecycle hardening (+ agent relay/grid fixes) ([25dd008](https://github.com/quidow/gridfleet/commit/25dd008c91d7a72a85850a6cd0b84f808ddef4f6))

## [0.21.1](https://github.com/quidow/gridfleet/compare/gridfleet-agent-v0.21.0...gridfleet-agent-v0.21.1) (2026-06-01)


### Bug Fixes

* **agent:** abort appium auto-restart when target served by another node ([facb76d](https://github.com/quidow/gridfleet/commit/facb76db816cbdbb2e33e8e891510d9f06f06d91))
* reap orphan/duplicate appium nodes (backend) and abort racing auto-restart (agent) ([aabdc19](https://github.com/quidow/gridfleet/commit/aabdc198315ecb0da14a90fec3fc26cd67fb3757))

## [0.21.0](https://github.com/quidow/gridfleet/compare/gridfleet-agent-v0.20.1...gridfleet-agent-v0.21.0) (2026-06-01)


### Features

* **backend:** device state derivation Stage 1 Phase 2 — reconciler becomes authoritative writer ([f91a51d](https://github.com/quidow/gridfleet/commit/f91a51db910b68540a30dc4ee9672a728b45ec88))


### Bug Fixes

* **agent:** advertise resolved device serial as grid slot udid ([cf68526](https://github.com/quidow/gridfleet/commit/cf68526ae59cfabc35e82b876100401eda76f687))
* **agent:** drop hardcoded appium:udid grid-slot rewrite; route on deviceId ([fcc0fab](https://github.com/quidow/gridfleet/commit/fcc0fab828e7b35b0189e3f37584e2f6ebd7f2a5))
* move appium:udid out of grid slot stereotype; route on deviceId ([4539b8e](https://github.com/quidow/gridfleet/commit/4539b8ea7c6c10877931828e1b27b74f551f89af))


### Dependencies

* **deps:** bump pytest-asyncio ([#414](https://github.com/quidow/gridfleet/issues/414)) ([60141aa](https://github.com/quidow/gridfleet/commit/60141aa44a0ad883605693cadcd25546aa251a9a))
* **deps:** bump ruff in /agent in the python-dependencies group ([#430](https://github.com/quidow/gridfleet/issues/430)) ([8e75c6e](https://github.com/quidow/gridfleet/commit/8e75c6eafd3229d1596983d4ee6e46def0b2b2e6))

## [0.20.1](https://github.com/quidow/gridfleet/compare/gridfleet-agent-v0.20.0...gridfleet-agent-v0.20.1) (2026-05-25)


### Dependencies

* **deps:** Bump the python-dependencies group in /agent with 2 updates ([#394](https://github.com/quidow/gridfleet/issues/394)) ([3c5ffd9](https://github.com/quidow/gridfleet/commit/3c5ffd9e56088fa81da25c95db4b6c1eafe48520))

## [0.20.0](https://github.com/quidow/gridfleet/compare/gridfleet-agent-v0.19.2...gridfleet-agent-v0.20.0) (2026-05-24)


### Features

* **agent:** add POST /agent/pack/{pack_id}/doctor endpoint ([71c4e72](https://github.com/quidow/gridfleet/commit/71c4e72dc7b815d3ca5f1e58023dc06a19781e29))
* **agent:** auto-run doctor checks on driver install or update ([a17a050](https://github.com/quidow/gridfleet/commit/a17a050e225fa7897caeadeee1a5de69bef8089d))
* **agent:** parse tool_dependencies from desired pack payload ([9339b46](https://github.com/quidow/gridfleet/commit/9339b46c6c837e629e67c02dfd63cc1b867ccc1e))
* **agent:** restructure tool status response with host/packs grouping ([689b205](https://github.com/quidow/gridfleet/commit/689b20518cef2e8fd3b26d18d9b664742753e987))
* **agent:** update ToolsStatusResponse schema for structured tool status ([0c29392](https://github.com/quidow/gridfleet/commit/0c2939224120edec788c69c6a3343beee9eccf0b))
* data-driven driver pack tool dependencies on host overview ([225adc5](https://github.com/quidow/gridfleet/commit/225adc56c475f5ef606b1cacfb61c9715cee57d7))
* on-demand Appium doctor checks ([221256d](https://github.com/quidow/gridfleet/commit/221256d16983974a0324774d85a6cfca99c78ee7))


### Bug Fixes

* **agent:** address code review feedback ([c6be98c](https://github.com/quidow/gridfleet/commit/c6be98c7ff212e37076af03a276fef48b0a02793))
* **agent:** sanitize exception detail in doctor endpoint response ([f5ffc7c](https://github.com/quidow/gridfleet/commit/f5ffc7cf93179d5c419ef5cdfa67f6c6145fec61))
* **agent:** sanitize pack_id before logging in doctor route ([eb13401](https://github.com/quidow/gridfleet/commit/eb13401d8cd4565e043780ebc1b96723266fa86c))
* **agent:** update golden tests for new doctor endpoint ([79ad238](https://github.com/quidow/gridfleet/commit/79ad2380080fe0bd06f73ef460ea799fe578b197))
* **agent:** update remaining tests for structured tool status response ([bbdf675](https://github.com/quidow/gridfleet/commit/bbdf67520254261bff83f0a55bb4ab86bcb19942))
* **agent:** use human-readable display names for host tools ([67f48be](https://github.com/quidow/gridfleet/commit/67f48bed99faec5f142cf2cba23edebad9db6a69))
* **main:** address code review feedback ([701fd5c](https://github.com/quidow/gridfleet/commit/701fd5c1d85e792f766efc188c927bf919fc66bf))
* **main:** truncate pack_id in log and add type guard on agent checks response ([d7f4503](https://github.com/quidow/gridfleet/commit/d7f4503cb37695579c91b65b4310b0dd452080d8))

## [0.19.2](https://github.com/quidow/gridfleet/compare/gridfleet-agent-v0.19.1...gridfleet-agent-v0.19.2) (2026-05-24)


### Bug Fixes

* **agent:** await adapter subprocess_env through IsolatedAdapter wrapper ([#382](https://github.com/quidow/gridfleet/issues/382)) ([31fc79a](https://github.com/quidow/gridfleet/commit/31fc79a22a5f31d2733b50eedebaef8b155ce1b4))

## [0.19.1](https://github.com/quidow/gridfleet/compare/gridfleet-agent-v0.19.0...gridfleet-agent-v0.19.1) (2026-05-24)


### Bug Fixes

* **agent:** await adapter tool_versions through IsolatedAdapter wrapper ([#379](https://github.com/quidow/gridfleet/issues/379)) ([dc11a1d](https://github.com/quidow/gridfleet/commit/dc11a1d820faa569bebbb06cad26fbf5f27d897c))

## [0.19.0](https://github.com/quidow/gridfleet/compare/gridfleet-agent-v0.18.1...gridfleet-agent-v0.19.0) (2026-05-24)


### Features

* **agent:** add resolved_connection_target to LifecycleActionResult ([5955ac0](https://github.com/quidow/gridfleet/commit/5955ac0ffc7c0ed77adc9ad86b67e64fe935fe86))
* **agent:** add tool_versions to DriverPackAdapter and pack_ids to AdapterRegistry ([accecea](https://github.com/quidow/gridfleet/commit/acceceadeaf308f37e9db96e0a1ab248ce1c8b18))


### Bug Fixes

* **agent:** avoid blocking event loop in tool_versions collection ([139e784](https://github.com/quidow/gridfleet/commit/139e784cbe9c722d3c535a2356604a6d2619534b))
* **agent:** remove _get_go_ios_version references from test_tools_and_utilities_more ([ea84f49](https://github.com/quidow/gridfleet/commit/ea84f49ead09d3a2293a4810fa925feac4dce5b8))
* **agent:** update integration test for resolved_connection_target ([ce270e0](https://github.com/quidow/gridfleet/commit/ce270e03aafbc5a630e32f277502125d3e2dc49b))
* **backend,agent:** resolve CodeQL code scanning alerts ([#374](https://github.com/quidow/gridfleet/issues/374)) ([48c1f71](https://github.com/quidow/gridfleet/commit/48c1f71a3743e417fb4960ea3fd183133e38147b))

## [0.18.1](https://github.com/quidow/gridfleet/compare/gridfleet-agent-v0.18.0...gridfleet-agent-v0.18.1) (2026-05-23)


### Bug Fixes

* **agent:** restart grid node service after heartbeat failure ([#350](https://github.com/quidow/gridfleet/issues/350)) ([c3f88af](https://github.com/quidow/gridfleet/commit/c3f88afb711b0b45a4a8e85d15591bc0b11032bd))

## [0.18.0](https://github.com/quidow/gridfleet/compare/gridfleet-agent-v0.17.0...gridfleet-agent-v0.18.0) (2026-05-23)


### ⚠ BREAKING CHANGES

* **agent:** agent no longer exposes the /agent/terminal WS endpoint.

### Bug Fixes

* **agent:** keep cooldown drain across forced relay restart ([#346](https://github.com/quidow/gridfleet/issues/346)) ([6e93247](https://github.com/quidow/gridfleet/commit/6e932479824850eafc660ec12f1bf93c09326324))


### Dependencies

* **deps:** bump idna from 3.11 to 3.15 in /agent ([#308](https://github.com/quidow/gridfleet/issues/308)) ([b96e628](https://github.com/quidow/gridfleet/commit/b96e628040050bbec5ca42dd0162bdfe210ae720))
* **deps:** bump ruff in /agent in the python-dependencies group ([#340](https://github.com/quidow/gridfleet/issues/340)) ([5cdc82a](https://github.com/quidow/gridfleet/commit/5cdc82aec13c3f0c9814430feb4411d62b78a264))


### Documentation

* **docs:** drop web terminal references ([e954fc0](https://github.com/quidow/gridfleet/commit/e954fc065b87ebd951eaddab4b2ad70354055971))


### Code Refactoring

* **agent:** remove web terminal package ([d6b7b37](https://github.com/quidow/gridfleet/commit/d6b7b3725f498449705e70a796bfd8a821720756))

## [0.17.0](https://github.com/quidow/gridfleet/compare/gridfleet-agent-v0.16.0...gridfleet-agent-v0.17.0) (2026-05-20)


### Features

* **agent:** track per-session WebDriver activity for idle expiry ([#319](https://github.com/quidow/gridfleet/issues/319)) ([7988a4b](https://github.com/quidow/gridfleet/commit/7988a4bb35ca9fdf2252097f148fa926ebebc429))


### Bug Fixes

* **agent:** preserve stop_pending intent across appium restart ([#315](https://github.com/quidow/gridfleet/issues/315)) ([7e48e5f](https://github.com/quidow/gridfleet/commit/7e48e5f51887496c643abf371656ede5c103fed7))
* **agent:** purge retired packs from RuntimeRegistry ([#317](https://github.com/quidow/gridfleet/issues/317)) ([579d6d8](https://github.com/quidow/gridfleet/commit/579d6d81c9e9779119562dae08a5540f91ef9da5))
* **agent:** read grid_node settings from per-domain config attribute ([#313](https://github.com/quidow/gridfleet/issues/313)) ([d45027b](https://github.com/quidow/gridfleet/commit/d45027ba89e75b1a198d233ce5ead5b9dea32138))
* **agent:** reap stuck reservations in grid relay heartbeat ([#314](https://github.com/quidow/gridfleet/issues/314)) ([4842465](https://github.com/quidow/gridfleet/commit/48424655932134eda375c53ed8dd9643f8a2492b))
* **agent:** rotate host_id consistently across awaiters and pack client ([#318](https://github.com/quidow/gridfleet/issues/318)) ([f7c4937](https://github.com/quidow/gridfleet/commit/f7c493798d1890ac09e64f83f7b514dcb654fdb8))
* **agent:** surface sidecar start failures in pack status ([#316](https://github.com/quidow/gridfleet/issues/316)) ([3848a1d](https://github.com/quidow/gridfleet/commit/3848a1d196e9209293438d1e656cb75f2f617da9))


### Performance Improvements

* **agent:** reuse shared HTTP client and offload tarball hashing ([#311](https://github.com/quidow/gridfleet/issues/311)) ([b01313f](https://github.com/quidow/gridfleet/commit/b01313f337612ea6f26a3fed78e3f8e89f68d5ce))

## [0.16.0](https://github.com/quidow/gridfleet/compare/gridfleet-agent-v0.15.2...gridfleet-agent-v0.16.0) (2026-05-18)


### Features

* selenium grid stability + performance wins ([b5370cc](https://github.com/quidow/gridfleet/commit/b5370ccd539e6692dcec500efe9cf3f7ce59268f))


### Bug Fixes

* **agent:** merge stereotype caps into session creation response ([9255c52](https://github.com/quidow/gridfleet/commit/9255c525dcae75154a941f019970b0e62c6c02e4))
* **agent:** report grid node version 4.43.0 matching hub pin ([450423b](https://github.com/quidow/gridfleet/commit/450423bb3f7f2153e27bfaef3fb8e9dec1a6baf7))
* **backend,agent:** close cooldown→grid-routing race window ([ba734a7](https://github.com/quidow/gridfleet/commit/ba734a72a5a57f7c3c8afd4a841864364ae7e906))
* **backend,agent:** close cooldown→grid-routing race window ([39a4886](https://github.com/quidow/gridfleet/commit/39a48868e52fc0135189da6205beea90df4defe8))
* **backend,agent:** deliver agent reconfigure inline on cooldown escalation ([bfd72e6](https://github.com/quidow/gridfleet/commit/bfd72e6f58bab2e782b0dd8ec0e4dac087c0169c))
* **backend:** bound inline cooldown reconfigure timeout to 5s ([0d8429a](https://github.com/quidow/gridfleet/commit/0d8429a7afa974af0158961b8eada59fd320362c))


### Dependencies

* **deps:** bump ruff in /agent ([#294](https://github.com/quidow/gridfleet/issues/294)) ([0f82674](https://github.com/quidow/gridfleet/commit/0f826741fbd1ebb90eeff8ab169b4aee4da7c91e))
* **deps:** bump uvicorn[standard] in /agent ([#293](https://github.com/quidow/gridfleet/issues/293)) ([96f8ec9](https://github.com/quidow/gridfleet/commit/96f8ec904f08572855b5481e5f02be83f979bb21))

## [0.15.2](https://github.com/quidow/gridfleet/compare/gridfleet-agent-v0.15.1...gridfleet-agent-v0.15.2) (2026-05-17)


### Bug Fixes

* **agent:** raise grid node session inactivity timeout default to 1800s ([a794993](https://github.com/quidow/gridfleet/commit/a79499303b638fcee50427a5c79bea4351042443))
* session-safe graceful node stops + adapter probe + idle timeout ([0cc0689](https://github.com/quidow/gridfleet/commit/0cc068958c1c7df2529b6146e1446e3fc0ca180f))

## [0.15.1](https://github.com/quidow/gridfleet/compare/gridfleet-agent-v0.15.0...gridfleet-agent-v0.15.1) (2026-05-17)


### Bug Fixes

* **agent:** fail fast on non-appium port squatter ([ef2a30c](https://github.com/quidow/gridfleet/commit/ef2a30c5ef2e5e45dfba34eb841aa389ef3df4f8))
* rotate past occupied appium ports during verification ([52b62c0](https://github.com/quidow/gridfleet/commit/52b62c0e3017a19b9dd87a2db69c62fe90d8149e))


### Documentation

* **agent:** explain bind-probe safety and CodeQL dismissal ([fec82bb](https://github.com/quidow/gridfleet/commit/fec82bb680ad03c812fe0c74985521b4658f8b67))

## [0.15.0](https://github.com/quidow/gridfleet/compare/gridfleet-agent-v0.14.0...gridfleet-agent-v0.15.0) (2026-05-16)


### Features

* **agent:** add --advertise-ip install flag ([#279](https://github.com/quidow/gridfleet/issues/279)) ([ad990c1](https://github.com/quidow/gridfleet/commit/ad990c193331cb4e79f9de0298ec008c44ae1265))


### Bug Fixes

* **agent:** preserve per-slot caps on grid node reconfigure ([#282](https://github.com/quidow/gridfleet/issues/282)) ([1dbc7a9](https://github.com/quidow/gridfleet/commit/1dbc7a90bbbfe631909ebd5e44ecd267b061bb7c))

## [0.14.0](https://github.com/quidow/gridfleet/compare/gridfleet-agent-v0.13.1...gridfleet-agent-v0.14.0) (2026-05-16)


### ⚠ BREAKING CHANGES

* **agent:** drop gridfleet:available stereotype emission

### Features

* **agent:** drop gridfleet:available stereotype emission ([4d4101f](https://github.com/quidow/gridfleet/commit/4d4101fcd6180bf094075544760a35bd1057d225))


### Bug Fixes

* **backend:** cover device.id None guard + clarify drop rationale ([e8711a6](https://github.com/quidow/gridfleet/commit/e8711a6ccd5ed3317aea212eb593d70cb6a2e811))

## [0.13.1](https://github.com/quidow/gridfleet/compare/gridfleet-agent-v0.13.0...gridfleet-agent-v0.13.1) (2026-05-16)


### Bug Fixes

* **backend,agent:** propagate os_version_display through property refresh ([#264](https://github.com/quidow/gridfleet/issues/264)) ([f96c3bb](https://github.com/quidow/gridfleet/commit/f96c3bb4c72b253883c7f6852593e2e474e10f44))

## [0.13.0](https://github.com/quidow/gridfleet/compare/gridfleet-agent-v0.12.1...gridfleet-agent-v0.13.0) (2026-05-16)


### Features

* **agent:** add os_version_display field to NormalizedDevice ([67fa911](https://github.com/quidow/gridfleet/commit/67fa911a5436ebe1cc4d8efbe8671e645225d432))
* **backend,agent,frontend:** split Fire OS display version from routing major ([a289455](https://github.com/quidow/gridfleet/commit/a2894559f41bd15b3a6a60e593021d1b2049d778))

## [0.12.1](https://github.com/quidow/gridfleet/compare/gridfleet-agent-v0.12.0...gridfleet-agent-v0.12.1) (2026-05-15)


### Bug Fixes

* **agent:** report user-data disk usage on macOS in decimal GB ([e63e213](https://github.com/quidow/gridfleet/commit/e63e213389aa9e8e74c2bc50b7ebdc800e170d06))
* **agent:** silence httpx logger to break shipper feedback loop ([5762283](https://github.com/quidow/gridfleet/commit/576228354aa19118955734657ffa368bd54b78d1))
* **agent:** silence shipper feedback loop and correct macOS disk reporting ([fd77ef6](https://github.com/quidow/gridfleet/commit/fd77ef6bc395d3530afbf3cc5d363509e8836188))

## [0.12.0](https://github.com/quidow/gridfleet/compare/gridfleet-agent-v0.11.0...gridfleet-agent-v0.12.0) (2026-05-15)


### Features

* **agent:** add in-memory log ring buffer ([13914cd](https://github.com/quidow/gridfleet/commit/13914cda19d6c4bd097c7772150120a8da31ac70))
* **agent:** collect host hardware metadata for registration ([21058a1](https://github.com/quidow/gridfleet/commit/21058a178cf599f290c7bd4becdb3783a5d9ab69))
* **agent:** include host_info in registration payload ([9d27f8f](https://github.com/quidow/gridfleet/commit/9d27f8fdb35714dc5e2c5104c46ce10655df7b8e))
* **agent:** queue shippable log lines ([091928c](https://github.com/quidow/gridfleet/commit/091928c49f007502772e36df348579dbb2a8557e))
* **agent:** ship log batches with retry ([db59f92](https://github.com/quidow/gridfleet/commit/db59f9235b194520a82de700ca3373f7c6724b4c))
* **agent:** wire log shipper into lifespan ([b41802c](https://github.com/quidow/gridfleet/commit/b41802c5bbd661772797c6b4bd4854769ad5905d))
* **frontend:** add host logs tab ([f21de6a](https://github.com/quidow/gridfleet/commit/f21de6af1964224f6f4bac132297829e3a5e9426))
* surface host hardware metadata on host detail ([de6116e](https://github.com/quidow/gridfleet/commit/de6116e5155d80eee9e868cbc69c56cdfa027afa))


### Bug Fixes

* **agent:** drop unused ring buffer and surface log shipper drops ([8073e0d](https://github.com/quidow/gridfleet/commit/8073e0d3294c8a062891f296971e4559c8c61ccd))
* **agent:** harden host hardware probe against hangs and transient failures ([871286b](https://github.com/quidow/gridfleet/commit/871286b4ee2f6c4e3cd3430ef93752e0ff8248aa))
* **agent:** use list singleton instead of global-cached for hardware snapshot ([304db1c](https://github.com/quidow/gridfleet/commit/304db1cf791e30e706896af1866508175109f173))

## [0.11.0](https://github.com/quidow/gridfleet/compare/gridfleet-agent-v0.10.0...gridfleet-agent-v0.11.0) (2026-05-15)


### Features

* **agent:** install ~/.local/bin/gridfleet-agent shim ([88dcfbe](https://github.com/quidow/gridfleet/commit/88dcfbe637327e9782df34d463b0069740d84457))


### Bug Fixes

* **agent:** set AGENT_RUNTIME_ROOT in launchd plist ([977bdd2](https://github.com/quidow/gridfleet/commit/977bdd20ecad8be99d5252389b4d8a00d6a3f72d))

## [0.10.0](https://github.com/quidow/gridfleet/compare/gridfleet-agent-v0.9.0...gridfleet-agent-v0.10.0) (2026-05-15)


### ⚠ BREAKING CHANGES

* **agent:** extract per-domain routers from main.py (PR #1 of 3) ([#218](https://github.com/quidow/gridfleet/issues/218))
* **agent:** user-scope install without sudo ([#209](https://github.com/quidow/gridfleet/issues/209))
* **agent:** remove direct appium probe session endpoint

### Features

* **agent:** add global exception handlers emitting ErrorEnvelope shape ([da2bc5a](https://github.com/quidow/gridfleet/commit/da2bc5a579f994d743fdf5f0b7bade15fd8bfd07))
* **agent:** add openapi metadata and grouped settings ([#220](https://github.com/quidow/gridfleet/issues/220)) ([186e05b](https://github.com/quidow/gridfleet/commit/186e05b201e23512a46000396e80d1f8ed5748f2))
* **agent:** bind AGENT_HOST_ID and AGENT_BACKEND_URL into per-domain settings ([d15f3e8](https://github.com/quidow/gridfleet/commit/d15f3e8c6ceaa7cfb2b5c18fd2095edb31c15751))
* **agent:** close FastAPI best-practice gaps ([443372e](https://github.com/quidow/gridfleet/commit/443372ecf9defd976cc17f327cdf81a5d233c211))
* **agent:** emit Retry-After on runtime start failures ([a8a0849](https://github.com/quidow/gridfleet/commit/a8a0849fbca367f6b5935f5c7ccb81909a7f3ed5))
* **agent:** expose public grid supervisor accessors on AppiumProcessManager ([458e4a5](https://github.com/quidow/gridfleet/commit/458e4a5448d8b9e37a8499d04c9b120d4b997c5f))
* **agent:** expose registration state in /agent/health response ([4dc6ed3](https://github.com/quidow/gridfleet/commit/4dc6ed3463a5bdf2bb99b922010aa1c8f804f749))
* **agent:** extract per-domain routers from main.py (PR [#1](https://github.com/quidow/gridfleet/issues/1) of 3) ([#218](https://github.com/quidow/gridfleet/issues/218)) ([7d440bb](https://github.com/quidow/gridfleet/commit/7d440bb257c7af70009e7de1ddba2441dda9ae8c))
* **agent:** per-domain dependencies.py for every router ([#228](https://github.com/quidow/gridfleet/issues/228)) ([146cac5](https://github.com/quidow/gridfleet/commit/146cac5878c08024a9f768c5ed6ead576cfff3e5))
* **agent:** remove direct appium probe session endpoint ([9113719](https://github.com/quidow/gridfleet/commit/9113719583811738ad42056f7ec6c29d8b247de6))
* **agent:** response_model + uniform metadata on every route ([#227](https://github.com/quidow/gridfleet/issues/227)) ([990472e](https://github.com/quidow/gridfleet/commit/990472ee71864610930d1f4dc6a901f6eb8e72e4))
* **agent:** store credentials and tokens as SecretStr to prevent repr leakage ([c754391](https://github.com/quidow/gridfleet/commit/c754391c2c721a306aa1e9655349f605999f795d))
* **agent:** supervise lifespan background tasks with crash-logging watchdog ([baec041](https://github.com/quidow/gridfleet/commit/baec04103d07f1192674de86d4ba858997a2fb28))
* **agent:** tighten response models with typed cores and named extras fields ([fd591e3](https://github.com/quidow/gridfleet/commit/fd591e3918f2a7e9f8f07daf012c823f5ab28745))
* **agent:** typed schemas + field constraints + per-domain exceptions ([#224](https://github.com/quidow/gridfleet/issues/224)) ([68fad2c](https://github.com/quidow/gridfleet/commit/68fad2ccf4b971325d29f85477c3b0128f5a64f0))
* **agent:** user-scope install without sudo ([#209](https://github.com/quidow/gridfleet/issues/209)) ([bbef2e9](https://github.com/quidow/gridfleet/commit/bbef2e9055752d1c90a4e6e3a84e510c1dd770e7))
* **agent:** validate pack-router query params via Annotated and shared regex ([73a4bf3](https://github.com/quidow/gridfleet/commit/73a4bf31d2913ff5d4752ca686317d8c2e663c56))
* **agent:** wire routers to annotated dependencies ([#219](https://github.com/quidow/gridfleet/issues/219)) ([967bce0](https://github.com/quidow/gridfleet/commit/967bce0b286bb776a3449d498e1f455c6befe6f9))
* **backend:** codegen Pydantic models from agent OpenAPI ([#235](https://github.com/quidow/gridfleet/issues/235)) ([8c70b5c](https://github.com/quidow/gridfleet/commit/8c70b5c1ca3baad63582e17030e54dbc76bc5503))


### Bug Fixes

* **agent:** cap grid node maxSessions at 1 per device ([#237](https://github.com/quidow/gridfleet/issues/237)) ([f3d1b26](https://github.com/quidow/gridfleet/commit/f3d1b2679da36835f75f1e2822c92573ede2630b))
* **agent:** clear CodeQL warnings in tests ([589b715](https://github.com/quidow/gridfleet/commit/589b71564e3c732a32af32226fbedd39f0ae72db))
* **agent:** harden uninstall against half-broken host state ([c069052](https://github.com/quidow/gridfleet/commit/c069052c7d40d2a3a485d6ebc1ebbe38fb23cdcf))
* **agent:** keep adapter-fed pack responses permissive ([954a31a](https://github.com/quidow/gridfleet/commit/954a31a8b9c4a8a1e5d03188be44713d576e5a5d))
* **agent:** retry 4xx registration failures with sanitized body logging ([8258c6c](https://github.com/quidow/gridfleet/commit/8258c6c7e468924926ca7a66f40011ac247fed8e))
* **agent:** reuse shared httpx client for pack tarball downloads ([30de8eb](https://github.com/quidow/gridfleet/commit/30de8eb05037ea4f4c50b26d18af673277e5106f))
* **agent:** silence CodeQL "statement has no effect" on bare await task ([f346f59](https://github.com/quidow/gridfleet/commit/f346f5998db1529274a9874dd19510fb2ed7889f))
* **agent:** stop leaking raw RuntimeError messages from /agent/appium/start ([f74fd81](https://github.com/quidow/gridfleet/commit/f74fd81d4607f5db553492eb330430824939b433))
* **agent:** wrap host telemetry in dep and thin sync_agent_plugins ([#230](https://github.com/quidow/gridfleet/issues/230)) ([5721f0c](https://github.com/quidow/gridfleet/commit/5721f0ca1456f1b69f3493c7aa9eb98c4d7d2328))
* **main:** route probe sessions through grid ([#211](https://github.com/quidow/gridfleet/issues/211)) ([5f7ef90](https://github.com/quidow/gridfleet/commit/5f7ef9036492949ba0dfb756ae5c84a3f3a9bb8a))

## [0.9.0](https://github.com/quidow/gridfleet/compare/gridfleet-agent-v0.8.0...gridfleet-agent-v0.9.0) (2026-05-13)


### Features

* **agent:** support live appium node reconfiguration ([36186a1](https://github.com/quidow/gridfleet/commit/36186a1dbb859a5d29e80223d06215cd868daed8))
* **main:** add device orchestration intent registry ([b42b3d4](https://github.com/quidow/gridfleet/commit/b42b3d47e96e1ee1257bdd6f7676f027eed6de57))


### Bug Fixes

* **agent:** address code scanning review comments ([e816232](https://github.com/quidow/gridfleet/commit/e8162324a60776574a73ce852f0ef9ec5d186317))
* **agent:** stop advertising global appium capability ([546890a](https://github.com/quidow/gridfleet/commit/546890adc68a822bab1dc260c7d0b00b8f21413d))
* **main:** satisfy intent registry verification ([5b0a097](https://github.com/quidow/gridfleet/commit/5b0a097788e2cd128d0e9d5721fe12602785b4bb))


### Dependencies

* **deps:** bump mypy in /agent ([#195](https://github.com/quidow/gridfleet/issues/195)) ([1317e59](https://github.com/quidow/gridfleet/commit/1317e59bbd4ae6969ed3c717c24b43dbfefec722))

## [0.8.0](https://github.com/quidow/gridfleet/compare/gridfleet-agent-v0.7.0...gridfleet-agent-v0.8.0) (2026-05-12)


### ⚠ BREAKING CHANGES

* **agent:** remove tool ensure endpoint and management logic

### Features

* **agent:** remove tool ensure endpoint and management logic ([89a581c](https://github.com/quidow/gridfleet/commit/89a581c5221c6272882bf6e4f3ee90b25052cba5))
* remove host tool ensure/version management ([#190](https://github.com/quidow/gridfleet/issues/190)) ([b2562c1](https://github.com/quidow/gridfleet/commit/b2562c16d75ef14c0f4c9131c03151b73f337802))


### Bug Fixes

* **agent:** stop requiring global appium runtime ([ef70225](https://github.com/quidow/gridfleet/commit/ef702257983f6d2ae7a69fae721c1b071b5121a4))

## [0.7.0](https://github.com/quidow/gridfleet/compare/gridfleet-agent-v0.6.2...gridfleet-agent-v0.7.0) (2026-05-11)


### Features

* **agent:** add grid node reregister endpoint ([be50f4c](https://github.com/quidow/gridfleet/commit/be50f4c84eeed7faf61921b82362471ca7044b2b))
* **agent:** default grid node run id to free ([fd88321](https://github.com/quidow/gridfleet/commit/fd883213c8ac6808de93f4e2b9779bdd1e5be257))
* **agent:** reregister grid node with updated stereotype ([8572e08](https://github.com/quidow/gridfleet/commit/8572e084412fad10acac891f1032fcafece0cb14))
* **agent:** support mutable grid node stereotypes ([9715cd6](https://github.com/quidow/gridfleet/commit/9715cd6474e3606e2a8ce0eb3f63b9a7fa0cc172))


### Dependencies

* **deps:** bump pydantic-settings in /agent ([#182](https://github.com/quidow/gridfleet/issues/182)) ([a6abb83](https://github.com/quidow/gridfleet/commit/a6abb83c6367703ca8acc8c0008a2762d0dcc958))

## [0.6.2](https://github.com/quidow/gridfleet/compare/gridfleet-agent-v0.6.1...gridfleet-agent-v0.6.2) (2026-05-10)


### Bug Fixes

* **agent:** release adapter-owned doctor refactor ([#165](https://github.com/quidow/gridfleet/issues/165)) ([f3ae257](https://github.com/quidow/gridfleet/commit/f3ae25787e2c8ef926312f11d2313c6513f8bfa9))

## [0.6.1](https://github.com/quidow/gridfleet/compare/gridfleet-agent-v0.6.0...gridfleet-agent-v0.6.1) (2026-05-10)


### Bug Fixes

* **agent:** split grid node bind host from advertised uri to fix linux docker setups ([#157](https://github.com/quidow/gridfleet/issues/157)) ([8e98a0d](https://github.com/quidow/gridfleet/commit/8e98a0dc28f9d5eedb5aa35566a83e48a6cca4fa))

## [0.6.0](https://github.com/quidow/gridfleet/compare/gridfleet-agent-v0.5.0...gridfleet-agent-v0.6.0) (2026-05-10)


### ⚠ BREAKING CHANGES

* **agent:** replace java grid relay with python grid node service ([#148](https://github.com/quidow/gridfleet/issues/148))

### Features

* **agent:** replace java grid relay with python grid node service ([#148](https://github.com/quidow/gridfleet/issues/148)) ([05f7604](https://github.com/quidow/gridfleet/commit/05f760426c3151b2c4b264d0c9e290469f60de28))


### Bug Fixes

* **agent:** close five remaining grid node review findings ([#156](https://github.com/quidow/gridfleet/issues/156)) ([d065ef2](https://github.com/quidow/gridfleet/commit/d065ef24182133b354ce667bb2bc679387326ddc))
* **agent:** match capabilities across always-match + first-match on grid node ([#155](https://github.com/quidow/gridfleet/issues/155)) ([d893655](https://github.com/quidow/gridfleet/commit/d89365546845228be01b45f5b1479c18a790fd4c))
* **agent:** populate session info in grid node status payload ([#153](https://github.com/quidow/gridfleet/issues/153)) ([7a34932](https://github.com/quidow/gridfleet/commit/7a349328000e087219816d8f3e2c03e32b052264))
* **agent:** share httpx async client across probe, registration, and pack-state loops ([#150](https://github.com/quidow/gridfleet/issues/150)) ([874aa11](https://github.com/quidow/gridfleet/commit/874aa1164a86a841fce36240f9711d2733ad8bc6))
* **agent:** tighten grid node lifecycle and capability edge cases ([#154](https://github.com/quidow/gridfleet/issues/154)) ([d5d9a04](https://github.com/quidow/gridfleet/commit/d5d9a047d50c84cc69d982752576a5b1450bff1d))

## [0.5.0](https://github.com/quidow/gridfleet/compare/gridfleet-agent-v0.4.0...gridfleet-agent-v0.5.0) (2026-05-10)


### Features

* **main:** add optional icmp ping health check for usb devices with saved ip ([#143](https://github.com/quidow/gridfleet/issues/143)) ([afda5ce](https://github.com/quidow/gridfleet/commit/afda5ce5527167bcd47cb04f227a791ab3cdea1b))

## [0.4.0](https://github.com/quidow/gridfleet/compare/gridfleet-agent-v0.3.0...gridfleet-agent-v0.4.0) (2026-05-08)


### ⚠ BREAKING CHANGES

* **backend:** derive device health summary on read ([#78](https://github.com/quidow/gridfleet/issues/78))

### Features

* **agent:** add --user to status/update/uninstall and --uv-bin to update ([da0636c](https://github.com/quidow/gridfleet/commit/da0636c83dcdad45082f403266ad759dc224c94c))
* **agent:** add operator identity resolver primitive ([575f05f](https://github.com/quidow/gridfleet/commit/575f05ff9d90dd8131eec1e540e73fda7d344558))
* **agent:** add uv runtime discovery and operator-context upgrade command ([a608e12](https://github.com/quidow/gridfleet/commit/a608e1230a76a28e3650fedb9a34ea8394d71079))
* **agent:** enforce optional http basic auth on backend-&gt;agent calls ([#100](https://github.com/quidow/gridfleet/issues/100)) ([00f985e](https://github.com/quidow/gridfleet/commit/00f985e58391b356861a94147b89d502bd57df35))
* **agent:** status reports operator identity and resolved uv path ([2b23756](https://github.com/quidow/gridfleet/commit/2b2375693ca5929c47c6f8321ba11c860d8c1012))


### Bug Fixes

* **agent:** address pr review findings on cli operator path ([67b41c3](https://github.com/quidow/gridfleet/commit/67b41c36041e9d51ae3e30e619260a85e735d3a0))
* **agent:** bootstrap script passes --user so service does not run as root ([a3a4272](https://github.com/quidow/gridfleet/commit/a3a42724b0163eee457d971d848c2d48527e2eae))
* **agent:** chown install artefacts to operator on macos as well as linux ([154f97f](https://github.com/quidow/gridfleet/commit/154f97fae12347bec2f40164dd990269addc7af6))
* **agent:** operator identity through CLI install/update/status ([4e717eb](https://github.com/quidow/gridfleet/commit/4e717ebd2016a6925066079194d394ac466b4f50))
* **agent:** pass operator to status service file resolution ([99ab0b0](https://github.com/quidow/gridfleet/commit/99ab0b089cf262568d3a85b4af3cdcbbfbf58a31))
* **agent:** resolve operator identity before install so service does not run as root ([26fe674](https://github.com/quidow/gridfleet/commit/26fe6748beba353560ea594b7840481ffcf64f4e))
* **agent:** restore current_home branch in uv discovery ([c566add](https://github.com/quidow/gridfleet/commit/c566addb6ad82ee5cf854447a94bb8518833b972))
* **agent:** uninstall uses operator identity for launchctl domain ([0efe41b](https://github.com/quidow/gridfleet/commit/0efe41b7f431c4b403af60f8c39fb67b3efc2815))
* **agent:** update runs uv as operator and maps drain/health failures to exit 1 ([a29bf57](https://github.com/quidow/gridfleet/commit/a29bf5757149011660882bafdfa05fc053a34773))
* **agent:** wrap discover_uv in update cli, fix readme version pin, gate chown on euid ([315f780](https://github.com/quidow/gridfleet/commit/315f780c03de00c904b35926a0674dfb986a31d8))
* **backend,agent:** close 52 codeql alerts ([#115](https://github.com/quidow/gridfleet/issues/115)) ([05190ac](https://github.com/quidow/gridfleet/commit/05190ac32e7be9c2b979513114230f51705a0422))


### Documentation

* **agent:** correct troubleshooting row for registration pending ([0f6b316](https://github.com/quidow/gridfleet/commit/0f6b31691e2c55878edd7b1aa468bd962cef3b80))
* **agent:** rewrite readme to match locked cli spec ([fafd38a](https://github.com/quidow/gridfleet/commit/fafd38a689844db9cadbdb06cfdf8a6c3c194b4a))


### Code Refactoring

* **backend:** derive device health summary on read ([#78](https://github.com/quidow/gridfleet/issues/78)) ([10078ef](https://github.com/quidow/gridfleet/commit/10078ef89dcf12e855776a68002456302c51684c))

## [0.3.0](https://github.com/quidow/gridfleet/compare/gridfleet-agent-v0.2.4...gridfleet-agent-v0.3.0) (2026-05-05)


### ⚠ BREAKING CHANGES

* typed Appium resource claims + structured agent errors ([#77](https://github.com/quidow/gridfleet/issues/77))

### Bug Fixes

* **backend:** stop transient agent blips from flapping device health ([#61](https://github.com/quidow/gridfleet/issues/61)) ([a58c8e5](https://github.com/quidow/gridfleet/commit/a58c8e5e835b72f5abde69bd078b2868c7cc84d5))


### Code Refactoring

* typed Appium resource claims + structured agent errors ([#77](https://github.com/quidow/gridfleet/issues/77)) ([9bfbc30](https://github.com/quidow/gridfleet/commit/9bfbc300df5fe779f91ba0ba00cc3b8fa2a589e9))

## [0.2.4](https://github.com/quidow/gridfleet/compare/gridfleet-agent-v0.2.3...gridfleet-agent-v0.2.4) (2026-05-03)


### Bug Fixes

* **agent:** trigger release for port conflict cleanup ([6a561ca](https://github.com/quidow/gridfleet/commit/6a561ca480c62b9abb2d5141fa98fc4e1a7696b6))

## [0.2.3](https://github.com/quidow/gridfleet/compare/gridfleet-agent-v0.2.2...gridfleet-agent-v0.2.3) (2026-05-03)


### Bug Fixes

* **agent:** prioritize node in service path ([c1d2b72](https://github.com/quidow/gridfleet/commit/c1d2b728d5d01a4a0b76f53ca48be8740de17918))

## [0.2.2](https://github.com/quidow/gridfleet/compare/gridfleet-agent-v0.2.1...gridfleet-agent-v0.2.2) (2026-05-03)


### Bug Fixes

* **agent:** prefer nvm node during install ([b0e672b](https://github.com/quidow/gridfleet/commit/b0e672b22e761593c657ae6d54b665f0112a61df))
* **agent:** support sh installer and auth hint ([81cc1fd](https://github.com/quidow/gridfleet/commit/81cc1fd0fb2d344baa135b31665f216d7d607c75))

## [0.2.1](https://github.com/quidow/gridfleet/compare/gridfleet-agent-v0.2.0...gridfleet-agent-v0.2.1) (2026-05-02)


### Bug Fixes

* **agent:** close port-allocator and adapter-loader race windows ([#23](https://github.com/quidow/gridfleet/issues/23)) ([4bea799](https://github.com/quidow/gridfleet/commit/4bea799dd6f7931223ec2d2828de5c1e83bf8b8c))
* **agent:** dedup and isolate tarball_fetch targets ([#27](https://github.com/quidow/gridfleet/issues/27)) ([f83ac99](https://github.com/quidow/gridfleet/commit/f83ac991b8b7f9d1916b64fc465187f1995274c7))
* **agent:** hold _start_lock across AppiumProcessManager.stop() body ([#24](https://github.com/quidow/gridfleet/issues/24)) ([a42f1da](https://github.com/quidow/gridfleet/commit/a42f1da759e52add383e9eea0852a85d5633c4e8))
* **agent:** idempotent bootstrap installer with sudo and launchd handling ([#51](https://github.com/quidow/gridfleet/issues/51)) ([db0f059](https://github.com/quidow/gridfleet/commit/db0f059d5288979bbca314fbcf2e92e09e888be8))
* **agent:** reset to 0.2.0, drop --locked from ci ([c6ee2ea](https://github.com/quidow/gridfleet/commit/c6ee2eab4ba7d4b761136cdea1a929d6e22bca3f))
* **agent:** use importlib.metadata for version, fix publish lock files ([b96a112](https://github.com/quidow/gridfleet/commit/b96a112db50ef8e7c8d5bd1524104d7f27cb5afd))
* authenticate agent driver pack tarball fetches ([898859e](https://github.com/quidow/gridfleet/commit/898859eae0ced10a6109058ac6aeab4b6c851934))
* **ci:** update agent lock file, add auto-lockfile workflow, fix local commitlint hook ([920b71e](https://github.com/quidow/gridfleet/commit/920b71eeaa942b33c711a3dcb75115b37525947c))

## 0.2.0

### Features

- Rewrite bootstrap installer to use `uv tool install` instead of manual venv creation. Users no longer need Python 3.12+ pre-installed — `uv` handles it.
- Replace `validate_dedicated_venv` with `resolve_bin_path` — the agent no longer requires running from `/opt/gridfleet-agent/venv/bin/`. Supports `uv tool install` paths natively.
- Add `bin_path` to `InstallConfig` for configurable binary resolution in service unit templates (systemd/launchd).
- Replace `pip install --upgrade` with `uv tool upgrade gridfleet-agent` in the update flow.
- Add upgrade awareness: the agent caches version guidance from the manager's registration response and surfaces it on `/agent/health`, `HealthCheckResult.details`, and `gridfleet-agent status` CLI output.
- Use `importlib.metadata` for runtime version resolution — eliminates version sync issues between `pyproject.toml` and source.

### Fixes

- Update CLI tests for removed venv validation guard.
- Close port-allocator and adapter-loader race windows.
- Deduplicate and isolate tarball fetch targets.
- Hold `_start_lock` across `AppiumProcessManager.stop()` body.
- Authenticate agent driver-pack tarball fetches.

## 0.1.0 — Initial Public Preview

- Initial public preview of the GridFleet host agent.
- FastAPI agent that runs on each device host, spawning Appium processes and Selenium Grid relay nodes.
- Driver-pack runtime with manifest-driven adapter loading and isolated APPIUM_HOME.
