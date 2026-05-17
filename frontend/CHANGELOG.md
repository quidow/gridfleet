# Changelog — GridFleet Frontend

All notable changes to the GridFleet operator dashboard (React + TypeScript + Vite) are documented here.

## Unreleased

### Features

- Show "Update available" badge and notice panel on host cards when the backend reports a newer recommended agent version.

### Fixes

- Run frontend nginx container as non-root user.

## [0.2.0](https://github.com/quidow/gridfleet/compare/gridfleet-frontend-v0.1.0...gridfleet-frontend-v0.2.0) (2026-05-17)


### ⚠ BREAKING CHANGES

* **backend:** unify verification node lifecycle ([#187](https://github.com/quidow/gridfleet/issues/187))
* **backend:** appium desired-state phase 6 — final cleanup ([#179](https://github.com/quidow/gridfleet/issues/179))
* **backend:** drop appium node state column ([#170](https://github.com/quidow/gridfleet/issues/170))
* **backend:** clients sending {drain: true|false} to /api/devices/ {id}/maintenance, /api/devices/bulk/enter-maintenance, or the group bulk equivalent must drop the field. The enter-maintenance behaviour is unchanged from drain=false (always stop the node).
* remove device_config secret masking ([#104](https://github.com/quidow/gridfleet/issues/104))
* **backend:** split device availability_status into operational_state + hold ([#87](https://github.com/quidow/gridfleet/issues/87))

### Features

* backend-driven event severity ([#263](https://github.com/quidow/gridfleet/issues/263)) ([b6bd4e4](https://github.com/quidow/gridfleet/commit/b6bd4e4f1f9315edc98a7fedd3f8cb721adbdd98))
* **backend,agent,frontend:** split Fire OS display version from routing major ([a289455](https://github.com/quidow/gridfleet/commit/a2894559f41bd15b3a6a60e593021d1b2049d778))
* **backend,testkit:** recreate run device cooldown api ([fccfbc7](https://github.com/quidow/gridfleet/commit/fccfbc7bcf694f8c59cbaa394bb075d20e1b34f0))
* **backend:** add appium node desired-state schema ([#163](https://github.com/quidow/gridfleet/issues/163)) ([b64ee2e](https://github.com/quidow/gridfleet/commit/b64ee2e9616b95a6d334dd5bcddbaa2432a1763c))
* **backend:** close fastapi best-practices gaps ([c47cd2e](https://github.com/quidow/gridfleet/commit/c47cd2ea6dca49cf0708153a865fea1f9ecaed18))
* **backend:** converge appium desired state via reconciler ([70bc7a8](https://github.com/quidow/gridfleet/commit/70bc7a8dc25ed57e2d4858ae88215ea0788152ab))
* **backend:** converge appium desired state via reconciler ([4ca1558](https://github.com/quidow/gridfleet/commit/4ca15584c0a62fd96bb7f732d90cab53f0ae1c66))
* **backend:** delete domain layout shims ([#234](https://github.com/quidow/gridfleet/issues/234)) ([a885a12](https://github.com/quidow/gridfleet/commit/a885a1272d6e54e931a83e354e1bce4dee784209))
* **backend:** device state model drift fixes (D1-D6) ([#144](https://github.com/quidow/gridfleet/issues/144)) ([09556fd](https://github.com/quidow/gridfleet/commit/09556fdac8ddb458f1655f9001f25240443062fb))
* **backend:** document api error responses ([94e3a67](https://github.com/quidow/gridfleet/commit/94e3a675463f62ca4fda24196b569afac60b511f))
* **backend:** drop appium node state column ([#170](https://github.com/quidow/gridfleet/issues/170)) ([d0337d6](https://github.com/quidow/gridfleet/commit/d0337d6b616f4b9134c93cfc2841cc96ae61dfa2))
* **backend:** dual-write appium desired-state writers ([#164](https://github.com/quidow/gridfleet/issues/164)) ([160dc5a](https://github.com/quidow/gridfleet/commit/160dc5a2788ffe2ede98776924347b184d332bbe))
* **backend:** escalate device to maintenance after N cooldowns in same run ([#121](https://github.com/quidow/gridfleet/issues/121)) ([7fe01f7](https://github.com/quidow/gridfleet/commit/7fe01f768ff70cd3ddb7f26aec1ab7210b49987f))
* **backend:** richer allocation payload with include=config,capabilities ([#94](https://github.com/quidow/gridfleet/issues/94)) ([4b44bad](https://github.com/quidow/gridfleet/commit/4b44badb15bb2d679202f006a5272c56d7d186f2))
* **backend:** unify verification node lifecycle ([#187](https://github.com/quidow/gridfleet/issues/187)) ([1d1e7d8](https://github.com/quidow/gridfleet/commit/1d1e7d8b3216f3244a3ab6b40f5d324d561c0f41))
* **frontend:** add devicetestdataeditor panel ([f82f979](https://github.com/quidow/gridfleet/commit/f82f9794553b2d958b31440b59565d77c4094d55))
* **frontend:** add host agent log panel ([69e5592](https://github.com/quidow/gridfleet/commit/69e55929e7a28f63431d6351517713f0f1996098))
* **frontend:** add host events panel ([a73ad05](https://github.com/quidow/gridfleet/commit/a73ad055ff4966b16c7c317dc565498b85e90312))
* **frontend:** add host log api client and hooks ([64511fe](https://github.com/quidow/gridfleet/commit/64511fe96df00021e17130e5da665f3e40bd0ba4))
* **frontend:** add host logs tab ([f21de6a](https://github.com/quidow/gridfleet/commit/f21de6af1964224f6f4bac132297829e3a5e9426))
* **frontend:** add inner tabs for host logs ([70596d7](https://github.com/quidow/gridfleet/commit/70596d78ef00a2f4150e9b1f9db34bb8d8c9a2c3))
* **frontend:** add logs tab to host detail ([41f0cbd](https://github.com/quidow/gridfleet/commit/41f0cbd00c92648f17af3b4726bbcda0a63699fb))
* **frontend:** add severities param to notifications API client ([3766bc2](https://github.com/quidow/gridfleet/commit/3766bc2979fb26eeda3728af81e21c6d88132ebb))
* **frontend:** add test_data api client methods and types ([b74c480](https://github.com/quidow/gridfleet/commit/b74c48028360d108af7f83d388f0a1aff3fa3988))
* **frontend:** add usedevicetestdata and mutation hooks ([d38de08](https://github.com/quidow/gridfleet/commit/d38de089b681dd6c9b63af049e85da1cad76867c))
* **frontend:** cover host logs tab in e2e ([83c51be](https://github.com/quidow/gridfleet/commit/83c51beba6bc981934efa7f02318815a4d4d4ad3))
* **frontend:** derive types from backend openapi schema ([#162](https://github.com/quidow/gridfleet/issues/162)) ([80be9a7](https://github.com/quidow/gridfleet/commit/80be9a7f272c311287fd6537d422e50a306baa0b))
* **frontend:** e2e coverage for severity chip filter ([1b46b8a](https://github.com/quidow/gridfleet/commit/1b46b8a8b57484c7f26a1a3dc1f33180d37386d2))
* **frontend:** mount devicetestdataeditor on device detail page ([8caf3d2](https://github.com/quidow/gridfleet/commit/8caf3d27114a505ce413c994a9ec1452014e94ae))
* **frontend:** move host tool versions to overview ([32ff0c9](https://github.com/quidow/gridfleet/commit/32ff0c9a944402d9bf672fa2bf76ec371d815bce))
* **frontend:** regenerate openapi types for fleet capacity has_data ([2737543](https://github.com/quidow/gridfleet/commit/2737543f3eb331680b7cb3b9b61d731b1b5e2782))
* **frontend:** regenerate openapi types for host logs ([7be1f05](https://github.com/quidow/gridfleet/commit/7be1f05bd810b99b603aa60e2314aa5394864622))
* **frontend:** regenerate openapi types with cooldown endpoint ([1bb8ff3](https://github.com/quidow/gridfleet/commit/1bb8ff3af7def9fe59c04c43b4a024eb43257f96))
* **frontend:** regenerate OpenAPI types with severity query param ([b041c08](https://github.com/quidow/gridfleet/commit/b041c0861f7e2d1f37ddbfc413e05bd02b38a6e7))
* **frontend:** remove ready run state from dashboard ([059eac9](https://github.com/quidow/gridfleet/commit/059eac9406dd36ae905aaf41f10ba1aee2576256))
* **frontend:** remove tool ensure ui and update tool status panel ([d33fad0](https://github.com/quidow/gridfleet/commit/d33fad0f4cd7e64b5fba6cc11137af7585293636))
* **frontend:** severity chip filter on Notifications page ([af00b59](https://github.com/quidow/gridfleet/commit/af00b59b4bb9a7f1dedb16c74fe382f4e614d611))
* **frontend:** show fire os display version in devices table ([315cdbc](https://github.com/quidow/gridfleet/commit/315cdbc6762602232167be63879e82504ef488bb))
* **frontend:** show host hardware metadata on overview tab ([bc118ef](https://github.com/quidow/gridfleet/commit/bc118ef464d5cb09807e9689cfdc8ed0d3f7314f))
* **frontend:** show memory/disk totals in host overview resource strip ([be5f912](https://github.com/quidow/gridfleet/commit/be5f9129a2d9e1b2ea62c827286778b068174b11))
* **frontend:** surface device review_required in triage and health badges ([8501c77](https://github.com/quidow/gridfleet/commit/8501c7785ccf30602bec76cc38b1ec9f0954dffd))
* **frontend:** wire test_data.updated to event stream and registry ([ee8b7d7](https://github.com/quidow/gridfleet/commit/ee8b7d7222fbd7c2710940a90a3f44d5fb272994))
* **main:** add device orchestration intent registry ([b42b3d4](https://github.com/quidow/gridfleet/commit/b42b3d47e96e1ee1257bdd6f7676f027eed6de57))
* **main:** add optional icmp ping health check for usb devices with saved ip ([#143](https://github.com/quidow/gridfleet/issues/143)) ([afda5ce](https://github.com/quidow/gridfleet/commit/afda5ce5527167bcd47cb04f227a791ab3cdea1b))
* **main:** split device test_data from device_config + modal portal ([b5d0fa0](https://github.com/quidow/gridfleet/commit/b5d0fa09a862af742b3a2462667a86b1d3a867b6))
* remove host tool ensure/version management ([#190](https://github.com/quidow/gridfleet/issues/190)) ([b2562c1](https://github.com/quidow/gridfleet/commit/b2562c16d75ef14c0f4c9131c03151b73f337802))
* severity filter on Notifications ([a173b4b](https://github.com/quidow/gridfleet/commit/a173b4b1cf93b6ab40cb277053d1f5fa2ced5549))
* show probe sessions on Sessions page (opt-in, no analytics impact) ([#246](https://github.com/quidow/gridfleet/issues/246)) ([6e2db59](https://github.com/quidow/gridfleet/commit/6e2db595f42f361b0e3d78d83bf7ff15203c6397))
* surface host hardware metadata on host detail ([de6116e](https://github.com/quidow/gridfleet/commit/de6116e5155d80eee9e868cbc69c56cdfa027afa))
* **testkit:** support tag-based device targeting ([db0d0e3](https://github.com/quidow/gridfleet/commit/db0d0e3d3d1231828bb22a707d3bdcab6c0ec717))


### Bug Fixes

* **agent:** release adapter-owned doctor refactor ([#165](https://github.com/quidow/gridfleet/issues/165)) ([f3ae257](https://github.com/quidow/gridfleet/commit/f3ae25787e2c8ef926312f11d2313c6513f8bfa9))
* **backend:** route verification_passed to available, not offline ([#189](https://github.com/quidow/gridfleet/issues/189)) ([a0ddd6a](https://github.com/quidow/gridfleet/commit/a0ddd6a431d88de3ee0cc3d8d122a89cda528757))
* fleet health history aggregation and gap rendering ([cc745aa](https://github.com/quidow/gridfleet/commit/cc745aaf8856e08afe97bf5edf1fb7625a50b520))
* **frontend,backend:** show busy chip when reserved device runs session ([#134](https://github.com/quidow/gridfleet/issues/134)) ([04c62f4](https://github.com/quidow/gridfleet/commit/04c62f44287c321b6058212107cbefd73a473497))
* **frontend:** add Cache-Control headers to nginx config ([3cb6ce3](https://github.com/quidow/gridfleet/commit/3cb6ce33767d56a13fcd1f5392755536c13a8f2f))
* **frontend:** add os_version_display to DeviceSortKey union ([aa322bc](https://github.com/quidow/gridfleet/commit/aa322bc0c1e8e422964dcd60e268d466789d9051))
* **frontend:** alias decimal.js-light to esm build ([#131](https://github.com/quidow/gridfleet/issues/131)) ([cb59434](https://github.com/quidow/gridfleet/commit/cb59434cf8259a6e8e3705ed80c29e8a0f7be238))
* **frontend:** align devices e2e with os_version_display filter key ([d9a3c91](https://github.com/quidow/gridfleet/commit/d9a3c91e9d4b9ea9bde5c832c0ae21342465dbf7))
* **frontend:** align favicon with sidebar app mark ([4a44f53](https://github.com/quidow/gridfleet/commit/4a44f53966c9a75e863786044d0e0f3d4e00b9b7))
* **frontend:** align FleetByPlatformCard timeline mock field names with schema ([3e27f48](https://github.com/quidow/gridfleet/commit/3e27f48d98a5b72ba1a80e861d22ef712f64389c))
* **frontend:** align host logs panels with backend contract ([9a25adb](https://github.com/quidow/gridfleet/commit/9a25adbf554dd3dfc08779c739bd19da33164957))
* **frontend:** anchor single-point fleet health dot to right edge ([5125c0c](https://github.com/quidow/gridfleet/commit/5125c0cf3b029cacf3413a3f23f3747f40b3b6fe))
* **frontend:** drive fleet capacity chart gaps off has_data ([2d52bf8](https://github.com/quidow/gridfleet/commit/2d52bf8fe45cfb71493e289883072a284dbf5a3c))
* **frontend:** exclude synthetic fleet capacity rows from dashboard sparklines ([c1b6790](https://github.com/quidow/gridfleet/commit/c1b679053dfb70d063f7343f685de6a1a4814177))
* **frontend:** portal modal to document.body to escape ancestor clipping ([8c52416](https://github.com/quidow/gridfleet/commit/8c524160f6d1d315d294e0496d902bdb81e72f21))
* **frontend:** render fleet health gaps and exclude them from average ([4f9f358](https://github.com/quidow/gridfleet/commit/4f9f358e040dc6d735ebc41cd4f793c06a2fd368))
* **frontend:** replace any with typed mocks in editor tests ([9d6985f](https://github.com/quidow/gridfleet/commit/9d6985f4ed472510c1e6fb9ec877e53621780d2e))
* **frontend:** seed FleetByPlatformCard timeline mock with has_data ([032296c](https://github.com/quidow/gridfleet/commit/032296c16ddcd2dd7fa90c087cba44232c0b0b2e))
* **frontend:** show failing health summary details ([0622649](https://github.com/quidow/gridfleet/commit/06226498534bbbda7f0860e840757214e0859f94))
* **frontend:** surface appium convergence failures ([7b4d491](https://github.com/quidow/gridfleet/commit/7b4d491196ce0b1bcc2336b43c504f2075880715))
* **frontend:** tighten empty-glyph assertion and document resource-strip fallback ([a3eee2f](https://github.com/quidow/gridfleet/commit/a3eee2f12d6e1406b1639ed8283d9cfe81dde737))
* **frontend:** update generated openapi types ([34c92b2](https://github.com/quidow/gridfleet/commit/34c92b2a65aeb79c93d1fe37df390903370fc2ee))
* **frontend:** update legacy appium prerequisite copy ([1ae9d17](https://github.com/quidow/gridfleet/commit/1ae9d172a99c5b7268b5913c2fb43db4e68ca857))
* **frontend:** update verification stage label from 'start temporary node' to 'start appium node' ([a0ddd6a](https://github.com/quidow/gridfleet/commit/a0ddd6a431d88de3ee0cc3d8d122a89cda528757))
* **frontend:** use design tokens for host agent log level stripe ([4521e47](https://github.com/quidow/gridfleet/commit/4521e47ebed4f6247f15bcc2f6ef1adfaa6ad818))
* **frontend:** use os_version_display fallback in device detail ([#267](https://github.com/quidow/gridfleet/issues/267)) ([ca9e25e](https://github.com/quidow/gridfleet/commit/ca9e25e06df593dc41d9597cbca7101afb1353de))
* **frontend:** validate test_data root and surface mutation errors ([7def72a](https://github.com/quidow/gridfleet/commit/7def72a36243f4636fd6e6a42caf43c114dbe43a))


### Performance Improvements

* **frontend:** split heavy deps into dedicated Vite chunks ([8fafa3b](https://github.com/quidow/gridfleet/commit/8fafa3bd3665520f64855c9c48b5fb0110b6ae57))


### Code Refactoring

* **backend:** appium desired-state phase 6 — final cleanup ([#179](https://github.com/quidow/gridfleet/issues/179)) ([c97ae99](https://github.com/quidow/gridfleet/commit/c97ae99974024e036a2fd7d2233442f70ff18fcb))
* **backend:** split device availability_status into operational_state + hold ([#87](https://github.com/quidow/gridfleet/issues/87)) ([1b329d3](https://github.com/quidow/gridfleet/commit/1b329d39c77c3a8594c8158a1a29ab8bd257a124))
* remove device_config secret masking ([#104](https://github.com/quidow/gridfleet/issues/104)) ([7329a31](https://github.com/quidow/gridfleet/commit/7329a3107814f653b81b2753e519e271ec0dd8bd))

## 0.1.0 — Initial Public Preview

- Initial public preview of the GridFleet operator UI.
- React 19 + TypeScript + Vite + Tailwind v4 dashboard with real-time device, session, and fleet views.
