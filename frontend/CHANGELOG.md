# Changelog — GridFleet Frontend

All notable changes to the GridFleet operator dashboard (React + TypeScript + Vite) are documented here.

## Unreleased

### Features

- Show "Update available" badge and notice panel on host cards when the backend reports a newer recommended agent version.

### Fixes

- Run frontend nginx container as non-root user.

## [0.4.1](https://github.com/quidow/gridfleet/compare/gridfleet-frontend-v0.4.0...gridfleet-frontend-v0.4.1) (2026-06-30)


### Bug Fixes

* **frontend:** use relative dates for session outcome heatmap e2e fixture ([c1b45b3](https://github.com/quidow/gridfleet/commit/c1b45b36c3f24c0e1f60662ef52895e4c00b2b98))
* **frontend:** use relative dates for session outcome heatmap e2e fixture ([2a85778](https://github.com/quidow/gridfleet/commit/2a85778ae096beb1654adad5790f7742cf09c836))

## [0.4.0](https://github.com/quidow/gridfleet/compare/gridfleet-frontend-v0.3.0...gridfleet-frontend-v0.4.0) (2026-06-26)


### ⚠ BREAKING CHANGES

* **backend:** OpenAPI schema BulkMaintenanceEnter removed; enter-maintenance endpoints now reference BulkDeviceIds.
* **frontend:** the Appium Plugins settings tab and host-detail Plugins tab are removed.
* **frontend:** the operator dashboard no longer exposes webhooks.
* **backend:** the /api/webhooks endpoints and the webhooks / webhook_deliveries tables are removed.

### Features

* **backend:** remove webhooks feature and drop its tables ([5382dda](https://github.com/quidow/gridfleet/commit/5382ddafc6236cb7995cbee8927ec3b60d83462c))
* **frontend:** align dark theme with github palette ([52c8faf](https://github.com/quidow/gridfleet/commit/52c8faff0ad5f03e307607e6662b05f6a0dd2640))
* **frontend:** align dark theme with github palette ([9a218b3](https://github.com/quidow/gridfleet/commit/9a218b37ff6f0d0e18b624019c4c6a2536d1882c))
* **frontend:** remove appium plugins panels and api client ([637db8f](https://github.com/quidow/gridfleet/commit/637db8fa9ff98e5e9f1c508f8f309c073f3d6df6))
* **frontend:** remove webhooks dashboard and api client ([0ba76dc](https://github.com/quidow/gridfleet/commit/0ba76dc3ec39f92ee97726bf2128699ec811864b))
* **frontend:** show routing buckets on the router page ([cc13b6e](https://github.com/quidow/gridfleet/commit/cc13b6e44b7fb2c650baed03388c5f69ae93a906))


### Bug Fixes

* **frontend:** point empty device settings tab at device_checks category ([7c7ef30](https://github.com/quidow/gridfleet/commit/7c7ef307bfdc496e0a2bba4577d949420d2e9c2d))
* **frontend:** point empty device settings tab at device_checks category ([f7cff47](https://github.com/quidow/gridfleet/commit/f7cff4734f5091e48fda6b44fc6e414639a573a6))
* **frontend:** unify status tones and resolve dashboard/settings UI drift ([ad4e2c5](https://github.com/quidow/gridfleet/commit/ad4e2c541a9c4da3944049a90726cf98169022bf))
* **frontend:** unify status tones and resolve dashboard/settings UI drift ([7125331](https://github.com/quidow/gridfleet/commit/7125331e4e692e1416028c5bcdc8d3c95aa12c61))


### Performance Improvements

* **backend:** gate DeviceDetail.orchestration behind ?include=orchestration ([9a029b4](https://github.com/quidow/gridfleet/commit/9a029b42b7321d282ff4bb2899e555033a6cbfb9))
* **backend:** trim FleetOverview to the two fields the dashboard reads ([fae48d2](https://github.com/quidow/gridfleet/commit/fae48d270d4e894e19fdec51707606c9690cc8e0))


### Code Refactoring

* **backend:** fold bulk-maintenance-enter into bulk-device-ids ([e7abf48](https://github.com/quidow/gridfleet/commit/e7abf48ead0d3aa15aa6579a786d7cb2102e6291))

## [0.3.0](https://github.com/quidow/gridfleet/compare/gridfleet-frontend-v0.2.0...gridfleet-frontend-v0.3.0) (2026-06-22)


### ⚠ BREAKING CHANGES

* **frontend:** the device detail Diagnostics panel and its bundle capture/history are removed.
* **backend:** the `/api/diagnostics/devices/{device_id}/export`, `/snapshots`, and `/snapshots/{snapshot_id}` endpoints are removed.
* **backend:** POST /api/runs no longer accepts ?include=; the config, live_capabilities, test_data, and unavailable_includes fields are removed from reserved-device responses. Fetch device config/test_data/capabilities via the per-device GET endpoints instead.
* **backend:** remove devices by-connection-target lookup endpoint

### Features

* add Router page (grid visibility console) ([c4a0318](https://github.com/quidow/gridfleet/commit/c4a031836b447de19fe9992a9ccb79731d97f7a8))
* **backend:** detect and cure orphan adb-server systemPort socket ([4bc42f9](https://github.com/quidow/gridfleet/commit/4bc42f97de89818c52f66d89ca16a2a963c366c8))
* **backend:** expose device allocatable and unavailable_reason projection ([dee5571](https://github.com/quidow/gridfleet/commit/dee557133acad7655a5c2f9b25b7525503b00e63))
* **backend:** make preparation-failure maintenance escalation configurable ([3aeccc8](https://github.com/quidow/gridfleet/commit/3aeccc8bc3ee93360219b96f4ecbb776f85a3e45))
* **backend:** remove device diagnostics feature ([6c73c47](https://github.com/quidow/gridfleet/commit/6c73c471f0404fe66f45c7df335da237c2308a68))
* **backend:** remove devices by-connection-target lookup endpoint ([e5dccfc](https://github.com/quidow/gridfleet/commit/e5dccfc7a2680490b88d2ed9fc66e2201d49638f))
* **backend:** retire runs ?include feature ([0d06451](https://github.com/quidow/gridfleet/commit/0d0645135284fa21875171f3e284d95388f57a39))
* **backend:** stage 4 — transitioning projection (P6) + post-grid cruft cleanup (P7) ([19183cf](https://github.com/quidow/gridfleet/commit/19183cf12746e2202dfeb46c60e7de340dbce211))
* **backend:** tvos appium_env rename and prefer_devicectl toggle ([2e52296](https://github.com/quidow/gridfleet/commit/2e5229642f69d13e5ba32510d3aacab434718cb4))
* **backend:** warm-park cooldown via the accepting_new_sessions soft-gate (Stage 2) ([7130430](https://github.com/quidow/gridfleet/commit/7130430c34fbed4550c198b4a0351a81e24be17d))
* derive needs_attention from the operational axis and align the dashboard card ([c9d6a24](https://github.com/quidow/gridfleet/commit/c9d6a247fb5288a4de6ea300d190c812053966e1))
* **frontend:** add attention-row derivation to dashboard summary ([dc3261d](https://github.com/quidow/gridfleet/commit/dc3261dfa47b8853dbc68c4e2781044fa07c21ac))
* **frontend:** add dashboard kpi scorecard ([7ab1fac](https://github.com/quidow/gridfleet/commit/7ab1fac42699b21459c9275e867223f3287d2297))
* **frontend:** add fleet card with reserved legend and analytics chart link ([7c5302e](https://github.com/quidow/gridfleet/commit/7c5302e70148f874edd15f5fba8d05391b286d14))
* **frontend:** add grid-router api client, hook, and types ([0108dd6](https://github.com/quidow/gridfleet/commit/0108dd68e45acb5d28365654ac2cc8f79eab4eea))
* **frontend:** add needs-attention card ([eb870c8](https://github.com/quidow/gridfleet/commit/eb870c8f9639b5301b85ffb1fec86d0e77a7d34b))
* **frontend:** add rolling seven-day window hook ([80ce26f](https://github.com/quidow/gridfleet/commit/80ce26ff3457b82ab660772b959df54721b01259))
* **frontend:** add router counts and queue tile components ([ba46903](https://github.com/quidow/gridfleet/commit/ba469033bf6fabb3bbb80572b8199b80af987a85))
* **frontend:** add router NodeCard component ([016e6e6](https://github.com/quidow/gridfleet/commit/016e6e6a4f4dbe1729b3260017df1811d25a3e80))
* **frontend:** add Router page with route and nav link ([5a920e7](https://github.com/quidow/gridfleet/commit/5a920e755c68c8845b6bf3b7f89207c3c1c448f4))
* **frontend:** add run-grouped activity card ([f1bb2ac](https://github.com/quidow/gridfleet/commit/f1bb2ac65fec2254385eef78a7159091df1eb64f))
* **frontend:** align attention card rows with the needs_attention flag ([585ca21](https://github.com/quidow/gridfleet/commit/585ca210b02f5b5d9e57c8a16f0103873bda627e))
* **frontend:** drop reserved from fleet card and filter reservation via reserved param ([7d3830b](https://github.com/quidow/gridfleet/commit/7d3830b702192938ff4bdd1ee661756bda0f0848))
* **frontend:** per-signal health filters on devices page ([a95dcc8](https://github.com/quidow/gridfleet/commit/a95dcc8a61736fd1f84b81297bbe7196c624ce5f))
* **frontend:** per-verdict status pills and triage on device detail ([24491db](https://github.com/quidow/gridfleet/commit/24491dba91dcda01b84769cafc012f973136715e))
* **frontend:** redesign dashboard with kpi scorecard and tiled layout ([7ec4105](https://github.com/quidow/gridfleet/commit/7ec41055386b889176505d98017a1bcb27870745))
* **frontend:** regenerate api types for health verdicts ([f0b99da](https://github.com/quidow/gridfleet/commit/f0b99da72e81b20525e0d9a024cf19bcf70ef1a7))
* **frontend:** regenerate openapi types for cooldown unavailable reason ([d7e284d](https://github.com/quidow/gridfleet/commit/d7e284d48f2a3b47d2c080a96931a7bfe50636c1))
* **frontend:** regenerate openapi types for release_forwarded_ports action ([5dfa1e8](https://github.com/quidow/gridfleet/commit/5dfa1e82e7e1c73aa49e21ac620d928a07f67895))
* **frontend:** regenerate openapi types for transitioning unavailable_reason ([5383989](https://github.com/quidow/gridfleet/commit/538398918f2f25f7a482b5ab30ce3aa5049db501))
* **frontend:** remove device diagnostics UI ([e0e9ac7](https://github.com/quidow/gridfleet/commit/e0e9ac7c3ac08138bd42fcb23aecb64af855be37))
* **frontend:** restructure dashboard into scorecard and tiled layout ([755fa04](https://github.com/quidow/gridfleet/commit/755fa0413396f86fa833e312acf79a5668a09d35))
* **frontend:** session capabilities expansion component ([284082e](https://github.com/quidow/gridfleet/commit/284082ec3d80ad82374bf71e2ab6d90476a65ac2))
* **frontend:** session kill api client and active list params ([ea540de](https://github.com/quidow/gridfleet/commit/ea540de8456976a8f84da901f6f737e390ae4427))
* **frontend:** split sessions page into active and history tabs with kill action ([cfc06b2](https://github.com/quidow/gridfleet/commit/cfc06b268383a703b518e2572b41b629f0ed762a))
* **frontend:** surface allocatable and unavailable_reason device fields ([0c35600](https://github.com/quidow/gridfleet/commit/0c35600786364095c1eb7ae79e48018f19581cc0))
* **frontend:** tri-dot device health cell from verdicts ([9b61210](https://github.com/quidow/gridfleet/commit/9b61210827699e2e1cbcc433531eeb3214faddbb))
* sessions page rework — active/history tabs, capabilities, operator kill ([c8edbe9](https://github.com/quidow/gridfleet/commit/c8edbe9c4e52a87b561132e9166fe0544e53f7ac))
* split device health into per-signal verdicts (device / node / viability) ([ab11a4b](https://github.com/quidow/gridfleet/commit/ab11a4b91ff56f6af1806103157355ba5f37ab23))
* thread gridfleet device id into session caps; retire by-connection-target lookup ([0fe77ce](https://github.com/quidow/gridfleet/commit/0fe77ced0475597213d4bbf1eadc694d78856680))


### Bug Fixes

* **backend:** address grid router review (counts, ordering, trim, dedupe) ([f806b4f](https://github.com/quidow/gridfleet/commit/f806b4fafbe948dd87f75c6ff082d9e712dc5e6f))
* **backend:** cooldown escalation status reflects the maintenance toggle ([7e43d2e](https://github.com/quidow/gridfleet/commit/7e43d2e3b3a77ad6ff89018dadd9b33d430ec96b))
* **backend:** debounce flaky health checks and restore self-healed devices to their run ([51a11b7](https://github.com/quidow/gridfleet/commit/51a11b786791fe646b829f68cc78cbd2d4ae3955))
* **backend:** make grid router nodes/queue required for non-optional TS types ([7b56763](https://github.com/quidow/gridfleet/commit/7b567630fef6c0a523575112fc439b9ebfc6be67))
* batch of open grid/lifecycle issues (recovery-skip, queue hygiene, BL[#1](https://github.com/quidow/gridfleet/issues/1), F1, I11/N15, test_name, frontend) ([673c4d8](https://github.com/quidow/gridfleet/commit/673c4d8a5c911da38cd7dcb39d966d949b97dd77))
* **frontend:** add aria-label to router search filter input ([da38bfd](https://github.com/quidow/gridfleet/commit/da38bfdef5e2937eafd89190234cc3486dc1a95c))
* **frontend:** address Router page review (clipboard, states, reuse, e2e) ([069d803](https://github.com/quidow/gridfleet/commit/069d8035b58207ed7c0414704e515466c6db16e1))
* **frontend:** derive severity toggle from latest URL param ([d38eb65](https://github.com/quidow/gridfleet/commit/d38eb659f7e0ee2c6f6e2769c0d1165cc962a232))
* **frontend:** read correct deviceId cap in QueuedRequestsCard ([ca29593](https://github.com/quidow/gridfleet/commit/ca29593c51dea9b5c088d5dacd5666fcfe098c22))
* **frontend:** read pinned device id from the gridfleet deviceId cap ([4a74f4e](https://github.com/quidow/gridfleet/commit/4a74f4eaa3ae061d2b30c1659fa2fb6c29b91f57))
* **frontend:** stabilize notifications severity filter against rapid-toggle race ([a5ba4b6](https://github.com/quidow/gridfleet/commit/a5ba4b6dbea46e99abd2a3e8516835d3da403416))
* **frontend:** stabilize notifications severity filter against rapid-toggle race ([6651402](https://github.com/quidow/gridfleet/commit/6651402f55b5ab823a56e2e542879451b5e478d5))
* **frontend:** surface session-test 409 as a notice, not a console error ([69c2a15](https://github.com/quidow/gridfleet/commit/69c2a15b561a1f7ae4c6ca3b9b0359e0864df619))
* **frontend:** treat degraded health 503 payload as data, not an error ([52c8964](https://github.com/quidow/gridfleet/commit/52c896452841e03564deb87a08f47d46a9cb511d))
* **frontend:** use distinct info token for verifying node state ([5be6e8b](https://github.com/quidow/gridfleet/commit/5be6e8b7c2151af6dca00ef0b8cdc83192796ab0))
* stop spurious 409 on Test Session from leaked viability probe lock ([1a0731b](https://github.com/quidow/gridfleet/commit/1a0731b5341c669570e903129487cb94bbbc5e94))

## [0.2.0](https://github.com/quidow/gridfleet/compare/gridfleet-frontend-v0.1.0...gridfleet-frontend-v0.2.0) (2026-05-25)


### ⚠ BREAKING CHANGES

* **backend:** web terminal endpoints, settings, and table removed.
* **frontend:** web terminal UI removed.

### Features

* backend-driven event severity ([#263](https://github.com/quidow/gridfleet/issues/263)) ([b6bd4e4](https://github.com/quidow/gridfleet/commit/b6bd4e4f1f9315edc98a7fedd3f8cb721adbdd98))
* **backend,agent,frontend:** split Fire OS display version from routing major ([a289455](https://github.com/quidow/gridfleet/commit/a2894559f41bd15b3a6a60e593021d1b2049d778))
* **backend,frontend:** per-host tool environment configuration ([#373](https://github.com/quidow/gridfleet/issues/373)) ([b11a489](https://github.com/quidow/gridfleet/commit/b11a4898dcd61b3f76d88e4a2f232d73cf38ca2a))
* **backend:** add device diagnostic export ([#287](https://github.com/quidow/gridfleet/issues/287)) ([6afb0d1](https://github.com/quidow/gridfleet/commit/6afb0d1c8486c642688fe0ad952d2575491dfed0))
* **backend:** close fastapi best-practices gaps ([c47cd2e](https://github.com/quidow/gridfleet/commit/c47cd2ea6dca49cf0708153a865fea1f9ecaed18))
* **backend:** document api error responses ([94e3a67](https://github.com/quidow/gridfleet/commit/94e3a675463f62ca4fda24196b569afac60b511f))
* consolidate device status panel with maintenance reasons and simplified health pills ([b259fff](https://github.com/quidow/gridfleet/commit/b259fff4b17baa508396ddc9997788d4b87697cf))
* data-driven driver pack tool dependencies on host overview ([225adc5](https://github.com/quidow/gridfleet/commit/225adc56c475f5ef606b1cacfb61c9715cee57d7))
* device config export/import + read-only inventory snapshot ([325b222](https://github.com/quidow/gridfleet/commit/325b222b2f90b1933a164c06e3940ae4c25be129))
* **frontend:** add device config export button to devices page ([8fe4ff1](https://github.com/quidow/gridfleet/commit/8fe4ff1685503a9da9fccb15fc2cb0efd102ce96))
* **frontend:** add device import and inventory mocked e2e ([650b513](https://github.com/quidow/gridfleet/commit/650b5131a5546493473accba20b9398ea6256de8))
* **frontend:** add device import results step ([49023f7](https://github.com/quidow/gridfleet/commit/49023f74d028aa2b092ce598214dadb9c6636a65))
* **frontend:** add device import review and map step ([fb55d4c](https://github.com/quidow/gridfleet/commit/fb55d4c9894dd723d662f9a334c1abe8ab029e83))
* **frontend:** add device import upload step ([58fb485](https://github.com/quidow/gridfleet/commit/58fb485f8362cf75217f95bd4780ba3d7e6916bb))
* **frontend:** add device import wizard route and entry point ([4af9bb5](https://github.com/quidow/gridfleet/commit/4af9bb5808baa6848925e5b27ae888f30e719dbd))
* **frontend:** add device import wizard state machine hook ([5fc89f1](https://github.com/quidow/gridfleet/commit/5fc89f111a636f9f1811067a6282f313a726f88e))
* **frontend:** add device inventory export modal ([75c8655](https://github.com/quidow/gridfleet/commit/75c8655d4888fd229240dd6d65e4680d104b559e))
* **frontend:** add devices portability and inventory api clients ([9057562](https://github.com/quidow/gridfleet/commit/9057562c574c228e8cddb596cb77aa3b9b84a2ad))
* **frontend:** add Doctor and Actions columns to host Drivers tab ([407e545](https://github.com/quidow/gridfleet/commit/407e545aa1488b980068a9ee36c0e927902d5103))
* **frontend:** add host agent log panel ([69e5592](https://github.com/quidow/gridfleet/commit/69e55929e7a28f63431d6351517713f0f1996098))
* **frontend:** add host events panel ([a73ad05](https://github.com/quidow/gridfleet/commit/a73ad055ff4966b16c7c317dc565498b85e90312))
* **frontend:** add host log api client and hooks ([64511fe](https://github.com/quidow/gridfleet/commit/64511fe96df00021e17130e5da665f3e40bd0ba4))
* **frontend:** add host logs tab ([f21de6a](https://github.com/quidow/gridfleet/commit/f21de6af1964224f6f4bac132297829e3a5e9426))
* **frontend:** add inner tabs for host logs ([70596d7](https://github.com/quidow/gridfleet/commit/70596d78ef00a2f4150e9b1f9db34bb8d8c9a2c3))
* **frontend:** add logs tab to host detail ([41f0cbd](https://github.com/quidow/gridfleet/commit/41f0cbd00c92648f17af3b4726bbcda0a63699fb))
* **frontend:** add sessions tab to device detail page ([#386](https://github.com/quidow/gridfleet/issues/386)) ([6b93c7b](https://github.com/quidow/gridfleet/commit/6b93c7be0140d50bbf40e9995c188d61fc515a44))
* **frontend:** add severities param to notifications API client ([3766bc2](https://github.com/quidow/gridfleet/commit/3766bc2979fb26eeda3728af81e21c6d88132ebb))
* **frontend:** add short tooltip to maintenance/offline badges in device list ([844a9f5](https://github.com/quidow/gridfleet/commit/844a9f5a8c39de70149e2f9d10c5dbcf5797e9bd))
* **frontend:** cover host logs tab in e2e ([83c51be](https://github.com/quidow/gridfleet/commit/83c51beba6bc981934efa7f02318815a4d4d4ad3))
* **frontend:** data-driven tool versions grouped by driver pack ([81f9dae](https://github.com/quidow/gridfleet/commit/81f9daef26332881cc09e55875edb2109ce8755e))
* **frontend:** e2e coverage for severity chip filter ([1b46b8a](https://github.com/quidow/gridfleet/commit/1b46b8a8b57484c7f26a1a3dc1f33180d37386d2))
* **frontend:** enhance triage card with maintenance actions and reason evidence ([a60b887](https://github.com/quidow/gridfleet/commit/a60b8877c2c8ee73ebef074f9bd7a66985a261ae))
* **frontend:** forward devices page filters to inventory export ([#352](https://github.com/quidow/gridfleet/issues/352)) ([e49d792](https://github.com/quidow/gridfleet/commit/e49d792b81a220648f4d6cca252e79db20e91bac))
* **frontend:** migrate query errors to boundary handling ([#361](https://github.com/quidow/gridfleet/issues/361)) ([70d56e1](https://github.com/quidow/gridfleet/commit/70d56e1ed9582bf91abc81cc9d850a29d9c71e90))
* **frontend:** move device import/export to Settings Backup & Restore tab ([#355](https://github.com/quidow/gridfleet/issues/355)) ([31f2c2a](https://github.com/quidow/gridfleet/commit/31f2c2a6b2cc7bc278cbf26cfb98eab9b3c6ea80))
* **frontend:** regenerate openapi types for device export and import endpoints ([f1784d0](https://github.com/quidow/gridfleet/commit/f1784d03d52ba2a8935fa14c4b5d9e7a41624183))
* **frontend:** regenerate openapi types for fleet capacity has_data ([2737543](https://github.com/quidow/gridfleet/commit/2737543f3eb331680b7cb3b9b61d731b1b5e2782))
* **frontend:** regenerate openapi types for host logs ([7be1f05](https://github.com/quidow/gridfleet/commit/7be1f05bd810b99b603aa60e2314aa5394864622))
* **frontend:** regenerate OpenAPI types with severity query param ([b041c08](https://github.com/quidow/gridfleet/commit/b041c0861f7e2d1f37ddbfc413e05bd02b38a6e7))
* **frontend:** set explicit refetchInterval on server-state hooks ([#328](https://github.com/quidow/gridfleet/issues/328)) ([d06be16](https://github.com/quidow/gridfleet/commit/d06be16c78a65d11353d223daa598c6b8f45deee))
* **frontend:** severity chip filter on Notifications page ([af00b59](https://github.com/quidow/gridfleet/commit/af00b59b4bb9a7f1dedb16c74fe382f4e614d611))
* **frontend:** show fire os display version in devices table ([315cdbc](https://github.com/quidow/gridfleet/commit/315cdbc6762602232167be63879e82504ef488bb))
* **frontend:** show host hardware metadata on overview tab ([bc118ef](https://github.com/quidow/gridfleet/commit/bc118ef464d5cb09807e9689cfdc8ed0d3f7314f))
* **frontend:** show memory/disk totals in host overview resource strip ([be5f912](https://github.com/quidow/gridfleet/commit/be5f9129a2d9e1b2ea62c827286778b068174b11))
* **frontend:** simplify health pills to one-word status indicators ([0944163](https://github.com/quidow/gridfleet/commit/0944163cba40f9361b23878de3996187d0fc6062))
* **frontend:** surface device review_required in triage and health badges ([8501c77](https://github.com/quidow/gridfleet/commit/8501c7785ccf30602bec76cc38b1ec9f0954dffd))
* **frontend:** wire /api/grid/queue into Sessions and Run Detail pages ([#390](https://github.com/quidow/gridfleet/issues/390)) ([859e4bc](https://github.com/quidow/gridfleet/commit/859e4bc913dd8382a9840a4879b6e336e70aaf25))
* **frontend:** wire driver pack policy editor and release deletion ([#395](https://github.com/quidow/gridfleet/issues/395)) ([4a47bb0](https://github.com/quidow/gridfleet/commit/4a47bb084b6e806063d4d9b957b716eb08472d46))
* layered triage card with state-specific panels ([#358](https://github.com/quidow/gridfleet/issues/358)) ([b95b25e](https://github.com/quidow/gridfleet/commit/b95b25eb866b6acc881b70a71d947f6aa09bdae3))
* on-demand Appium doctor checks ([221256d](https://github.com/quidow/gridfleet/commit/221256d16983974a0324774d85a6cfca99c78ee7))
* severity filter on Notifications ([a173b4b](https://github.com/quidow/gridfleet/commit/a173b4b1cf93b6ab40cb277053d1f5fa2ced5549))
* show probe sessions on Sessions page (opt-in, no analytics impact) ([#246](https://github.com/quidow/gridfleet/issues/246)) ([6e2db59](https://github.com/quidow/gridfleet/commit/6e2db595f42f361b0e3d78d83bf7ff15203c6397))
* surface host hardware metadata on host detail ([de6116e](https://github.com/quidow/gridfleet/commit/de6116e5155d80eee9e868cbc69c56cdfa027afa))


### Bug Fixes

* **agent:** address code review feedback ([c6be98c](https://github.com/quidow/gridfleet/commit/c6be98c7ff212e37076af03a276fef48b0a02793))
* **agent:** use human-readable display names for host tools ([67f48be](https://github.com/quidow/gridfleet/commit/67f48bed99faec5f142cf2cba23edebad9db6a69))
* **backend:** add doctor route tests, update OpenAPI surface baseline and types ([eb939c0](https://github.com/quidow/gridfleet/commit/eb939c0704ecf3c18b54dd2d394426e9b1d12435))
* **backend:** remove unused PUT config and POST refresh device endpoints ([#388](https://github.com/quidow/gridfleet/issues/388)) ([bbd1cd9](https://github.com/quidow/gridfleet/commit/bbd1cd9edf92110e6c02d8f3fb8cd400f2e05a89))
* fleet health history aggregation and gap rendering ([cc745aa](https://github.com/quidow/gridfleet/commit/cc745aaf8856e08afe97bf5edf1fb7625a50b520))
* **frontend:** add Cache-Control headers to nginx config ([3cb6ce3](https://github.com/quidow/gridfleet/commit/3cb6ce33767d56a13fcd1f5392755536c13a8f2f))
* **frontend:** add explicit polling tiers to hooks missing refetchInterval ([9cf9ce1](https://github.com/quidow/gridfleet/commit/9cf9ce1e4b452b8e9ea2726f63679d7a868d23af))
* **frontend:** add missing staleTime to all polling query hooks ([64e72b3](https://github.com/quidow/gridfleet/commit/64e72b3e5471fa5d43a28d7614d1b1592f6393d5))
* **frontend:** add os_version_display to DeviceSortKey union ([aa322bc](https://github.com/quidow/gridfleet/commit/aa322bc0c1e8e422964dcd60e268d466789d9051))
* **frontend:** add SSE-adaptive polling to all server-state hooks ([#366](https://github.com/quidow/gridfleet/issues/366)) ([f1b0405](https://github.com/quidow/gridfleet/commit/f1b0405f31d5a907d16fc094ed4a2224e624d5d6))
* **frontend:** align devices e2e with os_version_display filter key ([d9a3c91](https://github.com/quidow/gridfleet/commit/d9a3c91e9d4b9ea9bde5c832c0ae21342465dbf7))
* **frontend:** align favicon with sidebar app mark ([4a44f53](https://github.com/quidow/gridfleet/commit/4a44f53966c9a75e863786044d0e0f3d4e00b9b7))
* **frontend:** align FleetByPlatformCard timeline mock field names with schema ([3e27f48](https://github.com/quidow/gridfleet/commit/3e27f48d98a5b72ba1a80e861d22ef712f64389c))
* **frontend:** align host logs panels with backend contract ([9a25adb](https://github.com/quidow/gridfleet/commit/9a25adbf554dd3dfc08779c739bd19da33164957))
* **frontend:** align host resource gauge bars and add CPU busy-cores detail ([#353](https://github.com/quidow/gridfleet/issues/353)) ([0d0acba](https://github.com/quidow/gridfleet/commit/0d0acbac9d21c0b39b03efd7154375eb2d112cb2))
* **frontend:** anchor single-point fleet health dot to right edge ([5125c0c](https://github.com/quidow/gridfleet/commit/5125c0cf3b029cacf3413a3f23f3747f40b3b6fe))
* **frontend:** auto-save tool env variable deletion with confirmation ([#392](https://github.com/quidow/gridfleet/issues/392)) ([a5e95a3](https://github.com/quidow/gridfleet/commit/a5e95a3dadd3bc0b24646c3421b270c7fc810914))
* **frontend:** clean up unused restartNode import and fix AvailabilityCell test ([b18760f](https://github.com/quidow/gridfleet/commit/b18760f31337f7d8b2ec6f9a6f90e5472ade3f0b))
* **frontend:** close out audit deferred items ([#405](https://github.com/quidow/gridfleet/issues/405)) ([56cd08f](https://github.com/quidow/gridfleet/commit/56cd08f3e0d4ec44f352506a36a8d914409fa9b7))
* **frontend:** drive fleet capacity chart gaps off has_data ([2d52bf8](https://github.com/quidow/gridfleet/commit/2d52bf8fe45cfb71493e289883072a284dbf5a3c))
* **frontend:** drop bare JSX.Element return type annotation ([d7143db](https://github.com/quidow/gridfleet/commit/d7143dbfff71ad55ebf67a695bb0c65c38f2426f))
* **frontend:** enable color-contrast a11y and fix WCAG AA violations ([#401](https://github.com/quidow/gridfleet/issues/401)) ([a2fc5a8](https://github.com/quidow/gridfleet/commit/a2fc5a8d049e5431a608959250ec4c3165959f48))
* **frontend:** exclude synthetic fleet capacity rows from dashboard sparklines ([c1b6790](https://github.com/quidow/gridfleet/commit/c1b679053dfb70d063f7343f685de6a1a4814177))
* **frontend:** remove reference to deleted LifecycleActionOut.label field ([ff7fe0e](https://github.com/quidow/gridfleet/commit/ff7fe0e27eed749b3d5e85a15dd09c43714c3bb0))
* **frontend:** remove reference to deleted required_for_discovery field ([5f138f9](https://github.com/quidow/gridfleet/commit/5f138f92758ee4fa3649ead5f037e7fdd48e0613))
* **frontend:** remove unused pendingLabel and waitForNextPaint after auto_manage removal ([e7534fa](https://github.com/quidow/gridfleet/commit/e7534fa0054c7d8fd5a396c0566e0d84047a840c))
* **frontend:** render fleet health gaps and exclude them from average ([4f9f358](https://github.com/quidow/gridfleet/commit/4f9f358e040dc6d735ebc41cd4f793c06a2fd368))
* **frontend:** replace numeric tailwind color utilities with design tokens in import steps ([c826d8a](https://github.com/quidow/gridfleet/commit/c826d8abc15c47f75b6d370665e7b9332f9b7942))
* **frontend:** restore --max-height-modal token used by Modal component ([6377bda](https://github.com/quidow/gridfleet/commit/6377bdaab240b4aae5e12a9dc16db7383b354d89))
* **frontend:** seed FleetByPlatformCard timeline mock with has_data ([032296c](https://github.com/quidow/gridfleet/commit/032296c16ddcd2dd7fa90c087cba44232c0b0b2e))
* **frontend:** stack detail under percent in host resource gauge ([#343](https://github.com/quidow/gridfleet/issues/343)) ([75a5c84](https://github.com/quidow/gridfleet/commit/75a5c8407255486e0204c768686912e44fdd82f5))
* **frontend:** tighten empty-glyph assertion and document resource-strip fallback ([a3eee2f](https://github.com/quidow/gridfleet/commit/a3eee2f12d6e1406b1639ed8283d9cfe81dde737))
* **frontend:** update e2e test for simplified connectivity pill label ([db16a84](https://github.com/quidow/gridfleet/commit/db16a842e5aed8c021cd41da4e29fc858ceb472e))
* **frontend:** update e2e tests for triage heading clash and removed drivers tab ([9e5d62a](https://github.com/quidow/gridfleet/commit/9e5d62a7ac937ee6cfe73e74bf4239187cc75e61))
* **frontend:** update HostDriversPanel tests for doctor columns ([4fdc4bb](https://github.com/quidow/gridfleet/commit/4fdc4bbf19316f5856a8e014ef4482f66d5780ec))
* **frontend:** use dedicated maintenance_reason field instead of overloaded detail ([#357](https://github.com/quidow/gridfleet/issues/357)) ([5adc5f1](https://github.com/quidow/gridfleet/commit/5adc5f172f23be36d0d1e0d88d1d480b23e8af20))
* **frontend:** use design tokens for host agent log level stripe ([4521e47](https://github.com/quidow/gridfleet/commit/4521e47ebed4f6247f15bcc2f6ef1adfaa6ad818))
* **frontend:** use generated ToolEntry type to match optional version field ([46641b4](https://github.com/quidow/gridfleet/commit/46641b4a163f7f71b1be3c02a194f298b9034293))
* **frontend:** use os_version_display fallback in device detail ([#267](https://github.com/quidow/gridfleet/issues/267)) ([ca9e25e](https://github.com/quidow/gridfleet/commit/ca9e25e06df593dc41d9597cbca7101afb1353de))
* **frontend:** wrap host detail overview and logs tabs in error boundaries ([#378](https://github.com/quidow/gridfleet/issues/378)) ([fa3e014](https://github.com/quidow/gridfleet/commit/fa3e014231ae484572ff426c9e14398fcbc34c36))
* **main:** address code review feedback ([701fd5c](https://github.com/quidow/gridfleet/commit/701fd5c1d85e792f766efc188c927bf919fc66bf))
* **main:** drop ios_booted health check on tvos real devices ([da39468](https://github.com/quidow/gridfleet/commit/da39468715e0c619f93da04ab5a4e4b44db34df1))


### Performance Improvements

* **frontend:** defer recharts via React.lazy on chart panels ([#330](https://github.com/quidow/gridfleet/issues/330)) ([1fbc21d](https://github.com/quidow/gridfleet/commit/1fbc21dc3bd47e085f52774a1549acd3b3b64711))
* **frontend:** split heavy deps into dedicated Vite chunks ([8fafa3b](https://github.com/quidow/gridfleet/commit/8fafa3bd3665520f64855c9c48b5fb0110b6ae57))


### Code Refactoring

* **backend:** remove web terminal feature ([7b4ce2b](https://github.com/quidow/gridfleet/commit/7b4ce2befec37ccca3674254f9e4f19510ca9a73))
* **frontend:** remove web terminal panel ([2a7b6ae](https://github.com/quidow/gridfleet/commit/2a7b6aec4a2baadaf4fba00941655e058430d693))

## 0.1.0 — Initial Public Preview

- Initial public preview of the GridFleet operator UI.
- React 19 + TypeScript + Vite + Tailwind v4 dashboard with real-time device, session, and fleet views.
