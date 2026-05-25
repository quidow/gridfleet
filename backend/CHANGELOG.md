# Changelog — GridFleet Backend

All notable changes to the GridFleet backend (FastAPI manager, control plane) are documented here.

## Unreleased

### Features

- Add `GRIDFLEET_AGENT_RECOMMENDED_VERSION` setting and expose `recommended_agent_version` / `agent_update_available` fields on the host API, enabling upgrade awareness for connected agents.
- Add configurable terminal WebSocket scheme (`GRIDFLEET_TERMINAL_WS_SCHEME`).

### Fixes

- Bracket-wrap IPv6 addresses in agent terminal URLs so `ws://[::1]:5100/...` is valid.
- Close drain-transition race by committing draining state before `try_complete_drain`, preventing concurrent `assert_runnable` from starting new work during a drain.

## [0.2.0](https://github.com/quidow/gridfleet/compare/gridfleet-backend-v0.1.0...gridfleet-backend-v0.2.0) (2026-05-25)


### ⚠ BREAKING CHANGES

* **backend:** web terminal endpoints, settings, and table removed.
* **backend:** clients that relied on implicit run.active transition on observed session activity must now call /api/runs/{id}/active explicitly. Sessions started during 'preparing' have run_id=NULL and appear only under Sessions (advanced).
* **backend:** stereotype_caps no longer includes Appium-only device metadata (manufacturer, model, deviceName, ip, sanitized device_config appium_caps) or the unused gridfleet:available sentinel. Stereotype is now strictly the routing surface — pack-declared keys, deviceId, run_id, and tag fanout. Appium still receives full device metadata via extra_caps.

### Features

