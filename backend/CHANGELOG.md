# Changelog — GridFleet Backend

All notable changes to the GridFleet backend (FastAPI manager, control plane) are documented here.

## Unreleased

### Features

- Add `GRIDFLEET_AGENT_RECOMMENDED_VERSION` setting and expose `recommended_agent_version` / `agent_update_available` fields on the host API, enabling upgrade awareness for connected agents.
- Add configurable terminal WebSocket scheme (`GRIDFLEET_TERMINAL_WS_SCHEME`).

### Fixes

- Bracket-wrap IPv6 addresses in agent terminal URLs so `ws://[::1]:5100/...` is valid.
- Close drain-transition race by committing draining state before `try_complete_drain`, preventing concurrent `assert_runnable` from starting new work during a drain.

## [0.2.0](https://github.com/quidow/gridfleet/compare/gridfleet-backend-v0.1.0...gridfleet-backend-v0.2.0) (2026-05-15)


### ⚠ BREAKING CHANGES

* **agent:** extract per-domain routers from main.py (PR #1 of 3) ([#218](https://github.com/quidow/gridfleet/issues/218))
* **main:** require postgresql 18 baseline
* **backend:** remove host tool ensure jobs, endpoints, and settings
* **backend:** unify verification node lifecycle ([#187](https://github.com/quidow/gridfleet/issues/187))
* **backend:** appium desired-state phase 6 — final cleanup ([#179](https://github.com/quidow/gridfleet/issues/179))
* **backend:** Run claim retry and TTL settings are removed with the claim API.
* **backend:** Run claim, release, and release-with-cooldown endpoints are removed.
* **backend:** RunState.ready is removed. The ready endpoint now activates runs.
* **backend:** Reserved device payloads no longer include claim fields.
* **backend:** DeviceReservation claim columns are removed.
* **backend:** drop appium node state column ([#170](https://github.com/quidow/gridfleet/issues/170))
* **backend:** clients sending {drain: true|false} to /api/devices/ {id}/maintenance, /api/devices/bulk/enter-maintenance, or the group bulk equivalent must drop the field. The enter-maintenance behaviour is unchanged from drain=false (always stop the node).
* remove device_config secret masking ([#104](https://github.com/quidow/gridfleet/issues/104))
* **backend:** scope Appium port selection to the target host ([#102](https://github.com/quidow/gridfleet/issues/102))
* **backend:** split device availability_status into operational_state + hold ([#87](https://github.com/quidow/gridfleet/issues/87))
* **backend:** derive device health summary on read ([#78](https://github.com/quidow/gridfleet/issues/78))
* typed Appium resource claims + structured agent errors ([#77](https://github.com/quidow/gridfleet/issues/77))

### Features

* add reservation claim release api ([2390223](https://github.com/quidow/gridfleet/commit/239022398c2d9ab7c3f672987238dce6a8d7bef6))
* **agent:** close FastAPI best-practice gaps ([443372e](https://github.com/quidow/gridfleet/commit/443372ecf9defd976cc17f327cdf81a5d233c211))
* **agent:** enforce optional http basic auth on backend-&gt;agent calls ([#100](https://github.com/quidow/gridfleet/issues/100)) ([00f985e](https://github.com/quidow/gridfleet/commit/00f985e58391b356861a94147b89d502bd57df35))
* **agent:** expose registration state in /agent/health response ([4dc6ed3](https://github.com/quidow/gridfleet/commit/4dc6ed3463a5bdf2bb99b922010aa1c8f804f749))
* **agent:** extract per-domain routers from main.py (PR [#1](https://github.com/quidow/gridfleet/issues/1) of 3) ([#218](https://github.com/quidow/gridfleet/issues/218)) ([7d440bb](https://github.com/quidow/gridfleet/commit/7d440bb257c7af70009e7de1ddba2441dda9ae8c))
* **agent:** tighten response models with typed cores and named extras fields ([fd591e3](https://github.com/quidow/gridfleet/commit/fd591e3918f2a7e9f8f07daf012c823f5ab28745))
* **backend,testkit:** recreate run device cooldown api ([fccfbc7](https://github.com/quidow/gridfleet/commit/fccfbc7bcf694f8c59cbaa394bb075d20e1b34f0))
* **backend:** add /api/devices/{id}/test_data endpoints ([4e48f0c](https://github.com/quidow/gridfleet/commit/4e48f0c8fb5c6476544bf82e3580abbfd9d2746d))
* **backend:** add agent log ingest route ([c317b01](https://github.com/quidow/gridfleet/commit/c317b0173f5a4da5adf0fbdccd9f5c4e4e1246e9))
* **backend:** add app.security.dependencies with require_any_auth ([38cb66e](https://github.com/quidow/gridfleet/commit/38cb66e1bc6386360eacdbf09e0139069d4ffb7c))
* **backend:** add appium node desired-state schema ([#163](https://github.com/quidow/gridfleet/issues/163)) ([b64ee2e](https://github.com/quidow/gridfleet/commit/b64ee2e9616b95a6d334dd5bcddbaa2432a1763c))
* **backend:** add appium node grid run id columns ([5a6e75e](https://github.com/quidow/gridfleet/commit/5a6e75eea8b5ae5aa527608d419210176e431949))
* **backend:** add appium node grid run id fields ([ef6c627](https://github.com/quidow/gridfleet/commit/ef6c627273ed97b33661b6af43ef1b0daf9c8605))
* **backend:** add appium reconfigure client ([9d6ac7c](https://github.com/quidow/gridfleet/commit/9d6ac7c7b8331f6ce6aa628147c4601aa366409e))
* **backend:** add check_machine_credentials helper ([bdcdf4d](https://github.com/quidow/gridfleet/commit/bdcdf4deb604feaa3c5bf035ce09354fb1841b56))
* **backend:** add dated alembic filenames ([2de88fa](https://github.com/quidow/gridfleet/commit/2de88fac3d2b110efcde24fd9c30297624faf9ac))
* **backend:** add desired grid run id writer ([1990221](https://github.com/quidow/gridfleet/commit/1990221200b6ce7ee09ba7453eb0b202f17c0416))
* **backend:** add device intent registry schema ([9ca910f](https://github.com/quidow/gridfleet/commit/9ca910f3aa50e88a9fdeb9cf26d1d05e7b0fa55d))
* **backend:** add device lookup by connection target ([fc93a6e](https://github.com/quidow/gridfleet/commit/fc93a6e7a9a9d239917fa59589c8ec4a6f514055))
* **backend:** add device test_data audit log model ([f025bd5](https://github.com/quidow/gridfleet/commit/f025bd5371d37944a9d95ce2ad612f32aa4c2aa8))
* **backend:** add device.test_data column and audit log table ([df50cea](https://github.com/quidow/gridfleet/commit/df50cea9f4c6349ededfa91713e8af8aa18fd9ce))
* **backend:** add grid node reregister agent operation ([93c9736](https://github.com/quidow/gridfleet/commit/93c9736dd862230c5f55119710e7cc76315fb9ff))
* **backend:** add grid node run id reconciler ([17e6085](https://github.com/quidow/gridfleet/commit/17e608594bfcab7bde3d9ba5dfa7129998ee9cbf))
* **backend:** add host agent log entry model ([e130948](https://github.com/quidow/gridfleet/commit/e1309481c03c4ecf81ad7d1965f7b524f9096ba6))
* **backend:** add host agent log read route ([78ae75f](https://github.com/quidow/gridfleet/commit/78ae75fe99d248f4777fa635184219c7ce3d30c0))
* **backend:** add host events read route ([61e04d6](https://github.com/quidow/gridfleet/commit/61e04d6c7659ec50069e88a7194ac405e8d53569))
* **backend:** add host log schemas ([6262182](https://github.com/quidow/gridfleet/commit/6262182d4b09234548b1a2bd4fb1a89944cb3a66))
* **backend:** add hosts.* hardware metadata columns ([a52040d](https://github.com/quidow/gridfleet/commit/a52040d2975318e3f54e8c4ebc8424e801947111))
* **backend:** add intent registration service ([1d76b05](https://github.com/quidow/gridfleet/commit/1d76b05cde37e6405c0500613bf35f5b0c29b3b4))
* **backend:** add jittered retry-delay helper using tenacity ([332bde1](https://github.com/quidow/gridfleet/commit/332bde191c17f50f788467f1150b536ea9f5be20))
* **backend:** add metadata naming convention ([727b0d9](https://github.com/quidow/gridfleet/commit/727b0d9dee377f579db8984065b5b4f58ac9c910))
* **backend:** add migration for host agent logs ([0430568](https://github.com/quidow/gridfleet/commit/043056818f2a0dc063a7cc2039b99a984ca0bbbd))
* **backend:** add resolve_browser_session_from_token; route headers shim through it ([d54db84](https://github.com/quidow/gridfleet/commit/d54db84cacb040511dd3269f25ec287449c51bce))
* **backend:** add retryable-exception predicate for webhook dispatcher ([095ebed](https://github.com/quidow/gridfleet/commit/095ebedd26a9065a465ba8d6d3915515d7ba8640))
* **backend:** add settings registry keys for agent logs ([b4f0286](https://github.com/quidow/gridfleet/commit/b4f028672d35f4ad46bdca4681e80ad7c5fe2397))
* **backend:** add test_data payload schema with size cap ([aac4c7b](https://github.com/quidow/gridfleet/commit/aac4c7b0d250b803d231fcb5384cb72a72351eb2))
* **backend:** add test_data_service with audit log and event ([dcb8d94](https://github.com/quidow/gridfleet/commit/dcb8d94894ebc6a4429257767b70931c7f8046aa))
* **backend:** adopt PostgreSQL 18 primitives ([#206](https://github.com/quidow/gridfleet/issues/206)) ([46152a5](https://github.com/quidow/gridfleet/commit/46152a50c874c66784cf6ef9a930d7b4774977e2))
* **backend:** allow ?include=test_data on reserve and claim ([9105b5b](https://github.com/quidow/gridfleet/commit/9105b5bc6e1cf07c08c41a6012f479897daecdfd))
* **backend:** close fastapi best-practices gaps ([c47cd2e](https://github.com/quidow/gridfleet/commit/c47cd2ea6dca49cf0708153a865fea1f9ecaed18))
* **backend:** codegen Pydantic models from agent OpenAPI ([#235](https://github.com/quidow/gridfleet/issues/235)) ([8c70b5c](https://github.com/quidow/gridfleet/commit/8c70b5c1ca3baad63582e17030e54dbc76bc5503))
* **backend:** collapse run ready state ([0d91579](https://github.com/quidow/gridfleet/commit/0d915796c11cb31c1905e52b182b9f661dadd16a))
* **backend:** converge appium desired state via reconciler ([70bc7a8](https://github.com/quidow/gridfleet/commit/70bc7a8dc25ed57e2d4858ae88215ea0788152ab))
* **backend:** converge appium desired state via reconciler ([4ca1558](https://github.com/quidow/gridfleet/commit/4ca15584c0a62fd96bb7f732d90cab53f0ae1c66))
* **backend:** decompose runs service ([#242](https://github.com/quidow/gridfleet/issues/242)) ([b3f7e4b](https://github.com/quidow/gridfleet/commit/b3f7e4b49dadffc8903e8a2af177ef8b58e2b05c))
* **backend:** delete domain layout shims ([#234](https://github.com/quidow/gridfleet/issues/234)) ([a885a12](https://github.com/quidow/gridfleet/commit/a885a1272d6e54e931a83e354e1bce4dee784209))
* **backend:** delete legacy middleware-auth helpers from auth.py ([648ade3](https://github.com/quidow/gridfleet/commit/648ade3a5af6fbae7b34820e560f8c7d0905964d))
* **backend:** device state model drift fixes (D1-D6) ([#144](https://github.com/quidow/gridfleet/issues/144)) ([09556fd](https://github.com/quidow/gridfleet/commit/09556fdac8ddb458f1655f9001f25240443062fb))
* **backend:** document api error responses ([94e3a67](https://github.com/quidow/gridfleet/commit/94e3a675463f62ca4fda24196b569afac60b511f))
* **backend:** domain-layout refactor phases 0a + 0b + 1 (app/core/ + app/auth/) ([#217](https://github.com/quidow/gridfleet/issues/217)) ([93bf5c7](https://github.com/quidow/gridfleet/commit/93bf5c77cb2ff4ce271d16977e64d017c85e7dd0))
* **backend:** drop appium node state column ([#170](https://github.com/quidow/gridfleet/issues/170)) ([d0337d6](https://github.com/quidow/gridfleet/commit/d0337d6b616f4b9134c93cfc2841cc96ae61dfa2))
* **backend:** drop device reservation claim columns ([5e89e35](https://github.com/quidow/gridfleet/commit/5e89e35064a4d1307c668ec0dac766275eadf8e3))
* **backend:** drop device reservation claim fields ([6536918](https://github.com/quidow/gridfleet/commit/6536918b91c6c123df16ddd818f597dd3f07750e))
* **backend:** drop heavy Grid probe from node_health loop ([#249](https://github.com/quidow/gridfleet/issues/249)) ([c7bea0d](https://github.com/quidow/gridfleet/commit/c7bea0d2f399ea43024bffa9e613004df0583d96))
* **backend:** dual-write appium desired-state writers ([#164](https://github.com/quidow/gridfleet/issues/164)) ([160dc5a](https://github.com/quidow/gridfleet/commit/160dc5a2788ffe2ede98776924347b184d332bbe))
* **backend:** escalate device to maintenance after N cooldowns in same run ([#121](https://github.com/quidow/gridfleet/issues/121)) ([7fe01f7](https://github.com/quidow/gridfleet/commit/7fe01f768ff70cd3ddb7f26aec1ab7210b49987f))
* **backend:** evaluate device orchestration intents ([d2c6282](https://github.com/quidow/gridfleet/commit/d2c62827343f7757c51a72b0cd37703d83576785))
* **backend:** expose device tags as grid capabilities ([c5ab200](https://github.com/quidow/gridfleet/commit/c5ab2005bc337b5c84ba2dbf44641b968ffbcb1c))
* **backend:** expose orchestration intent state ([5fcdb2a](https://github.com/quidow/gridfleet/commit/5fcdb2a25b930ef1c674895a35b53ab665b24e0a))
* **backend:** fence leader-owned writes against stale leadership ([#105](https://github.com/quidow/gridfleet/issues/105)) ([cca5c17](https://github.com/quidow/gridfleet/commit/cca5c175bb2a6a667af7d1c764aa5c547cae2b65))
* **backend:** gate /docs, /redoc, /metrics, /openapi.json via static paths middleware ([80e2e7a](https://github.com/quidow/gridfleet/commit/80e2e7af44d903a25c983a1bf70672fb8686c316))
* **backend:** gate all protected routers via depends(require_any_auth) ([81a6fc6](https://github.com/quidow/gridfleet/commit/81a6fc632cba1907b1048e97246b7f7139d635ad))
* **backend:** gate docs by environment ([473a411](https://github.com/quidow/gridfleet/commit/473a411241ebd4be0b701c5207f602a4ec83f91d))
* **backend:** heartbeat diagnostic + resilience phase A ([#135](https://github.com/quidow/gridfleet/issues/135)) ([4c3a341](https://github.com/quidow/gridfleet/commit/4c3a341f3f8eddf24e92574b456bf7812bdd1d1f))
* **backend:** introduce DeviceStateMachine for operational/hold transitions ([#152](https://github.com/quidow/gridfleet/issues/152)) ([4cee1a5](https://github.com/quidow/gridfleet/commit/4cee1a5a89861c00eff830142ac8ae89f54639cd))
* **backend:** inverted router-gate audit test ([2f7c349](https://github.com/quidow/gridfleet/commit/2f7c3496cbeabd269345b65f3e7bc4944dbdbc25))
* **backend:** leader-failover latency via heartbeat and watcher ([#91](https://github.com/quidow/gridfleet/issues/91)) ([bf2ea4a](https://github.com/quidow/gridfleet/commit/bf2ea4a3e69912eeee046086af834248bac49922))
* **backend:** map device.test_data column on device model ([5b85f59](https://github.com/quidow/gridfleet/commit/5b85f59a080da633bc755a987719d59a432f9a31))
* **backend:** migrate background orchestration to intents ([09581e3](https://github.com/quidow/gridfleet/commit/09581e3fa35e5e77e6542db30d44aa824f25e56f))
* **backend:** migrate operator orchestration to intents ([c15ef0a](https://github.com/quidow/gridfleet/commit/c15ef0adfe8a778f29779c7d4fab2e3fc341bb6b))
* **backend:** migrate run orchestration to intents ([78b5148](https://github.com/quidow/gridfleet/commit/78b51489b8faf91de8686744abb7f096f3eaa47d))
* **backend:** move additional areas into domain packages ([#221](https://github.com/quidow/gridfleet/issues/221)) ([2f7d425](https://github.com/quidow/gridfleet/commit/2f7d42520f0b2a484124fc8a72c2a3b85b77725e))
* **backend:** move agent communication into domain package ([#222](https://github.com/quidow/gridfleet/issues/222)) ([0846ff3](https://github.com/quidow/gridfleet/commit/0846ff3f1b7c5d143eedda30b03ba9558662e90c))
* **backend:** move appium nodes into domain package ([#226](https://github.com/quidow/gridfleet/issues/226)) ([6c4aa41](https://github.com/quidow/gridfleet/commit/6c4aa41fd6c831ac1eb5688bbd39470f99f2d8a2))
* **backend:** move devices into domain package ([#229](https://github.com/quidow/gridfleet/issues/229)) ([2fc52e7](https://github.com/quidow/gridfleet/commit/2fc52e7b99a5865b7d7194fcab2c4c4142ef2dd7))
* **backend:** move hosts into domain package ([#223](https://github.com/quidow/gridfleet/issues/223)) ([9bd3d65](https://github.com/quidow/gridfleet/commit/9bd3d654f964cbb16364df711fa8f93843a23f5c))
* **backend:** move leader subsystem into core ([#236](https://github.com/quidow/gridfleet/issues/236)) ([49d4461](https://github.com/quidow/gridfleet/commit/49d446154d76e7ef6d1fa3b6341a641259fee278))
* **backend:** move packs into domain package ([#225](https://github.com/quidow/gridfleet/issues/225)) ([49ea136](https://github.com/quidow/gridfleet/commit/49ea1364165464dd3738db127a8e4fd8acab3ba0))
* **backend:** move runs into domain package ([#233](https://github.com/quidow/gridfleet/issues/233)) ([b1e3bfe](https://github.com/quidow/gridfleet/commit/b1e3bfeced4c593ca95c43951b643b3eae3535b4))
* **backend:** move sessions into domain package ([#231](https://github.com/quidow/gridfleet/issues/231)) ([54e7622](https://github.com/quidow/gridfleet/commit/54e762289055f1f73a5f284f23ea82bfb934935d))
* **backend:** orphan-reaping appium reconciler (phase 1) ([#161](https://github.com/quidow/gridfleet/issues/161)) ([c1659a5](https://github.com/quidow/gridfleet/commit/c1659a53089c05ab1454b30897de4f58e63592ad))
* **backend:** per-host httpx pool for backend→agent calls ([#89](https://github.com/quidow/gridfleet/issues/89)) ([6a1ed6e](https://github.com/quidow/gridfleet/commit/6a1ed6e6000f4a5293f2ed74af5ef378c678cc1d))
* **backend:** persist host hardware metadata from registration payload ([3b29b2c](https://github.com/quidow/gridfleet/commit/3b29b2c7acd5439179f6beca5238c5389b2fc57e))
* **backend:** prune stored agent logs in cleanup loop ([491bc90](https://github.com/quidow/gridfleet/commit/491bc90504be378380fafde3e4cb885ee045a83e))
* **backend:** query agent logs with filters ([14794f0](https://github.com/quidow/gridfleet/commit/14794f077e73ee0596af7bda0b9d8c7f91728a50))
* **backend:** query host-scoped events ([9b3cbc5](https://github.com/quidow/gridfleet/commit/9b3cbc58d139de086860041ee94bae347054abdd))
* **backend:** reconcile device orchestration intents ([354f647](https://github.com/quidow/gridfleet/commit/354f647aa544556e5e9e349d10c64e86f364c9c0))
* **backend:** recreate run device cooldown endpoint ([39b7d91](https://github.com/quidow/gridfleet/commit/39b7d9139c57ac492f28389b820f006d4492b708))
* **backend:** register test_data.updated public event ([0da64fe](https://github.com/quidow/gridfleet/commit/0da64fe526b6527605d611da879ed5004a32be2e))
* **backend:** remove auth enforcement from requestcontextmiddleware ([b7c4c7f](https://github.com/quidow/gridfleet/commit/b7c4c7fe026ca6ad5a5c464767e19633902e7430))
* **backend:** remove host tool ensure jobs, endpoints, and settings ([75b2051](https://github.com/quidow/gridfleet/commit/75b2051e5bfc81f381f8e45895625efdc273791b))
* **backend:** remove obsolete run claim settings ([5910221](https://github.com/quidow/gridfleet/commit/59102215a1c1b248e618dbd936c0b426e5243f73))
* **backend:** remove run claim and release api ([0d6a5a7](https://github.com/quidow/gridfleet/commit/0d6a5a72620c283d40a25a60f2b65de94f661c1d))
* **backend:** richer allocation payload with include=config,capabilities ([#94](https://github.com/quidow/gridfleet/issues/94)) ([4b44bad](https://github.com/quidow/gridfleet/commit/4b44badb15bb2d679202f006a5272c56d7d186f2))
* **backend:** run grid node run id reconciler loop ([d56730e](https://github.com/quidow/gridfleet/commit/d56730ed4a010daed0d08ef5f37457a1c4e55d40))
* **backend:** scope Appium port selection to the target host ([#102](https://github.com/quidow/gridfleet/issues/102)) ([5be5562](https://github.com/quidow/gridfleet/commit/5be5562d01731dad5c6d3d9aa0adb9b875a9c85b))
* **backend:** send orchestration metadata in appium starts ([7eaff4f](https://github.com/quidow/gridfleet/commit/7eaff4f10844a836bafa9288c1b365531454d1fa))
* **backend:** sweep device test_data audit log in data_cleanup ([03e49fa](https://github.com/quidow/gridfleet/commit/03e49fa71e7a7f037be5cec58bf4b4a9579aaa61))
* **backend:** switch session tokens to jwt hs256 via pyjwt ([71b223d](https://github.com/quidow/gridfleet/commit/71b223df997b913590d7679405a42e3123e00693))
* **backend:** test coverage for require_any_auth dispatch matrix ([8067f2e](https://github.com/quidow/gridfleet/commit/8067f2ece7826b8a0134e1857af22ba0b98f2d5c))
* **backend:** unify verification node lifecycle ([#187](https://github.com/quidow/gridfleet/issues/187)) ([1d1e7d8](https://github.com/quidow/gridfleet/commit/1d1e7d8b3216f3244a3ab6b40f5d324d561c0f41))
* **backend:** write agent log batches with deduplication ([04118c2](https://github.com/quidow/gridfleet/commit/04118c24d0ebd6965f47ba4eb9baf440b2c11051))
* **backend:** write desired grid run id during run flow ([e627175](https://github.com/quidow/gridfleet/commit/e627175b40f993ec592d37579b12b56d015f9a23))
* **frontend:** add host logs tab ([f21de6a](https://github.com/quidow/gridfleet/commit/f21de6af1964224f6f4bac132297829e3a5e9426))
* **frontend:** derive types from backend openapi schema ([#162](https://github.com/quidow/gridfleet/issues/162)) ([80be9a7](https://github.com/quidow/gridfleet/commit/80be9a7f272c311287fd6537d422e50a306baa0b))
* **main:** add device orchestration intent registry ([b42b3d4](https://github.com/quidow/gridfleet/commit/b42b3d47e96e1ee1257bdd6f7676f027eed6de57))
* **main:** add optional icmp ping health check for usb devices with saved ip ([#143](https://github.com/quidow/gridfleet/issues/143)) ([afda5ce](https://github.com/quidow/gridfleet/commit/afda5ce5527167bcd47cb04f227a791ab3cdea1b))
* **main:** require postgresql 18 baseline ([d43fd18](https://github.com/quidow/gridfleet/commit/d43fd18f70a4732b5237e9dfadff02f2dc76f6b3))
* **main:** split device test_data from device_config + modal portal ([b5d0fa0](https://github.com/quidow/gridfleet/commit/b5d0fa09a862af742b3a2462667a86b1d3a867b6))
* remove host tool ensure/version management ([#190](https://github.com/quidow/gridfleet/issues/190)) ([b2562c1](https://github.com/quidow/gridfleet/commit/b2562c16d75ef14c0f4c9131c03151b73f337802))
* show probe sessions on Sessions page (opt-in, no analytics impact) ([#246](https://github.com/quidow/gridfleet/issues/246)) ([6e2db59](https://github.com/quidow/gridfleet/commit/6e2db595f42f361b0e3d78d83bf7ff15203c6397))
* surface host hardware metadata on host detail ([de6116e](https://github.com/quidow/gridfleet/commit/de6116e5155d80eee9e868cbc69c56cdfa027afa))
* **testkit:** add run-scoped device cooldowns ([#54](https://github.com/quidow/gridfleet/issues/54)) ([6163dc9](https://github.com/quidow/gridfleet/commit/6163dc959334e933b43c20a99ad4edcbdae6c98b))
* **testkit:** support tag-based device targeting ([db0d0e3](https://github.com/quidow/gridfleet/commit/db0d0e3d3d1231828bb22a707d3bdcab6c0ec717))


### Bug Fixes

* **agent:** keep adapter-fed pack responses permissive ([954a31a](https://github.com/quidow/gridfleet/commit/954a31a8b9c4a8a1e5d03188be44713d576e5a5d))
* **agent:** release adapter-owned doctor refactor ([#165](https://github.com/quidow/gridfleet/issues/165)) ([f3ae257](https://github.com/quidow/gridfleet/commit/f3ae25787e2c8ef926312f11d2313c6513f8bfa9))
* **agent:** trigger release for port conflict cleanup ([6a561ca](https://github.com/quidow/gridfleet/commit/6a561ca480c62b9abb2d5141fa98fc4e1a7696b6))
* **backend,agent:** close 52 codeql alerts ([#115](https://github.com/quidow/gridfleet/issues/115)) ([05190ac](https://github.com/quidow/gridfleet/commit/05190ac32e7be9c2b979513114230f51705a0422))
* **backend:** add lifecycle_run_cooldown_set to deviceeventtype enum ([#57](https://github.com/quidow/gridfleet/issues/57)) ([43e0fda](https://github.com/quidow/gridfleet/commit/43e0fdaf6ec07663fc7600916bb8cb97b62231fb))
* **backend:** address cooldown review findings ([c5745ee](https://github.com/quidow/gridfleet/commit/c5745ee310a96807aee70e8b41f56b475cf44314))
* **backend:** avoid password taint in session tokens ([#120](https://github.com/quidow/gridfleet/issues/120)) ([dbbb250](https://github.com/quidow/gridfleet/commit/dbbb250ffd215552ae1673600664d316e5246962))
* **backend:** backfill active run start during session sync ([5179d32](https://github.com/quidow/gridfleet/commit/5179d3214c61504875b1e529f0bd401644a6b2f2))
* **backend:** bound agent reconfigure outbox retries ([ce2bbdc](https://github.com/quidow/gridfleet/commit/ce2bbdcbb7daeb5333215d9d91ed34c050d76c55))
* **backend:** broaden jwt decode exception catch to cover misconfig errors ([ccde255](https://github.com/quidow/gridfleet/commit/ccde2551872d7c9ab4f474b966576cec077aa4ba))
* **backend:** bump openapi route surface baseline for host log routes ([f76930a](https://github.com/quidow/gridfleet/commit/f76930ab2c6b350fabf9673b943b33edc1f913b0))
* **backend:** chain agent-log migration after host-hardware migration ([a73d0d8](https://github.com/quidow/gridfleet/commit/a73d0d85c34e0eefff6a7f0a0f66f60d8de3d125))
* **backend:** clamp test_data history limit and emit cleanup count ([35516d1](https://github.com/quidow/gridfleet/commit/35516d1f1ddf25fcd6870482b2d6988512e51105))
* **backend:** clear CodeQL warnings in services and tests ([39f2e19](https://github.com/quidow/gridfleet/commit/39f2e195777f29107fafae3cbdff253ea555e7cb))
* **backend:** clear lifecycle suppression on manual node restart ([#159](https://github.com/quidow/gridfleet/issues/159)) ([ef72f0d](https://github.com/quidow/gridfleet/commit/ef72f0d8f5baff95739da8e9be111a51603be6a2))
* **backend:** clear maintenance-driven recovery suppression on exit ([#141](https://github.com/quidow/gridfleet/issues/141)) ([ffe98a1](https://github.com/quidow/gridfleet/commit/ffe98a1fbfbea80f268d366bcf0d208928f377ce))
* **backend:** clear reconciler start-failure flag on observed-running convergence ([2a19e2c](https://github.com/quidow/gridfleet/commit/2a19e2c8c42e4fc822885c97149978c4bb55b199))
* **backend:** clear stale reconciler start-failure metadata on node start ([#207](https://github.com/quidow/gridfleet/issues/207)) ([226762c](https://github.com/quidow/gridfleet/commit/226762c721a1ba45b64679a32f298d0b125a724e))
* **backend:** clear stale stop_pending on every session-end path and recover healthy devices ([#71](https://github.com/quidow/gridfleet/issues/71)) ([0dc6ed5](https://github.com/quidow/gridfleet/commit/0dc6ed583c6eec872574de3f06546221eb8b26bd))
* **backend:** close 9 device-state race conditions via row locks ([#14](https://github.com/quidow/gridfleet/issues/14)) ([5c41c6c](https://github.com/quidow/gridfleet/commit/5c41c6cbc1f0db023f9fe4483b5f1d19c1d51419))
* **backend:** close appium reconciler edge cases ([af5ae67](https://github.com/quidow/gridfleet/commit/af5ae6791d8ae25941eaabc722ce94c2d63833a2))
* **backend:** close appium reconciler review gaps ([6ef4f4b](https://github.com/quidow/gridfleet/commit/6ef4f4b631d785b9d9e443de78a6b839d9929882))
* **backend:** close AppiumNode row-write race family ([#22](https://github.com/quidow/gridfleet/issues/22)) ([0fdb551](https://github.com/quidow/gridfleet/commit/0fdb5511fb44d9883b5f6a12e6e1bbe50ed3b577))
* **backend:** close webhook dispatcher coverage gaps ([44c804d](https://github.com/quidow/gridfleet/commit/44c804d0b6e1eede4f1855ca2b03f5f4601c87a4))
* **backend:** converge node restart immediately ([#188](https://github.com/quidow/gridfleet/issues/188)) ([33f9fef](https://github.com/quidow/gridfleet/commit/33f9fefb3df204dc4bbfeb3b3d336cf8f9ecea33))
* **backend:** cover webhook delivery success, 4xx terminal, 5xx jittered retry, timeout, invalid url ([bcbcdea](https://github.com/quidow/gridfleet/commit/bcbcdea20fc4e7aebe557c4d9a041914da9f3546))
* **backend:** de-flake ci tests and silence suite warnings ([#160](https://github.com/quidow/gridfleet/issues/160)) ([15e7f6b](https://github.com/quidow/gridfleet/commit/15e7f6b7150f6f5ea09a2b26ee76900564c08080))
* **backend:** defer availability flip until retained-node restart succeeds ([#15](https://github.com/quidow/gridfleet/issues/15)) ([d7ca20e](https://github.com/quidow/gridfleet/commit/d7ca20e9a818eb15043d7202e0b1974f38d839d1))
* **backend:** do not clear desired_grid_run_id on cooldown ([bba2f8d](https://github.com/quidow/gridfleet/commit/bba2f8d62e14eb62e8112e533c030cf4b7ed5f07))
* **backend:** do not restart node on cooldown expiry when auto_manage is off ([56f38bc](https://github.com/quidow/gridfleet/commit/56f38bc8c42f49e913791a8b48d3d1ac4c5839b3))
* **backend:** do not restart node on cooldown expiry when device is in maintenance ([3ce4c6c](https://github.com/quidow/gridfleet/commit/3ce4c6c5bc9f007201179d603ec5f269c308caba))
* **backend:** drain control-plane services before DROP SCHEMA in tests ([0d14676](https://github.com/quidow/gridfleet/commit/0d1467681a30bb268ee44fc9ebde99496823dcdd))
* **backend:** eliminate breaker false-positives on slow pack adapter endpoints ([#139](https://github.com/quidow/gridfleet/issues/139)) ([c4ea895](https://github.com/quidow/gridfleet/commit/c4ea89572c572f980c7cef019f5c96081d7a35a0))
* **backend:** enforce 32-byte min on auth_session_secret + pad test keys ([#232](https://github.com/quidow/gridfleet/issues/232)) ([ec957cb](https://github.com/quidow/gridfleet/commit/ec957cb892939e4d60ebd626bfdd997c852149ac))
* **backend:** expose device identity conflict error ([462a35b](https://github.com/quidow/gridfleet/commit/462a35b7d8a4d47d043596aa928d0b65d950887a))
* **backend:** filter legacy appium host capability ([625b57c](https://github.com/quidow/gridfleet/commit/625b57cee19dd262a51ca9c75b8da61818a449ed))
* **backend:** fix missing type args on probe handler return type ([9c3dde8](https://github.com/quidow/gridfleet/commit/9c3dde80135909214d937f06d2ebdc8c9b11add8))
* **backend:** gate /docs/* prefix and wrap static auth 401 in request context ([7bf4c65](https://github.com/quidow/gridfleet/commit/7bf4c653248abd878626341ab3065bf867b57f21))
* **backend:** gate ready_operational_state on combined health ([#137](https://github.com/quidow/gridfleet/issues/137)) ([cb90464](https://github.com/quidow/gridfleet/commit/cb9046433374565ef806e50377fea3ba9fb5af30))
* **backend:** guard auto-recovery against active cooldowns ([6298517](https://github.com/quidow/gridfleet/commit/6298517139696cc945da44fbd5e3753ff399e78f))
* **backend:** guard host hardware setattr against schema drift ([b67f5bf](https://github.com/quidow/gridfleet/commit/b67f5bf84cf870ee0c38d3c2011e36dffc65a01e))
* **backend:** harden appium reconciler convergence ([27a3d56](https://github.com/quidow/gridfleet/commit/27a3d564f294bb1cb1acbba7b94ffb7995042203))
* **backend:** harden intent and reconfigure edge cases ([5c3bb1a](https://github.com/quidow/gridfleet/commit/5c3bb1a1e1714e6a4d5b1d19c5828240de879234))
* **backend:** harden intent reconciliation semantics ([c6da63c](https://github.com/quidow/gridfleet/commit/c6da63c25bdfb022a753cf93ce99294781d7ce0d))
* **backend:** hold device row lock inside mark_node_started / mark_node_stopped ([#18](https://github.com/quidow/gridfleet/issues/18)) ([78579f6](https://github.com/quidow/gridfleet/commit/78579f687004755d1269d084fc4151182cb0de60))
* **backend:** honor node grid url for probes ([99762a3](https://github.com/quidow/gridfleet/commit/99762a3fdc10fe25fd41623af682872c43772190))
* **backend:** lock device row before availability writes in update_device + retain_verified_node ([#17](https://github.com/quidow/gridfleet/issues/17)) ([cf4f5d7](https://github.com/quidow/gridfleet/commit/cf4f5d7007afef65de458b804f4ed00036a60821))
* **backend:** lock device rows before offline write in heartbeat _check_hosts ([#26](https://github.com/quidow/gridfleet/issues/26)) ([5c7eea5](https://github.com/quidow/gridfleet/commit/5c7eea54213d31aa890f41dbb4dd607613e96747))
* **backend:** make naming-convention baseline idempotent ([e8fa067](https://github.com/quidow/gridfleet/commit/e8fa067efccb40da23c247589ebe50aaa0d8b4bb))
* **backend:** mark device busy on claim to close chip race ([#138](https://github.com/quidow/gridfleet/issues/138)) ([7b2f966](https://github.com/quidow/gridfleet/commit/7b2f966f4ce6ba5359565acc121a60272a49abcf))
* **backend:** narrow router excepts; adopt annotated depends form ([#215](https://github.com/quidow/gridfleet/issues/215)) ([8034f75](https://github.com/quidow/gridfleet/commit/8034f75f186754dc56fb11937c5b3d881501d764))
* **backend:** narrow terminal proxy errors ([8874ab5](https://github.com/quidow/gridfleet/commit/8874ab53104e6e18f52d77a5339adfc050ad7fe6))
* **backend:** node state split-brain on operator stop ([#63](https://github.com/quidow/gridfleet/issues/63)) ([0de7a88](https://github.com/quidow/gridfleet/commit/0de7a887d1882b3851bfe3d9db6042c9fcf585db))
* **backend:** preserve desired_port when restarting node after cooldown expiry ([d002123](https://github.com/quidow/gridfleet/commit/d002123b247b4aa7be4d1f70a6f1015294a4ad63))
* **backend:** prevent backend test hangs ([6bd0760](https://github.com/quidow/gridfleet/commit/6bd0760125e12a299ea21e3401c8c3f8b10e6293))
* **backend:** reconcile doctor rows per posted pack, not per posted doctor entry ([#167](https://github.com/quidow/gridfleet/issues/167)) ([d724d86](https://github.com/quidow/gridfleet/commit/d724d86cf1f3c4e34a02fdfa9df2030c2caab745))
* **backend:** refresh pooled agent http client on tuning setting change ([#90](https://github.com/quidow/gridfleet/issues/90)) ([82924e9](https://github.com/quidow/gridfleet/commit/82924e9927437431e9b46214baa9b5443f4123ea))
* **backend:** remove require_any_auth from host_terminal websocket router ([ff37055](https://github.com/quidow/gridfleet/commit/ff3705500943ab3dc981b98516d914db057ee407))
* **backend:** repair CI failures from PR [#243](https://github.com/quidow/gridfleet/issues/243) ([63a7299](https://github.com/quidow/gridfleet/commit/63a72991d5929f28b8ebf5843f3ccaebfc9f260c))
* **backend:** reset heartbeat resume-guard state in offline-cascade test ([70dcbb5](https://github.com/quidow/gridfleet/commit/70dcbb52f020c1cb0e308131832592c4b53bf887))
* **backend:** resolve device recovery deadlock after session viability failure ([#210](https://github.com/quidow/gridfleet/issues/210)) ([2db1f42](https://github.com/quidow/gridfleet/commit/2db1f42fdf9509ac8ac763cc219f6e28b0b87fa9))
* **backend:** retry pending agent reconfigures ([5aec469](https://github.com/quidow/gridfleet/commit/5aec4694de399871f89f09c675c77135280e02e2))
* **backend:** return raw response payloads from routers ([733d0e5](https://github.com/quidow/gridfleet/commit/733d0e5d6ea81aa6f4a92b4c3d44278309515653))
* **backend:** route probe sessions through grid ([f45ed24](https://github.com/quidow/gridfleet/commit/f45ed242aab2867ff79635b87e51522a9228c87c))
* **backend:** route verification_passed to available, not offline ([#189](https://github.com/quidow/gridfleet/issues/189)) ([a0ddd6a](https://github.com/quidow/gridfleet/commit/a0ddd6a431d88de3ee0cc3d8d122a89cda528757))
* **backend:** share httpx async client in grid_service and session_viability ([#149](https://github.com/quidow/gridfleet/issues/149)) ([d9d9287](https://github.com/quidow/gridfleet/commit/d9d928732882f9640e8a059d339090e4c2c0bbe2))
* **backend:** skip released reservations in cooldown expiry scan ([fbf2133](https://github.com/quidow/gridfleet/commit/fbf21332f11cea3e466e61221a07f998e1b986da))
* **backend:** split retryable vs terminal webhook failures, add jitter ([31cdaf0](https://github.com/quidow/gridfleet/commit/31cdaf0f05f2804d9472974fd3e96b86737b70ad))
* **backend:** stop appium node during cooldown and restore after ttl expiry ([29cdc23](https://github.com/quidow/gridfleet/commit/29cdc230dcc1be4ae10f482d967deb8fe56449b4))
* **backend:** stop auto-escalating to maintenance on health/connectivity failures ([#142](https://github.com/quidow/gridfleet/issues/142)) ([9ca4a90](https://github.com/quidow/gridfleet/commit/9ca4a903f4ecbabc499c9f5b15c06b7942b970d3))
* **backend:** stop node atomically during cooldown under run lock ([2ea4eaa](https://github.com/quidow/gridfleet/commit/2ea4eaa5ac6f56f7cd74162769768c82be832053))
* **backend:** stop transient agent blips from flapping device health ([#61](https://github.com/quidow/gridfleet/issues/61)) ([a58c8e5](https://github.com/quidow/gridfleet/commit/a58c8e5e835b72f5abde69bd078b2868c7cc84d5))
* **backend:** sync grid run id on lifecycle exclusion ([7e3ac44](https://github.com/quidow/gridfleet/commit/7e3ac44715f0d1466db0d917f020685f57521b24))
* **backend:** tighten host agent log surface from PR review ([5d32557](https://github.com/quidow/gridfleet/commit/5d3255739e939bc7d7e91bf0036a0d823ffdc12a))
* **backend:** type session viability checked_by via shared enum ([63ba5dc](https://github.com/quidow/gridfleet/commit/63ba5dc4d5da0b88f4b08c5cb4c608939b960944))
* **backend:** unblock session_sync on duplicate running rows and restore busy on stale claim sweep ([#140](https://github.com/quidow/gridfleet/issues/140)) ([b066ec1](https://github.com/quidow/gridfleet/commit/b066ec11b733b91b623d16b0494004d52ffd845a))
* **backend:** update host capability normalizer test ([681377f](https://github.com/quidow/gridfleet/commit/681377f8c540de33f9e77f94a0fd9c245f96d9da))
* **backend:** update require_admin docstring to reference require_any_auth ([13d8d12](https://github.com/quidow/gridfleet/commit/13d8d12d57bb3a18268b9f68ef4323a8832958c9))
* **backend:** update session viability probe fake ([61e10a8](https://github.com/quidow/gridfleet/commit/61e10a8ab3618e5f264dfc5cfb01da402e41d2ea))
* **backend:** use starlette cookie_parser in _read_cookie ([64692e5](https://github.com/quidow/gridfleet/commit/64692e5068388020d801894e476d5047128e863e))
* **backend:** wrap test_data hydration in try/except for parity with config ([1347468](https://github.com/quidow/gridfleet/commit/134746823815c4bf44a03ab2860cf073f5267f02))
* **docker:** let selenium hub resolve host gateway ([#136](https://github.com/quidow/gridfleet/issues/136)) ([fed0cfd](https://github.com/quidow/gridfleet/commit/fed0cfd665b3762f9b70652e256c261d87a62851))
* **frontend,backend:** show busy chip when reserved device runs session ([#134](https://github.com/quidow/gridfleet/issues/134)) ([04c62f4](https://github.com/quidow/gridfleet/commit/04c62f44287c321b6058212107cbefd73a473497))
* **frontend:** update verification stage label from 'start temporary node' to 'start appium node' ([a0ddd6a](https://github.com/quidow/gridfleet/commit/a0ddd6a431d88de3ee0cc3d8d122a89cda528757))
* idempotent device release after lifecycle cleanup ([#12](https://github.com/quidow/gridfleet/issues/12)) ([7a98a5d](https://github.com/quidow/gridfleet/commit/7a98a5d18330150aab0a852f6b894d1d53de257c))
* **main:** route probe sessions through grid ([#211](https://github.com/quidow/gridfleet/issues/211)) ([5f7ef90](https://github.com/quidow/gridfleet/commit/5f7ef9036492949ba0dfb756ae5c84a3f3a9bb8a))
* **main:** satisfy intent registry verification ([5b0a097](https://github.com/quidow/gridfleet/commit/5b0a097788e2cd128d0e9d5721fe12602785b4bb))
* preserve masked device config secrets ([bc5aead](https://github.com/quidow/gridfleet/commit/bc5aeadbc7583cc877a951deac70a2526a8306e7))
* restore busy devices on force release ([9b8c2a6](https://github.com/quidow/gridfleet/commit/9b8c2a6bfc67731e5e93a4f9315bae81fe6f7394))
* stabilize device verification lifecycle ([a95df85](https://github.com/quidow/gridfleet/commit/a95df857aeb53e8efa627a250d4539f3a554dab0))


### Performance Improvements

* **backend:** cut 132s from test suite by skipping 60s recovery wait ([769c313](https://github.com/quidow/gridfleet/commit/769c3130d23dcbdd9fc783050e34ddb3c19ef784))
* **backend:** offload pack archive work ([dd123ea](https://github.com/quidow/gridfleet/commit/dd123ea2d3a417a9307f33a490edc7791a3ea69c))


### Dependencies

* **backend:** add pyjwt for session token format ([3f9dedb](https://github.com/quidow/gridfleet/commit/3f9dedb453ebdfccf53686d54ef2a276d8e42fc4))
* **backend:** add tenacity for jittered retry math ([f119272](https://github.com/quidow/gridfleet/commit/f11927265d926012419340c496997dae44d8d680))
* **deps-dev:** bump types-pyyaml in /backend ([#125](https://github.com/quidow/gridfleet/issues/125)) ([db2bd2e](https://github.com/quidow/gridfleet/commit/db2bd2ed76094fd474fdd6333114a3c4858dd750))
* **deps-dev:** bump types-pyyaml in /backend ([#185](https://github.com/quidow/gridfleet/issues/185)) ([d3051e9](https://github.com/quidow/gridfleet/commit/d3051e95245c1393a50ea2821f594f3143e6cff4))
* **deps:** bump mako from 1.3.11 to 1.3.12 in /backend ([03c634a](https://github.com/quidow/gridfleet/commit/03c634ad98cb606edf179746b6a360ab3b374503))
* **deps:** bump mypy in /agent ([#195](https://github.com/quidow/gridfleet/issues/195)) ([1317e59](https://github.com/quidow/gridfleet/commit/1317e59bbd4ae6969ed3c717c24b43dbfefec722))
* **deps:** bump pydantic-settings in /agent ([#182](https://github.com/quidow/gridfleet/issues/182)) ([a6abb83](https://github.com/quidow/gridfleet/commit/a6abb83c6367703ca8acc8c0008a2762d0dcc958))
* **deps:** bump python-multipart in /backend ([#184](https://github.com/quidow/gridfleet/issues/184)) ([93a4963](https://github.com/quidow/gridfleet/commit/93a4963d02a74b0be6eab207a14f33ad54246476))
* **deps:** bump sse-starlette in /backend ([7f815c7](https://github.com/quidow/gridfleet/commit/7f815c7482354d1d4487ec241db02f0c56cb4f00))
* **deps:** bump sse-starlette in /backend ([#196](https://github.com/quidow/gridfleet/issues/196)) ([c0eef40](https://github.com/quidow/gridfleet/commit/c0eef40a3fc44b3d3d322a1ec7a022ebc3ddf56c))
* **deps:** bump sse-starlette in /backend ([#204](https://github.com/quidow/gridfleet/issues/204)) ([f3fc52b](https://github.com/quidow/gridfleet/commit/f3fc52bef66fefaa7362047210300f4b68843823))


### Documentation

* **docs:** document discriminated-union release-with-cooldown response ([7fe01f7](https://github.com/quidow/gridfleet/commit/7fe01f768ff70cd3ddb7f26aec1ab7210b49987f))


### Code Refactoring

* **backend:** appium desired-state phase 6 — final cleanup ([#179](https://github.com/quidow/gridfleet/issues/179)) ([c97ae99](https://github.com/quidow/gridfleet/commit/c97ae99974024e036a2fd7d2233442f70ff18fcb))
* **backend:** derive device health summary on read ([#78](https://github.com/quidow/gridfleet/issues/78)) ([10078ef](https://github.com/quidow/gridfleet/commit/10078ef89dcf12e855776a68002456302c51684c))
* **backend:** split device availability_status into operational_state + hold ([#87](https://github.com/quidow/gridfleet/issues/87)) ([1b329d3](https://github.com/quidow/gridfleet/commit/1b329d39c77c3a8594c8158a1a29ab8bd257a124))
* remove device_config secret masking ([#104](https://github.com/quidow/gridfleet/issues/104)) ([7329a31](https://github.com/quidow/gridfleet/commit/7329a3107814f653b81b2753e519e271ec0dd8bd))
* typed Appium resource claims + structured agent errors ([#77](https://github.com/quidow/gridfleet/issues/77)) ([9bfbc30](https://github.com/quidow/gridfleet/commit/9bfbc300df5fe779f91ba0ba00cc3b8fa2a589e9))

## 0.1.0 — Initial Public Preview

- Initial public preview baseline for the GridFleet control plane backend.
- FastAPI manager with async SQLAlchemy + Postgres, Alembic migrations, and leader-owned background loops.
- Hardened production compose defaults around authentication and host approval.
- Added CI, security scanning, and dependency update workflows.
