# Changelog — GridFleet Testkit

All notable changes to the GridFleet testkit (`gridfleet-testkit` on PyPI) are documented here.

## [0.14.6](https://github.com/quidow/gridfleet/compare/gridfleet-testkit-v0.14.5...gridfleet-testkit-v0.14.6) (2026-07-17)


### Dependencies

* **deps:** bump httpx2 in /testkit in the python-dependencies group ([#832](https://github.com/quidow/gridfleet/issues/832)) ([d1cff22](https://github.com/quidow/gridfleet/commit/d1cff22edd759561c938de540e9fe2c1712fb2c5))
* **deps:** bump mypy in /testkit in the python-dependencies group ([#818](https://github.com/quidow/gridfleet/issues/818)) ([cf28cb7](https://github.com/quidow/gridfleet/commit/cf28cb70d21032b2115ae9f10a6a5d552921d3d1))
* **deps:** bump ruff in /testkit in the python-dependencies group ([#840](https://github.com/quidow/gridfleet/issues/840)) ([85c51be](https://github.com/quidow/gridfleet/commit/85c51be2eaf2f944d13ff04a43e1a93629fbd723))

## [0.14.5](https://github.com/quidow/gridfleet/compare/gridfleet-testkit-v0.14.4...gridfleet-testkit-v0.14.5) (2026-07-10)


### Dependencies

* **deps:** bump ruff in /testkit in the python-dependencies group ([#785](https://github.com/quidow/gridfleet/issues/785)) ([bdfdffe](https://github.com/quidow/gridfleet/commit/bdfdffe9f3ee270727997712629a4a027f197a5b))

## [0.14.4](https://github.com/quidow/gridfleet/compare/gridfleet-testkit-v0.14.3...gridfleet-testkit-v0.14.4) (2026-07-08)


### Dependencies

* **deps:** bump mypy in /testkit in the python-dependencies group ([#753](https://github.com/quidow/gridfleet/issues/753)) ([56fce4a](https://github.com/quidow/gridfleet/commit/56fce4ad5953c76da440d3205c0244c754778c37))

## [0.14.3](https://github.com/quidow/gridfleet/compare/gridfleet-testkit-v0.14.2...gridfleet-testkit-v0.14.3) (2026-07-05)


### Bug Fixes

* **testkit:** default a 720s http read timeout on appium drivers ([c158ec3](https://github.com/quidow/gridfleet/commit/c158ec3e67cbd1e22aea5338b0b31ec23dce93a2))
* **testkit:** default a 720s http read timeout on appium drivers ([743209d](https://github.com/quidow/gridfleet/commit/743209df8b4cb15b02c89d4d57a2f5779022c792))

## [0.14.2](https://github.com/quidow/gridfleet/compare/gridfleet-testkit-v0.14.1...gridfleet-testkit-v0.14.2) (2026-06-26)


### Dependencies

* **deps:** bump the python-dependencies group ([#694](https://github.com/quidow/gridfleet/issues/694)) ([537ba27](https://github.com/quidow/gridfleet/commit/537ba273cec07fc6a247c2d17da012805845479a))

## [0.14.1](https://github.com/quidow/gridfleet/compare/gridfleet-testkit-v0.14.0...gridfleet-testkit-v0.14.1) (2026-06-25)


### Dependencies

* **deps:** bump pytest in /testkit in the python-dependencies group ([#653](https://github.com/quidow/gridfleet/issues/653)) ([873d92a](https://github.com/quidow/gridfleet/commit/873d92a4890b39b62bf8a46dde47650870a37af9))
* **deps:** bump ruff in /testkit in the python-dependencies group ([#672](https://github.com/quidow/gridfleet/issues/672)) ([8ebf10c](https://github.com/quidow/gridfleet/commit/8ebf10c8344fefa539aca0a3f8d2e43de398d111))

## [0.14.0](https://github.com/quidow/gridfleet/compare/gridfleet-testkit-v0.13.0...gridfleet-testkit-v0.14.0) (2026-06-21)


### ⚠ BREAKING CHANGES

* **testkit:** test capabilities must use gridfleet:tag:* (not

### Features

* **testkit:** resolve device id and tags via the gridfleet cap prefix ([20dcf5a](https://github.com/quidow/gridfleet/commit/20dcf5a3e19ca2cc0d58fb352cc9191f50dd2873))

## [0.13.0](https://github.com/quidow/gridfleet/compare/gridfleet-testkit-v0.12.0...gridfleet-testkit-v0.13.0) (2026-06-20)


### ⚠ BREAKING CHANGES

* **testkit:** remove AllocatedDevice and hydrate_allocated_device
* **testkit:** type resolve_device_handle_from_driver and device_handle as Device
* **testkit:** return typed Device from get_device and list_devices
* **testkit:** add typed Device dataclass for device reads
* **testkit:** removed AllocatedDevice.live_capabilities, the fetch_capabilities argument of hydrate_allocated_device, and GridFleetClient.get_device_capabilities. Read live session capabilities from the Appium driver's .capabilities instead.
* **testkit:** removed the public testkit symbol hydrate_allocated_device_from_driver. Use hydrate_allocated_device(fetch_capabilities=True), or fold capabilities onto a frozen AllocatedDevice with dataclasses.replace, instead.
* **testkit:** removed public testkit symbols UnavailableInclude, UnknownIncludeError, ReserveCapabilitiesUnsupportedError, the include= keyword argument of reserve_devices, and AllocatedDevice.unavailable_includes.
* **testkit:** removed public testkit symbols get_connection_target_from_driver, GridFleetClient.get_device_config, get_device_config_for_driver, the device_config pytest fixture, and AllocatedDevice.config (plus the fetch_config argument of hydrate_allocated_device). Use driver.capabilities["appium:udid"] and the test_data helpers instead.
* **testkit:** gridfleet_testkit.GRID_URL and gridfleet_testkit.GRIDFLEET_API_URL module attributes are removed. Use gridfleet_testkit.grid_url() and gridfleet_testkit.api_url() functions instead. The GRID_URL/GRIDFLEET_API_URL environment variables are unchanged.
* **testkit:** resolve device id from gridfleet:deviceId cap, drop connection-target lookup

### Features

* **backend:** make preparation-failure maintenance escalation configurable ([3aeccc8](https://github.com/quidow/gridfleet/commit/3aeccc8bc3ee93360219b96f4ecbb776f85a3e45))
* **testkit:** add typed Device dataclass for device reads ([1fcc89e](https://github.com/quidow/gridfleet/commit/1fcc89ea360ca028c09df02bc04e7761db7d9687))
* **testkit:** drop live_capabilities and get_device_capabilities ([b12a25f](https://github.com/quidow/gridfleet/commit/b12a25f5e3e67dfa95fa839c6dcc141571d21915))
* **testkit:** drop reserve include= param and include error types ([14b7965](https://github.com/quidow/gridfleet/commit/14b79659f82a4130052f2cc36f8fee6c200203f8))
* **testkit:** remove AllocatedDevice and hydrate_allocated_device ([dfcf979](https://github.com/quidow/gridfleet/commit/dfcf9790e57526db16428527ce2ad3789ff480fa))
* **testkit:** remove hydrate_allocated_device_from_driver ([e8633fa](https://github.com/quidow/gridfleet/commit/e8633fa4279c57ea976f4be502ce19e2e00d4057))
* **testkit:** remove redundant connection-target and device-config read helpers ([c71c08f](https://github.com/quidow/gridfleet/commit/c71c08f54bb682d24716e40290f6e3138ae890af))
* **testkit:** resolve device id from gridfleet:deviceId cap, drop connection-target lookup ([c778084](https://github.com/quidow/gridfleet/commit/c778084063f058eeaa881af2412248dcf174c10a))
* **testkit:** return typed Device from get_device and list_devices ([09fcb0f](https://github.com/quidow/gridfleet/commit/09fcb0fc03efa34bce8c42d66a347f98b3bc3b79))
* **testkit:** support client_config for Appium driver creation ([4a45541](https://github.com/quidow/gridfleet/commit/4a45541a7b5c98f146ac9f81c6b40b3dc63ac5a5))
* **testkit:** support client_config for Appium driver creation ([2000acf](https://github.com/quidow/gridfleet/commit/2000acfc3b2b885dd0539aa17977524dfedd6a06))
* **testkit:** type resolve_device_handle_from_driver and device_handle as Device ([575879e](https://github.com/quidow/gridfleet/commit/575879e98b1601b8a1e68dde6e5a6e1f7ec0e753))
* thread gridfleet device id into session caps; retire by-connection-target lookup ([0fe77ce](https://github.com/quidow/gridfleet/commit/0fe77ced0475597213d4bbf1eadc694d78856680))


### Bug Fixes

* **backend:** update grid cooldown mocks and testkit type for 5-tuple/released status ([3a51fd8](https://github.com/quidow/gridfleet/commit/3a51fd898c62826d8428631f20d245e78a3bb7ba))
* **testkit:** align pytest fixtures with deviceId cap resolution ([5a86f07](https://github.com/quidow/gridfleet/commit/5a86f079f678d33c27d476342a95dc82eaef9434))
* **testkit:** fetch hydrate config by device id regardless of connection target ([53a6e8c](https://github.com/quidow/gridfleet/commit/53a6e8c9236b7b959743f287140f41af5b02cbad))
* **testkit:** hydrate device config by device id, not connection target ([7bb9ee4](https://github.com/quidow/gridfleet/commit/7bb9ee47807387d23477816224e29af06120a767))
* **testkit:** repair stale GRID_URL doc, preserve tags, re-home dropped fixture tests ([d6303b7](https://github.com/quidow/gridfleet/commit/d6303b70b941edde0c102c8ff7751903ccf04297))
* **testkit:** require appium-python-client &gt;=5.0 for client_config ([8a58ea1](https://github.com/quidow/gridfleet/commit/8a58ea1decccc9dc8e08a999535aec99f441c671))


### Dependencies

* **deps:** bump ruff in /testkit in the python-dependencies group ([#628](https://github.com/quidow/gridfleet/issues/628)) ([54fa5c8](https://github.com/quidow/gridfleet/commit/54fa5c81f1f4fb50219be19147f269b28cd35937))
* **testkit:** migrate from httpx to httpx2 ([a51bde5](https://github.com/quidow/gridfleet/commit/a51bde538405696cb1938df13ecc3448ab01c400))


### Documentation

* **docs:** replace by-connection-target lookup with gridfleet:deviceId cap ([73740de](https://github.com/quidow/gridfleet/commit/73740de952ff2c8aa7e81ea9ab118fd527033847))
* **testkit:** document typed Device, drop AllocatedDevice/hydration ([db8290a](https://github.com/quidow/gridfleet/commit/db8290abcc002b3e18bbc4d3a908a4b6d2aba794))
* **testkit:** drop stale reserve-inline test_data note ([ed1d6df](https://github.com/quidow/gridfleet/commit/ed1d6df99fb3999e4e0b3143b60826cb2f910e9d))
* **testkit:** restore tags-cast rationale in Device.from_payload ([0ff4b3f](https://github.com/quidow/gridfleet/commit/0ff4b3f9721b3682fd68a1ac401f1f84d2fd43ac))


### Code Refactoring

* **testkit:** move env/url resolution to config, drop GRID_URL module attrs ([a26912e](https://github.com/quidow/gridfleet/commit/a26912e9747f172e6ef46e3e737a937507c0ae3e))

## [0.12.0](https://github.com/quidow/gridfleet/compare/gridfleet-testkit-v0.11.0...gridfleet-testkit-v0.12.0) (2026-06-18)


### ⚠ BREAKING CHANGES

* **testkit:** remove Selenium Grid-era session registration

### Features

* **testkit:** remove Selenium Grid-era session registration ([6b22582](https://github.com/quidow/gridfleet/commit/6b2258206758f6e60a3d32f962ac82c968cb69fb))


### Dependencies

* **deps:** bump pytest in /testkit in the python-dependencies group ([#595](https://github.com/quidow/gridfleet/issues/595)) ([3a39406](https://github.com/quidow/gridfleet/commit/3a39406eb93f0082027a0d1a3460627479ff1f1a))
* **deps:** bump ruff in /testkit in the python-dependencies group ([#584](https://github.com/quidow/gridfleet/issues/584)) ([1b68d43](https://github.com/quidow/gridfleet/commit/1b68d439a73255857d11ef4cf9791c9629b93736))

## [0.11.0](https://github.com/quidow/gridfleet/compare/gridfleet-testkit-v0.10.0...gridfleet-testkit-v0.11.0) (2026-06-11)


### Features

* **testkit:** add reserved filter passthrough to list_devices ([a00162a](https://github.com/quidow/gridfleet/commit/a00162a084b981e4c9ae28852fb0bc1fcd1720de))

## [0.10.0](https://github.com/quidow/gridfleet/compare/gridfleet-testkit-v0.9.5...gridfleet-testkit-v0.10.0) (2026-06-07)


### ⚠ BREAKING CHANGES

* **testkit:** bind sessions to runs via the run-scoped grid url

### Features

* **testkit:** bind sessions to runs via the run-scoped grid url ([da34f10](https://github.com/quidow/gridfleet/commit/da34f10f095ed74b2733c0746f11632039bd109e))


### Bug Fixes

* **testkit:** route the pytest plugin driver fixture through the run-scoped url ([d976c8a](https://github.com/quidow/gridfleet/commit/d976c8afb7c10ade85b3b03fb1c7d40756e952ca))


### Dependencies

* **deps:** bump ruff in /testkit in the python-dependencies group ([#513](https://github.com/quidow/gridfleet/issues/513)) ([f7e8fb9](https://github.com/quidow/gridfleet/commit/f7e8fb92da993155852e328dd2e8e174fe93bba7))


### Documentation

* **docs:** sweep selenium grid references for router architecture ([d086bcb](https://github.com/quidow/gridfleet/commit/d086bcb7c1619fb21cdf5e59499ab8221b18a0e4))

## [0.9.5](https://github.com/quidow/gridfleet/compare/gridfleet-testkit-v0.9.4...gridfleet-testkit-v0.9.5) (2026-06-04)


### Dependencies

* **deps:** bump ruff in /testkit in the python-dependencies group ([#428](https://github.com/quidow/gridfleet/issues/428)) ([06ba6a7](https://github.com/quidow/gridfleet/commit/06ba6a7936769768436499d7e34f1053f3bf4710))


### Documentation

* **docs:** align all docs with the actual implementation state ([#499](https://github.com/quidow/gridfleet/issues/499)) ([1d7a4ea](https://github.com/quidow/gridfleet/commit/1d7a4ea2afafbd5872856a01a9f73792c9ce5f7f))

## [0.9.4](https://github.com/quidow/gridfleet/compare/gridfleet-testkit-v0.9.3...gridfleet-testkit-v0.9.4) (2026-05-24)


### Dependencies

* **deps:** bump idna from 3.11 to 3.15 in /testkit ([#307](https://github.com/quidow/gridfleet/issues/307)) ([5db2fb8](https://github.com/quidow/gridfleet/commit/5db2fb89577da1a15d6fbe9cfbc6d7c8d634ccab))
* **deps:** bump ruff in /agent ([#294](https://github.com/quidow/gridfleet/issues/294)) ([0f82674](https://github.com/quidow/gridfleet/commit/0f826741fbd1ebb90eeff8ab169b4aee4da7c91e))
* **deps:** bump ruff in /testkit in the python-dependencies group ([#339](https://github.com/quidow/gridfleet/issues/339)) ([abd41ab](https://github.com/quidow/gridfleet/commit/abd41abe4e02db264f164ab7e8aa3d62e9c00d66))

## [0.9.3](https://github.com/quidow/gridfleet/compare/gridfleet-testkit-v0.9.2...gridfleet-testkit-v0.9.3) (2026-05-16)


### Bug Fixes

* bind testkit-registered sessions to their device ([35030ef](https://github.com/quidow/gridfleet/commit/35030eff438b3d8cfa17b54f1329dfeb03dadf07))
* **testkit:** stop sending client-derived device identity on register ([c025d1e](https://github.com/quidow/gridfleet/commit/c025d1e10d2bd793dadc4ca167aaaf4a7a2e23e7))

## [0.9.2](https://github.com/quidow/gridfleet/compare/gridfleet-testkit-v0.9.1...gridfleet-testkit-v0.9.2) (2026-05-16)


### Bug Fixes

* **testkit:** remove pytest_plugins shim and stale --extra appium docs ([#270](https://github.com/quidow/gridfleet/issues/270)) ([1165fc1](https://github.com/quidow/gridfleet/commit/1165fc1e7326853e7467500c41d8b1c197e7046f))

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
