# Changelog — GridFleet Testkit

All notable changes to the GridFleet testkit (`gridfleet-testkit` on PyPI) are documented here.

## [0.9.1](https://github.com/quidow/gridfleet/compare/gridfleet-testkit-v0.9.0...gridfleet-testkit-v0.9.1) (2026-05-13)


### Bug Fixes

* **testkit:** align release policy with commitlint ([5dd8220](https://github.com/quidow/gridfleet/commit/5dd822010460e994f2e3c5b6676a69bed05678ed))

## [0.9.0](https://github.com/quidow/gridfleet/compare/gridfleet-testkit-v0.8.0...gridfleet-testkit-v0.9.0) (2026-05-12)


### Features

* **testkit:** add run detail client helper ([c80a8cc](https://github.com/quidow/gridfleet/commit/c80a8cc95093c2f46a7e714c96ff0b33018af5ba))
* **testkit:** expose allocation device tags ([0bf5e2e](https://github.com/quidow/gridfleet/commit/0bf5e2e04b806fadd0afcd3c95073828d0c2414e))
* **testkit:** support tag-based device targeting ([db0d0e3](https://github.com/quidow/gridfleet/commit/db0d0e3d3d1231828bb22a707d3bdcab6c0ec717))


### Documentation

* **testkit:** document tag-based device targeting ([096841b](https://github.com/quidow/gridfleet/commit/096841b737dec71524d0edfa4c538d9cc69e7c2c))

## [0.8.0](https://github.com/quidow/gridfleet/compare/gridfleet-testkit-v0.7.0...gridfleet-testkit-v0.8.0) (2026-05-12)


### Features

* **backend,testkit:** recreate run device cooldown api ([fccfbc7](https://github.com/quidow/gridfleet/commit/fccfbc7bcf694f8c59cbaa394bb075d20e1b34f0))
* **testkit:** add cooldown_device client helper and result types ([584f411](https://github.com/quidow/gridfleet/commit/584f411f3b72f815e6f4055667c0eaccb1926bc5))


### Dependencies

* **deps:** bump mypy in /agent ([#195](https://github.com/quidow/gridfleet/issues/195)) ([1317e59](https://github.com/quidow/gridfleet/commit/1317e59bbd4ae6969ed3c717c24b43dbfefec722))


### Documentation

* **testkit:** correct cooldown_device ttl error in readme ([f768b6b](https://github.com/quidow/gridfleet/commit/f768b6b11b87ad3c4aa369ee2bcdebfeed5c1f86))
* **testkit:** document cooldown_device api and result types ([0e2418b](https://github.com/quidow/gridfleet/commit/0e2418b0ea3b952d6151a08e3f75116de7edcdd8))

## [0.7.0](https://github.com/quidow/gridfleet/compare/gridfleet-testkit-v0.6.0...gridfleet-testkit-v0.7.0) (2026-05-11)


### ⚠ BREAKING CHANGES

* **testkit:** AllocatedDevice hydration now accepts device handles, not claim payloads.
* **testkit:** GridFleetClient claim/release helpers and NoClaimableDevicesError are removed.

### Features

* **testkit:** drop claim release client api ([8d1f295](https://github.com/quidow/gridfleet/commit/8d1f29504e6d640ce9d85c70594a515868045be8))
* **testkit:** inject grid run id capability ([b4ae38c](https://github.com/quidow/gridfleet/commit/b4ae38ce9969ae325bffdb9716ba7d6c52a699ac))
* **testkit:** resolve device handle by connection target ([22b4299](https://github.com/quidow/gridfleet/commit/22b4299d6bc4302503a2c2b6f17018ceebc03084))


### Bug Fixes

* **agent:** release adapter-owned doctor refactor ([#165](https://github.com/quidow/gridfleet/issues/165)) ([f3ae257](https://github.com/quidow/gridfleet/commit/f3ae25787e2c8ef926312f11d2313c6513f8bfa9))


### Dependencies

* **deps:** bump urllib3 from 2.6.3 to 2.7.0 in /testkit ([#186](https://github.com/quidow/gridfleet/issues/186)) ([dd7a1df](https://github.com/quidow/gridfleet/commit/dd7a1df8fb29ae76f618abab947db2027471b536))


### Code Refactoring

* **testkit:** drop claim response allocation metadata ([f0eec3e](https://github.com/quidow/gridfleet/commit/f0eec3e9c9f804439241ddbbb56b196bc467effd))

## [0.6.0](https://github.com/quidow/gridfleet/compare/gridfleet-testkit-v0.5.0...gridfleet-testkit-v0.6.0) (2026-05-10)


### ⚠ BREAKING CHANGES

* **backend:** clients sending {drain: true|false} to /api/devices/ {id}/maintenance, /api/devices/bulk/enter-maintenance, or the group bulk equivalent must drop the field. The enter-maintenance behaviour is unchanged from drain=false (always stop the node).

### Features

* **backend:** device state model drift fixes (D1-D6) ([#144](https://github.com/quidow/gridfleet/issues/144)) ([09556fd](https://github.com/quidow/gridfleet/commit/09556fdac8ddb458f1655f9001f25240443062fb))

## [0.5.0](https://github.com/quidow/gridfleet/compare/gridfleet-testkit-v0.4.0...gridfleet-testkit-v0.5.0) (2026-05-08)


### ⚠ BREAKING CHANGES

* remove device_config secret masking ([#104](https://github.com/quidow/gridfleet/issues/104))

### Features

* **backend:** escalate device to maintenance after N cooldowns in same run ([#121](https://github.com/quidow/gridfleet/issues/121)) ([7fe01f7](https://github.com/quidow/gridfleet/commit/7fe01f768ff70cd3ddb7f26aec1ab7210b49987f))
* **main:** split device test_data from device_config + modal portal ([b5d0fa0](https://github.com/quidow/gridfleet/commit/b5d0fa09a862af742b3a2462667a86b1d3a867b6))
* **testkit:** add allocated_device test_data and hydration ([e814e3d](https://github.com/quidow/gridfleet/commit/e814e3d0040b87a547ffc892ee2064305724d576))
* **testkit:** add device_test_data pytest fixture ([51eeefc](https://github.com/quidow/gridfleet/commit/51eeefc522989e827610f2b1103d2ded0cadda89))
* **testkit:** add gridfleetclient test_data methods ([b7f1124](https://github.com/quidow/gridfleet/commit/b7f1124e49618a7193ffc61f9db066f847d38522))
* **testkit:** align public surface, fix run cleanup, lazy env reads ([#128](https://github.com/quidow/gridfleet/issues/128)) ([ee85958](https://github.com/quidow/gridfleet/commit/ee859581f84f77f43c2d0bb627eeeaef1e2a99db))


### Dependencies

* **deps:** bump appium-python-client in /testkit ([dc12591](https://github.com/quidow/gridfleet/commit/dc12591f5ebfba2361341132df546f6325750a61))


### Documentation

* **docs:** document discriminated-union release-with-cooldown response ([7fe01f7](https://github.com/quidow/gridfleet/commit/7fe01f768ff70cd3ddb7f26aec1ab7210b49987f))


### Code Refactoring

* remove device_config secret masking ([#104](https://github.com/quidow/gridfleet/issues/104)) ([7329a31](https://github.com/quidow/gridfleet/commit/7329a3107814f653b81b2753e519e271ec0dd8bd))

## [0.4.0](https://github.com/quidow/gridfleet/compare/gridfleet-testkit-v0.3.0...gridfleet-testkit-v0.4.0) (2026-05-06)


### Features

* **testkit:** wire ?include=config,capabilities through claim/reserve and hydrate inline ([#95](https://github.com/quidow/gridfleet/issues/95)) ([20ed20d](https://github.com/quidow/gridfleet/commit/20ed20d9ee362890923146e771ad8805b45e5bfa))

## [0.3.0](https://github.com/quidow/gridfleet/compare/gridfleet-testkit-v0.2.1...gridfleet-testkit-v0.3.0) (2026-05-05)


### ⚠ BREAKING CHANGES

* **testkit:** promote public api helpers ([#92](https://github.com/quidow/gridfleet/issues/92))

### Features

* **testkit:** add xdist recipe primitives ([#93](https://github.com/quidow/gridfleet/issues/93)) ([58fd3c3](https://github.com/quidow/gridfleet/commit/58fd3c3402ba7e735aae55e27abbe65a05c8ffe8))
* **testkit:** promote public api helpers ([#92](https://github.com/quidow/gridfleet/issues/92)) ([80d4483](https://github.com/quidow/gridfleet/commit/80d44832903f532de3da238d020b5dc27eb8b30e))


### Bug Fixes

* **agent:** trigger release for port conflict cleanup ([6a561ca](https://github.com/quidow/gridfleet/commit/6a561ca480c62b9abb2d5141fa98fc4e1a7696b6))

## [0.2.1](https://github.com/quidow/gridfleet/compare/gridfleet-testkit-v0.2.0...gridfleet-testkit-v0.2.1) (2026-05-03)


### Bug Fixes

* **testkit:** bound supported python metadata ([c5fff86](https://github.com/quidow/gridfleet/commit/c5fff86cbb2a4897ac571c7c5b989f0361e49743))

## [0.2.0](https://github.com/quidow/gridfleet/compare/gridfleet-testkit-v0.1.0...gridfleet-testkit-v0.2.0) (2026-05-03)


### Features

* **testkit:** add run-scoped device cooldowns ([#54](https://github.com/quidow/gridfleet/issues/54)) ([6163dc9](https://github.com/quidow/gridfleet/commit/6163dc959334e933b43c20a99ad4edcbdae6c98b))


### Bug Fixes

* idempotent device release after lifecycle cleanup ([#12](https://github.com/quidow/gridfleet/issues/12)) ([7a98a5d](https://github.com/quidow/gridfleet/commit/7a98a5d18330150aab0a852f6b894d1d53de257c))

## 0.1.0 — Initial Public Preview

- Initial public preview of the GridFleet testkit.
- Python pytest/Appium helper package with device reservation, capability injection, and session lifecycle fixtures.