* **agent:** add POST /agent/pack/{pack_id}/doctor endpoint ([71c4e72](https://github.com/quidow/gridfleet/commit/71c4e72dc7b815d3ca5f1e58023dc06a19781e29))
* **agent:** close FastAPI best-practice gaps ([443372e](https://github.com/quidow/gridfleet/commit/443372ecf9defd976cc17f327cdf81a5d233c211))
* **agent:** expose registration state in /agent/health response ([4dc6ed3](https://github.com/quidow/gridfleet/commit/4dc6ed3463a5bdf2bb99b922010aa1c8f804f749))
* **agent:** tighten response models with typed cores and named extras fields ([fd591e3](https://github.com/quidow/gridfleet/commit/fd591e3918f2a7e9f8f07daf012c823f5ab28745))
* **agent:** update ToolsStatusResponse schema for structured tool status ([0c29392](https://github.com/quidow/gridfleet/commit/0c2939224120edec788c69c6a3343beee9eccf0b))
* backend-driven event severity ([#263](https://github.com/quidow/gridfleet/issues/263)) ([b6bd4e4](https://github.com/quidow/gridfleet/commit/b6bd4e4f1f9315edc98a7fedd3f8cb721adbdd98))
* **backend,agent,frontend:** split Fire OS display version from routing major ([a289455](https://github.com/quidow/gridfleet/commit/a2894559f41bd15b3a6a60e593021d1b2049d778))
* **backend,frontend:** per-host tool environment configuration ([#373](https://github.com/quidow/gridfleet/issues/373)) ([b11a489](https://github.com/quidow/gridfleet/commit/b11a4898dcd61b3f76d88e4a2f232d73cf38ca2a))
* **backend:** add agent log ingest route ([c317b01](https://github.com/quidow/gridfleet/commit/c317b0173f5a4da5adf0fbdccd9f5c4e4e1246e9))
* **backend:** add canonical bundle hash helper ([fd04b3b](https://github.com/quidow/gridfleet/commit/fd04b3bcac9c48bbb5e7b3490c6aafcd4c039474))
* **backend:** add dated alembic filenames ([2de88fa](https://github.com/quidow/gridfleet/commit/2de88fac3d2b110efcde24fd9c30297624faf9ac))
* **backend:** add declarative intent preconditions ([#285](https://github.com/quidow/gridfleet/issues/285)) ([943fb5c](https://github.com/quidow/gridfleet/commit/943fb5cfb1256a55866a59739389fd7cf1bdf9c6))
* **backend:** add device diagnostic export ([#287](https://github.com/quidow/gridfleet/issues/287)) ([6afb0d1](https://github.com/quidow/gridfleet/commit/6afb0d1c8486c642688fe0ad952d2575491dfed0))
* **backend:** add device export bundle builder ([01903df](https://github.com/quidow/gridfleet/commit/01903df77afb32b71d2715a0f2fbb34253ebff77))
* **backend:** add device inventory column enum ([afbe4a8](https://github.com/quidow/gridfleet/commit/afbe4a83b66c35fbaf1158af17a49eebb8f4a0bc))
* **backend:** add device portability schemas ([d9c2fb8](https://github.com/quidow/gridfleet/commit/d9c2fb8ad2c3f941ce16c1a8c3397ed24f558009))
* **backend:** add GET /api/devices/export endpoint ([e4d18cd](https://github.com/quidow/gridfleet/commit/e4d18cd05fbbd8a28531bcccad4fc609811c7556))
* **backend:** add GET /api/devices/inventory endpoint ([19f52ad](https://github.com/quidow/gridfleet/commit/19f52ade7692c7ad74adac4c8f73588e1c219df5))
* **backend:** add has_data flag to fleet capacity timeline point ([e368dea](https://github.com/quidow/gridfleet/commit/e368deadbc294b2ca44330ca8e87534b0ebd1390))
* **backend:** add host agent log entry model ([e130948](https://github.com/quidow/gridfleet/commit/e1309481c03c4ecf81ad7d1965f7b524f9096ba6))
* **backend:** add host agent log read route ([78ae75f](https://github.com/quidow/gridfleet/commit/78ae75fe99d248f4777fa635184219c7ce3d30c0))
* **backend:** add host events read route ([61e04d6](https://github.com/quidow/gridfleet/commit/61e04d6c7659ec50069e88a7194ac405e8d53569))
* **backend:** add host log schemas ([6262182](https://github.com/quidow/gridfleet/commit/6262182d4b09234548b1a2bd4fb1a89944cb3a66))
* **backend:** add hosts.* hardware metadata columns ([a52040d](https://github.com/quidow/gridfleet/commit/a52040d2975318e3f54e8c4ebc8424e801947111))
* **backend:** add hub event-bus subscriber ([d773ea9](https://github.com/quidow/gridfleet/commit/d773ea9a106a6db41a0d1bce0ad5e3228137962c))
* **backend:** add metadata naming convention ([727b0d9](https://github.com/quidow/gridfleet/commit/727b0d9dee377f579db8984065b5b4f58ac9c910))
* **backend:** add migration for host agent logs ([0430568](https://github.com/quidow/gridfleet/commit/043056818f2a0dc063a7cc2039b99a984ca0bbbd))
* **backend:** add migration to drop auto_manage column and setting ([0fe8f35](https://github.com/quidow/gridfleet/commit/0fe8f35e78f0d0452f401d09036b599bf9c73d95))
* **backend:** add os_version_display column to devices ([0669e95](https://github.com/quidow/gridfleet/commit/0669e95668892565cc97dca6bb9362a29433ff42))
* **backend:** add POST /api/devices/import endpoint ([ca07b3b](https://github.com/quidow/gridfleet/commit/ca07b3bf1410e29507313b75eab890e9b935c3e6))
* **backend:** add POST /api/devices/import/validate endpoint ([341df36](https://github.com/quidow/gridfleet/commit/341df362a1e93dd51317ace69a597bf291a23a5a))
* **backend:** add POST /api/hosts/{host_id}/driver-packs/{pack_id}/doctor proxy route ([cb222a5](https://github.com/quidow/gridfleet/commit/cb222a51a1b9199569bbbfdf588aa65a0f6f7f74))
* **backend:** add settings registry keys for agent logs ([b4f0286](https://github.com/quidow/gridfleet/commit/b4f028672d35f4ad46bdca4681e80ad7c5fe2397))
* **backend:** add streaming device inventory export service ([7a5df25](https://github.com/quidow/gridfleet/commit/7a5df2517e9dd094917426c45df9e81313c11999))
* **backend:** add structured status fields to DeviceHealthSummaryRead ([84f2545](https://github.com/quidow/gridfleet/commit/84f2545ae537a8072b3c20a374ed86c317996550))
* **backend:** add tool_dependencies to driver pack manifest schema ([cd56a6e](https://github.com/quidow/gridfleet/commit/cd56a6e9e18a7f50bdbaa8e1f92210f299acc70e))
* **backend:** close fastapi best-practices gaps ([c47cd2e](https://github.com/quidow/gridfleet/commit/c47cd2ea6dca49cf0708153a865fea1f9ecaed18))
* **backend:** close PR [#297](https://github.com/quidow/gridfleet/issues/297) follow-ups (intent preconditions + resolver refactor) ([#301](https://github.com/quidow/gridfleet/issues/301)) ([8aff496](https://github.com/quidow/gridfleet/commit/8aff4968ce0e747832c7f43f3903961af491c869))
* **backend:** codegen Pydantic models from agent OpenAPI ([#235](https://github.com/quidow/gridfleet/issues/235)) ([8c70b5c](https://github.com/quidow/gridfleet/commit/8c70b5c1ca3baad63582e17030e54dbc76bc5503))
* **backend:** commit device import bundle with atomic verification enqueue ([14cf656](https://github.com/quidow/gridfleet/commit/14cf6560f115e39d22d19da12861d8eae486c73e))
* **backend:** declare stereotype routing filters in pack manifests ([56cd0bd](https://github.com/quidow/gridfleet/commit/56cd0bd2e7c4a505e5818bbc6b3dbe192524a00f))
* **backend:** decode selenium grid event-bus frames ([02edb8b](https://github.com/quidow/gridfleet/commit/02edb8baef8206a1c929561448cdb404c2c298c0))
* **backend:** decompose runs service ([#242](https://github.com/quidow/gridfleet/issues/242)) ([b3f7e4b](https://github.com/quidow/gridfleet/commit/b3f7e4b49dadffc8903e8a2af177ef8b58e2b05c))
* **backend:** detect invalid pack in import validate and expand test coverage ([ecf3b34](https://github.com/quidow/gridfleet/commit/ecf3b34caffd1ffb9935729566cfdc6496b513a5))
* **backend:** document api error responses ([94e3a67](https://github.com/quidow/gridfleet/commit/94e3a675463f62ca4fda24196b569afac60b511f))
* **backend:** drop heavy Grid probe from node_health loop ([#249](https://github.com/quidow/gridfleet/issues/249)) ([c7bea0d](https://github.com/quidow/gridfleet/commit/c7bea0d2f399ea43024bffa9e613004df0583d96))
* **backend:** expose grid event-bus subscriber metrics ([661dd5b](https://github.com/quidow/gridfleet/commit/661dd5b9e67bc87fed646a4025b2119175f911eb))
* **backend:** expose grid event-bus URLs via GridConfig ([04eb071](https://github.com/quidow/gridfleet/commit/04eb071bfc025c46ac0f3e602826b9b7e7738c1c))
* **backend:** expose os_version_display on Device API ([9ed66bd](https://github.com/quidow/gridfleet/commit/9ed66bd800c9161298ba2a932c7e6d99956b9c78))
* **backend:** filter notifications by severity ([7700964](https://github.com/quidow/gridfleet/commit/770096478b6b99119ecbaea64c4990dfd6d24f15))
* **backend:** gate docs by environment ([473a411](https://github.com/quidow/gridfleet/commit/473a411241ebd4be0b701c5207f602a4ec83f91d))
* **backend:** hold graceful node stops while client session active ([c6b2ae5](https://github.com/quidow/gridfleet/commit/c6b2ae5a1902a2dbab122fd39cd52e3938487ca3))
* **backend:** interpolate device context in render_stereotype ([19555cd](https://github.com/quidow/gridfleet/commit/19555cd37dd7f33d057a6f50396bdee0e3d2935a))
* **backend:** isolate prep sessions from test run results ([#290](https://github.com/quidow/gridfleet/issues/290)) ([02669cc](https://github.com/quidow/gridfleet/commit/02669ccb156c023978e23750596bffac785f3432))
* **backend:** leader-owned hub event-bus subscriber loop ([6926d27](https://github.com/quidow/gridfleet/commit/6926d27bd9ec4760f3b227482c2abfec7b3f88d9))
* **backend:** move leader subsystem into core ([#236](https://github.com/quidow/gridfleet/issues/236)) ([49d4461](https://github.com/quidow/gridfleet/commit/49d446154d76e7ef6d1fa3b6341a641259fee278))
* **backend:** pass distinct maintenance_reason from automated escalation paths ([29e2966](https://github.com/quidow/gridfleet/commit/29e2966882e5416b2991cf56a8479a2fb0f4eb04))
* **backend:** persist host hardware metadata from registration payload ([3b29b2c](https://github.com/quidow/gridfleet/commit/3b29b2c7acd5439179f6beca5238c5389b2fc57e))
* **backend:** prune stored agent logs in cleanup loop ([491bc90](https://github.com/quidow/gridfleet/commit/491bc90504be378380fafde3e4cb885ee045a83e))
* **backend:** query agent logs with filters ([14794f0](https://github.com/quidow/gridfleet/commit/14794f077e73ee0596af7bda0b9d8c7f91728a50))
* **backend:** query host-scoped events ([9b3cbc5](https://github.com/quidow/gridfleet/commit/9b3cbc58d139de086860041ee94bae347054abdd))
* **backend:** register grid event-bus URLs and downgrade poll default to 30s ([5b9656e](https://github.com/quidow/gridfleet/commit/5b9656e4620c0e74561d1cc9a2443e77f4752a84))
* **backend:** replace health_failure recovery deny intent with review_required flag ([bb27547](https://github.com/quidow/gridfleet/commit/bb275475b11c40e691e513643221375a87de961d))
* **backend:** snap fleet capacity timeline window to bucket grid ([37e9eb1](https://github.com/quidow/gridfleet/commit/37e9eb1ce12e5534dc9a27d7ef7190fcbf326cb2))
* **backend:** start grid event-bus subscriber on leader acquisition ([9d21014](https://github.com/quidow/gridfleet/commit/9d21014499142ea75c19b09ce3665915159f3224))
* **backend:** state-write hardening (guardrail + projection conversions + intent sweep + audit) ([#297](https://github.com/quidow/gridfleet/issues/297)) ([90491e2](https://github.com/quidow/gridfleet/commit/90491e2d59a41d2f0724320afa75d125c271842d))
* **backend:** store maintenance_reason in lifecycle_policy_state JSON ([c72fcfc](https://github.com/quidow/gridfleet/commit/c72fcfc71a55d13ee5ce4972296075e08230aa46))
* **backend:** surface maintenance_reason in lifecycle policy summary detail ([9ebed7f](https://github.com/quidow/gridfleet/commit/9ebed7f9ac77cb191351650be3f2328eab95703d))
* **backend:** thread device_context to render_stereotype ([e3b925d](https://github.com/quidow/gridfleet/commit/e3b925de6da94adabf3f5c5c67c71bcb974afbff))
* **backend:** trim Grid slot stereotype to routing surface ([32c7fce](https://github.com/quidow/gridfleet/commit/32c7fcedc8be768a730d705d529bdf7351d76771))
* **backend:** update HostToolStatusRead schema for structured tool status ([334e21d](https://github.com/quidow/gridfleet/commit/334e21d41d66b057e54abe0269720ddb3cc01902))
* **backend:** validate device import bundles ([11f0fcd](https://github.com/quidow/gridfleet/commit/11f0fcdba71ffcd9dee0f5a8798182a62bfe4d68))
* **backend:** verify allocator matches firetv routing major ([266bc2a](https://github.com/quidow/gridfleet/commit/266bc2a37a9f1aeded348d5939f691a922382a87))
* **backend:** wake session_sync_loop on hub event-bus doorbell ([7d95618](https://github.com/quidow/gridfleet/commit/7d95618d948aa28908df86faf42775de1c3998e5))
* **backend:** write agent log batches with deduplication ([04118c2](https://github.com/quidow/gridfleet/commit/04118c24d0ebd6965f47ba4eb9baf440b2c11051))
* consolidate device status panel with maintenance reasons and simplified health pills ([b259fff](https://github.com/quidow/gridfleet/commit/b259fff4b17baa508396ddc9997788d4b87697cf))
* data-driven driver pack tool dependencies on host overview ([225adc5](https://github.com/quidow/gridfleet/commit/225adc56c475f5ef606b1cacfb61c9715cee57d7))
* device config export/import + read-only inventory snapshot ([325b222](https://github.com/quidow/gridfleet/commit/325b222b2f90b1933a164c06e3940ae4c25be129))
* **frontend:** add host logs tab ([f21de6a](https://github.com/quidow/gridfleet/commit/f21de6af1964224f6f4bac132297829e3a5e9426))
* **frontend:** add sessions tab to device detail page ([#386](https://github.com/quidow/gridfleet/issues/386)) ([6b93c7b](https://github.com/quidow/gridfleet/commit/6b93c7be0140d50bbf40e9995c188d61fc515a44))
* **frontend:** surface device review_required in triage and health badges ([8501c77](https://github.com/quidow/gridfleet/commit/8501c7785ccf30602bec76cc38b1ec9f0954dffd))
* **frontend:** wire driver pack policy editor and release deletion ([#395](https://github.com/quidow/gridfleet/issues/395)) ([4a47bb0](https://github.com/quidow/gridfleet/commit/4a47bb084b6e806063d4d9b957b716eb08472d46))
* on-demand Appium doctor checks ([221256d](https://github.com/quidow/gridfleet/commit/221256d16983974a0324774d85a6cfca99c78ee7))
* selenium grid stability + performance wins ([b5370cc](https://github.com/quidow/gridfleet/commit/b5370ccd539e6692dcec500efe9cf3f7ce59268f))
* severity filter on Notifications ([a173b4b](https://github.com/quidow/gridfleet/commit/a173b4b1cf93b6ab40cb277053d1f5fa2ced5549))
* show probe sessions on Sessions page (opt-in, no analytics impact) ([#246](https://github.com/quidow/gridfleet/issues/246)) ([6e2db59](https://github.com/quidow/gridfleet/commit/6e2db595f42f361b0e3d78d83bf7ff15203c6397))
* surface host hardware metadata on host detail ([de6116e](https://github.com/quidow/gridfleet/commit/de6116e5155d80eee9e868cbc69c56cdfa027afa))


### Bug Fixes

* **agent:** keep adapter-fed pack responses permissive ([954a31a](https://github.com/quidow/gridfleet/commit/954a31a8b9c4a8a1e5d03188be44713d576e5a5d))
* **agent:** update remaining tests for structured tool status response ([bbdf675](https://github.com/quidow/gridfleet/commit/bbdf67520254261bff83f0a55bb4ab86bcb19942))
* **agent:** use human-readable display names for host tools ([67f48be](https://github.com/quidow/gridfleet/commit/67f48bed99faec5f142cf2cba23edebad9db6a69))
* **backend,agent:** close cooldown→grid-routing race window ([ba734a7](https://github.com/quidow/gridfleet/commit/ba734a72a5a57f7c3c8afd4a841864364ae7e906))
* **backend,agent:** close cooldown→grid-routing race window ([39a4886](https://github.com/quidow/gridfleet/commit/39a48868e52fc0135189da6205beea90df4defe8))
* **backend,agent:** deliver agent reconfigure inline on cooldown escalation ([bfd72e6](https://github.com/quidow/gridfleet/commit/bfd72e6f58bab2e782b0dd8ec0e4dac087c0169c))
* **backend,agent:** propagate os_version_display through property refresh ([#264](https://github.com/quidow/gridfleet/issues/264)) ([f96c3bb](https://github.com/quidow/gridfleet/commit/f96c3bb4c72b253883c7f6852593e2e474e10f44))
* **backend,agent:** resolve CodeQL code scanning alerts ([#374](https://github.com/quidow/gridfleet/issues/374)) ([48c1f71](https://github.com/quidow/gridfleet/commit/48c1f71a3743e417fb4960ea3fd183133e38147b))
* **backend:** add doctor route tests, update OpenAPI surface baseline and types ([eb939c0](https://github.com/quidow/gridfleet/commit/eb939c0704ecf3c18b54dd2d394426e9b1d12435))
* **backend:** aggregate fleet capacity timeline by latest snapshot per bucket ([2fdad8f](https://github.com/quidow/gridfleet/commit/2fdad8f582926dfd58f782fa1d720ab016dcc20b))
* **backend:** align maintenance recovery intent reason with cleanup constant ([9db8c42](https://github.com/quidow/gridfleet/commit/9db8c42d5bbf75840dd5c35771814908c061cabf))
* **backend:** block available transition while appium node stop is in flight ([#289](https://github.com/quidow/gridfleet/issues/289)) ([6df682b](https://github.com/quidow/gridfleet/commit/6df682bc786e5239e86cff9d2f65e5820840fdcb))
* **backend:** bound inline cooldown reconfigure timeout to 5s ([0d8429a](https://github.com/quidow/gridfleet/commit/0d8429a7afa974af0158961b8eada59fd320362c))
* **backend:** bump openapi route surface baseline for host log routes ([f76930a](https://github.com/quidow/gridfleet/commit/f76930ab2c6b350fabf9673b943b33edc1f913b0))
* **backend:** chain agent-log migration after host-hardware migration ([a73d0d8](https://github.com/quidow/gridfleet/commit/a73d0d85c34e0eefff6a7f0a0f66f60d8de3d125))
* **backend:** clear CodeQL warnings in services and tests ([39f2e19](https://github.com/quidow/gridfleet/commit/39f2e195777f29107fafae3cbdff253ea555e7cb))
* **backend:** clear health-failure reservation intent on run release ([49ea165](https://github.com/quidow/gridfleet/commit/49ea165b8389ff2b39607ee97116dc70a291aa9c))
* **backend:** clear reconciler start-failure flag on observed-running convergence ([2a19e2c](https://github.com/quidow/gridfleet/commit/2a19e2c8c42e4fc822885c97149978c4bb55b199))
* **backend:** clear stale session-running recovery suppression ([#345](https://github.com/quidow/gridfleet/issues/345)) ([bf8b7a9](https://github.com/quidow/gridfleet/commit/bf8b7a961c6d3a3b098dd8f5b6ef65e7a8ab44d8))
* **backend:** close host/group registration TOCTOU races ([#320](https://github.com/quidow/gridfleet/issues/320)) ([40bec89](https://github.com/quidow/gridfleet/commit/40bec89da5ba05ea1abf16660f03e49fadfdd02f))
* **backend:** close orphan-session hydration and viability-probe races ([#322](https://github.com/quidow/gridfleet/issues/322)) ([8312e71](https://github.com/quidow/gridfleet/commit/8312e71deb00f6b6560fdaa5d2fbf533e141c2de))
* **backend:** close run-reaper and exclude-device reservation races ([#321](https://github.com/quidow/gridfleet/issues/321)) ([2b57d64](https://github.com/quidow/gridfleet/commit/2b57d647660711d4f2aa873eeb7460cd07a56682))
* **backend:** coerce verification payload enums after agent normalize ([#262](https://github.com/quidow/gridfleet/issues/262)) ([e856acf](https://github.com/quidow/gridfleet/commit/e856acf4b074c9d5186fd5e7aa6901322da73463))
* **backend:** collapse session-end stop-in-flight flap + revoke stale active_session intent ([ea9c8cb](https://github.com/quidow/gridfleet/commit/ea9c8cbcbb47a0ddfedd33987f1258b5ea99b8bb))
* **backend:** cover device.id None guard + clarify drop rationale ([e8711a6](https://github.com/quidow/gridfleet/commit/e8711a6ccd5ed3317aea212eb593d70cb6a2e811))
* **backend:** defer verification intent revoke until verified_at is set ([#310](https://github.com/quidow/gridfleet/issues/310)) ([07e8773](https://github.com/quidow/gridfleet/commit/07e877368bdc5dcba90b3f290fca407a66c64b96))
* **backend:** drain chained handler tasks during event bus shutdown ([62d425d](https://github.com/quidow/gridfleet/commit/62d425def08f8f506e4f07a0147f5039a058d9bb))
* **backend:** drain control-plane services before DROP SCHEMA in tests ([0d14676](https://github.com/quidow/gridfleet/commit/0d1467681a30bb268ee44fc9ebde99496823dcdd))
* **backend:** drop phantom probe rows from session list ([#303](https://github.com/quidow/gridfleet/issues/303)) ([d70ce6d](https://github.com/quidow/gridfleet/commit/d70ce6d66f1958bb625c5d7fc237908f0ebd85c9))
* **backend:** drop viability-probe projection + revoke stale connectivity intent ([23561c4](https://github.com/quidow/gridfleet/commit/23561c4ab1454bac249067eceb291d1a10fdf6d5))
* **backend:** expose device identity conflict error ([462a35b](https://github.com/quidow/gridfleet/commit/462a35b7d8a4d47d043596aa928d0b65d950887a))
* **backend:** fix remaining test failures from auto_manage removal ([205d677](https://github.com/quidow/gridfleet/commit/205d67784f1a7edea332e002f48309676eb071b9))
* **backend:** guard host hardware setattr against schema drift ([b67f5bf](https://github.com/quidow/gridfleet/commit/b67f5bf84cf870ee0c38d3c2011e36dffc65a01e))
* **backend:** harden inventory csv export and import commit error handling ([3690e5b](https://github.com/quidow/gridfleet/commit/3690e5bbb8444dad118d454e0b507aba16911f82))
* **backend:** hydrate orphan session rows from grid stereotype ([6b37e51](https://github.com/quidow/gridfleet/commit/6b37e517ab00ec6cd02f43c1e8e35c43cce5506f))
* **backend:** include host_id in circuit_breaker events ([#274](https://github.com/quidow/gridfleet/issues/274)) ([ec4fc11](https://github.com/quidow/gridfleet/commit/ec4fc11ea3d1bb23ee82915325d353f5ab66fc77))
* **backend:** keep verification node alive during slow probe ([#305](https://github.com/quidow/gridfleet/issues/305)) ([34216fb](https://github.com/quidow/gridfleet/commit/34216fb60271f65a3e8c720ad7908fa7a57fd5d3))
* **backend:** kick immediate convergence from verification run_probe ([d1435b0](https://github.com/quidow/gridfleet/commit/d1435b0ae13e6c93701f3efe8db06849a73c7a90))
* **backend:** make naming-convention baseline idempotent ([e8fa067](https://github.com/quidow/gridfleet/commit/e8fa067efccb40da23c247589ebe50aaa0d8b4bb))
* **backend:** migrate completing rows before dropping enum value ([2f49de8](https://github.com/quidow/gridfleet/commit/2f49de85be48c2ab24996b29ce8727b71a7d1df3))
* **backend:** narrow terminal proxy errors ([8874ab5](https://github.com/quidow/gridfleet/commit/8874ab53104e6e18f52d77a5339adfc050ad7fe6))
* **backend:** persist reservation cooldown counter across TTL exclusion clears ([bfe9603](https://github.com/quidow/gridfleet/commit/bfe96039f4ed34e26ccec71b8fd89779acb1d67c))
* **backend:** preserve cooldown_count across legacy expired-cooldown sweep ([57eb770](https://github.com/quidow/gridfleet/commit/57eb770acbd18c0ba881f79e32d135003e32c208))
* **backend:** read session identity from grid stereotype not capabilities ([#277](https://github.com/quidow/gridfleet/issues/277)) ([428a137](https://github.com/quidow/gridfleet/commit/428a1373d9cd0e41661d3882e56d0c557fb32c08))
* **backend:** remove dangling `template` export from packs services __all__ ([b3458ed](https://github.com/quidow/gridfleet/commit/b3458ed81b7af6c4605b850fc76f6fd794ed9e37))
* **backend:** remove unused PUT config and POST refresh device endpoints ([#388](https://github.com/quidow/gridfleet/issues/388)) ([bbd1cd9](https://github.com/quidow/gridfleet/commit/bbd1cd9edf92110e6c02d8f3fb8cd400f2e05a89))
* **backend:** repair CI failures from PR [#243](https://github.com/quidow/gridfleet/issues/243) ([63a7299](https://github.com/quidow/gridfleet/commit/63a72991d5929f28b8ebf5843f3ccaebfc9f260c))
* **backend:** resolve remaining CodeQL code scanning alerts ([#375](https://github.com/quidow/gridfleet/issues/375)) ([3628e20](https://github.com/quidow/gridfleet/commit/3628e2050d013d60b28c8322ebf759d9028089fc))
* **backend:** return raw response payloads from routers ([733d0e5](https://github.com/quidow/gridfleet/commit/733d0e5d6ea81aa6f4a92b4c3d44278309515653))
* **backend:** serialize pack drain count + disable under FOR UPDATE ([#324](https://github.com/quidow/gridfleet/issues/324)) ([d393d27](https://github.com/quidow/gridfleet/commit/d393d279417d5475c98f08ad36dfdca39700430e))
* **backend:** serialize uuid columns in inventory json export ([df83644](https://github.com/quidow/gridfleet/commit/df836442bbdb6eb745ac02c38b5d188802b3d6fb))
* **backend:** stage forced reconfigure after cooldowned-node restart ([#348](https://github.com/quidow/gridfleet/issues/348)) ([3bafa25](https://github.com/quidow/gridfleet/commit/3bafa2573926bc97fd3e1787960575db53858444))
* **backend:** stop auto_recovery intent from pinning stale desired_port ([864e6fe](https://github.com/quidow/gridfleet/commit/864e6feb427f050586995363a59f7566a757062a))
* **backend:** stop spurious offline flap on session-end and node observation writes ([1df6def](https://github.com/quidow/gridfleet/commit/1df6defc74175864c973987a61b130aca89d7406))
* **backend:** suppress device.crashed event spam for already-offline devices ([#402](https://github.com/quidow/gridfleet/issues/402)) ([5c43d76](https://github.com/quidow/gridfleet/commit/5c43d76f948454bb0fb5ab0e41a00406d93f26df))
* **backend:** surface inline reconfigure delivery failure from cooldown ([#347](https://github.com/quidow/gridfleet/issues/347)) ([58b6626](https://github.com/quidow/gridfleet/commit/58b6626d6803cdf22659859cad040b33c6b97561))
* **backend:** tick subscriber-loop heartbeat each cycle ([b0d22dc](https://github.com/quidow/gridfleet/commit/b0d22dc9748c373e9e6d35f25fe53e9a7236fa0e))
* **backend:** tidy precondition sweep TOCTOU and natural-clear token metric ([#323](https://github.com/quidow/gridfleet/issues/323)) ([21ec7ed](https://github.com/quidow/gridfleet/commit/21ec7edc91d063ac4fb369536c5fbc316d32e775))
* **backend:** tighten host agent log surface from PR review ([5d32557](https://github.com/quidow/gridfleet/commit/5d3255739e939bc7d7e91bf0036a0d823ffdc12a))
* **backend:** tokenized start intents beat tokenless baselines at the same priority ([#273](https://github.com/quidow/gridfleet/issues/273)) ([312935b](https://github.com/quidow/gridfleet/commit/312935b9ed98e6ce1dfe45921788ecf28a02da7b))
* **backend:** tolerate deleted device in reconciler start-failure path ([53b03b3](https://github.com/quidow/gridfleet/commit/53b03b3409a335bf118ea50298573069c2eb0097))
* **backend:** tolerate state-machine races during orphan hydration ([258a704](https://github.com/quidow/gridfleet/commit/258a704dde2ac2561dfcb146b55ef6273b58efb6))
* **backend:** tolerate transient Selenium Grid hiccups in session viability ([#261](https://github.com/quidow/gridfleet/issues/261)) ([62f153a](https://github.com/quidow/gridfleet/commit/62f153a10fcd61177a254de67214a0c5fc8d05f4))
* **backend:** unblock tvOS device verification via probe routing ([#326](https://github.com/quidow/gridfleet/issues/326)) ([af2c3d0](https://github.com/quidow/gridfleet/commit/af2c3d00f0e3861a33e9d3092b9e29fdc07547da))
* **backend:** unify operator node lifecycle through device_intents ([#302](https://github.com/quidow/gridfleet/issues/302)) ([4fb30d2](https://github.com/quidow/gridfleet/commit/4fb30d2560205063c86092ea8818e499737eeb5f))
* **backend:** update pre-existing tests for doorbell loop refactor ([11ce067](https://github.com/quidow/gridfleet/commit/11ce0674c40476f8a7b1a52cc464d65c9a62844c))
* **backend:** validate types filter symmetrically with severity ([571a987](https://github.com/quidow/gridfleet/commit/571a98787e0e911200d086069999a598345a210e))
* bind testkit-registered sessions to their device ([35030ef](https://github.com/quidow/gridfleet/commit/35030eff438b3d8cfa17b54f1329dfeb03dadf07))
* fleet health history aggregation and gap rendering ([cc745aa](https://github.com/quidow/gridfleet/commit/cc745aaf8856e08afe97bf5edf1fb7625a50b520))
* **frontend:** anchor single-point fleet health dot to right edge ([5125c0c](https://github.com/quidow/gridfleet/commit/5125c0cf3b029cacf3413a3f23f3747f40b3b6fe))
* **frontend:** use dedicated maintenance_reason field instead of overloaded detail ([#357](https://github.com/quidow/gridfleet/issues/357)) ([5adc5f1](https://github.com/quidow/gridfleet/commit/5adc5f172f23be36d0d1e0d88d1d480b23e8af20))
* **main:** address code review feedback ([701fd5c](https://github.com/quidow/gridfleet/commit/701fd5c1d85e792f766efc188c927bf919fc66bf))
* **main:** drop ios_booted health check on tvos real devices ([da39468](https://github.com/quidow/gridfleet/commit/da39468715e0c619f93da04ab5a4e4b44db34df1))
* **main:** truncate pack_id in log and add type guard on agent checks response ([d7f4503](https://github.com/quidow/gridfleet/commit/d7f4503cb37695579c91b65b4310b0dd452080d8))
* rotate past occupied appium ports during verification ([52b62c0](https://github.com/quidow/gridfleet/commit/52b62c0e3017a19b9dd87a2db69c62fe90d8149e))
* session-safe graceful node stops + adapter probe + idle timeout ([0cc0689](https://github.com/quidow/gridfleet/commit/0cc068958c1c7df2529b6146e1446e3fc0ca180f))


### Performance Improvements

* **backend:** batch background-loop heartbeats with in-memory flush ([#254](https://github.com/quidow/gridfleet/issues/254)) ([5249bef](https://github.com/quidow/gridfleet/commit/5249befc6f4a99801f53b3a8335a0bebfef3e0b6))
* **backend:** batch driver-pack lookups in device readiness ([#312](https://github.com/quidow/gridfleet/issues/312)) ([4244c02](https://github.com/quidow/gridfleet/commit/4244c02ccbed56f223cc94abaf785bf944e7eb75))
* **backend:** offload pack archive work ([dd123ea](https://github.com/quidow/gridfleet/commit/dd123ea2d3a417a9307f33a490edc7791a3ea69c))


### Dependencies

* **deps-dev:** bump types-pyyaml in /backend ([#296](https://github.com/quidow/gridfleet/issues/296)) ([e29ea29](https://github.com/quidow/gridfleet/commit/e29ea2954b762bff94771a7c6ee52fd61f813418))
* **deps:** bump idna from 3.11 to 3.15 in /backend ([#309](https://github.com/quidow/gridfleet/issues/309)) ([30c0115](https://github.com/quidow/gridfleet/commit/30c011516537c4b2bf52b22bab9c7a3cf8fb05d9))
* **deps:** bump python-multipart in /backend ([#295](https://github.com/quidow/gridfleet/issues/295)) ([8d8773f](https://github.com/quidow/gridfleet/commit/8d8773fed5bd0ffa35fcd59a14122f71f26cabde))
* **deps:** bump ruff in /agent ([#294](https://github.com/quidow/gridfleet/issues/294)) ([0f82674](https://github.com/quidow/gridfleet/commit/0f826741fbd1ebb90eeff8ab169b4aee4da7c91e))
* **deps:** Bump the python-dependencies group ([#396](https://github.com/quidow/gridfleet/issues/396)) ([cdb584d](https://github.com/quidow/gridfleet/commit/cdb584d47f941c605148ba159585f4f1b7196e96))
* **deps:** bump the python-dependencies group across 1 directory with 2 updates ([#341](https://github.com/quidow/gridfleet/issues/341)) ([a230e17](https://github.com/quidow/gridfleet/commit/a230e1776c013d69166978de7b4858380c415862))
* **deps:** bump uvicorn[standard] in /agent ([#293](https://github.com/quidow/gridfleet/issues/293)) ([96f8ec9](https://github.com/quidow/gridfleet/commit/96f8ec904f08572855b5481e5f02be83f979bb21))
* **deps:** drop web terminal deps ([22aca83](https://github.com/quidow/gridfleet/commit/22aca83c7c68351ca52ecd276d776e6606c19b30))


### Documentation

* **backend:** clarify grid event-bus XPUB/XSUB port assignment ([b238768](https://github.com/quidow/gridfleet/commit/b23876846791eb2fcf689ea6978417e58a2fef33))


### Code Refactoring

* **backend:** remove web terminal feature ([7b4ce2b](https://github.com/quidow/gridfleet/commit/7b4ce2befec37ccca3674254f9e4f19510ca9a73))

## 0.1.0 — Initial Public Preview

- Initial public preview baseline for the GridFleet control plane backend.
- FastAPI manager with async SQLAlchemy + Postgres, Alembic migrations, and leader-owned background loops.
- Hardened production compose defaults around authentication and host approval.
- Added CI, security scanning, and dependency update workflows.
