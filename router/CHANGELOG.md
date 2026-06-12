# Changelog

## [0.2.0](https://github.com/quidow/gridfleet/compare/gridfleet-router-v0.1.0...gridfleet-router-v0.2.0) (2026-06-12)


### Features

* **router:** backend control-plane client ([ca772db](https://github.com/quidow/gridfleet/commit/ca772db797b3b221efe03a9d6b1e74a78825261a))
* **router:** env-var configuration for all flags ([5e412ae](https://github.com/quidow/gridfleet/commit/5e412ae0a2a107991e3d1be7d801a606cab4009f))
* **router:** extract negotiated capabilities from create response ([2f06493](https://github.com/quidow/gridfleet/commit/2f064935291ca8392c4e873a9c52f867ab45f2d3))
* **router:** new-session allocate, create, confirm flow ([1623d6e](https://github.com/quidow/gridfleet/commit/1623d6ec005a3850486b99e170ff5c5e0cd3bc02))
* **router:** pass negotiated capabilities to backend confirm ([ce15768](https://github.com/quidow/gridfleet/commit/ce157684d8547daf0e7a4a66eb1c3f14f3088056))
* **router:** proxy core with route resolution and lazy pruning ([a7b8fee](https://github.com/quidow/gridfleet/commit/a7b8fee7f89e3b15703ff2a1c075f6e9f6907e29))
* **router:** route map, activity tracker and w3c envelopes ([642b729](https://github.com/quidow/gridfleet/commit/642b7298e6edf797736765f8cf8a26f9b538495a))
* **router:** run-scoped /run/{run_id} webdriver endpoint ([b8bbefb](https://github.com/quidow/gridfleet/commit/b8bbefb843ff7eed952eafe3201189f59fd34e75))
* **router:** scaffold gridfleet-router crate and repo plumbing ([81cac08](https://github.com/quidow/gridfleet/commit/81cac08e552daa7489fc645256d8086f0c1be008))
* **router:** server bootstrap, periodic reconcile and metrics ([fe9e8b7](https://github.com/quidow/gridfleet/commit/fe9e8b7b7b1f15bb190fd9175185d34de0867cd1))
* **router:** w3c path classification ([14fa590](https://github.com/quidow/gridfleet/commit/14fa590b2b2891dcd51377abe8ec7341abd2b949))
* **router:** w3c webdriver router component (grid-router 2/3) ([58cd9bb](https://github.com/quidow/gridfleet/commit/58cd9bb484ac67a57bb307928b6fe9350c9d55d8))
* sessions page rework — active/history tabs, capabilities, operator kill ([c8edbe9](https://github.com/quidow/gridfleet/commit/c8edbe9c4e52a87b561132e9166fe0544e53f7ac))


### Bug Fixes

* **router:** bound session create by claim window and roll back unconfirmed sessions ([49a1e0b](https://github.com/quidow/gridfleet/commit/49a1e0b6fb4be09219b324bfebff0b9507808c37))
* **router:** close session-leak and timeout gaps in proxy flow ([368e823](https://github.com/quidow/gridfleet/commit/368e823d4601e8ee52a0cd30d672edf89aad30f1))
* **router:** fail allocate fast on unexpected 4xx instead of retrying ([3810e72](https://github.com/quidow/gridfleet/commit/3810e721c2238038ed0884f1c0b3392c8a553ac0))
* **router:** keep idle websocket tunnels alive past the proxy timeout ([ed69114](https://github.com/quidow/gridfleet/commit/ed6911461c42456f4b16aac8773c57d21b6ab997))
* **router:** normalize trailing slash and fail fast on backend auth errors ([9267dab](https://github.com/quidow/gridfleet/commit/9267dabb481476bdfb2171c5e71c1ec4a5c73013))
* **router:** preserve activity on failed flush ([0b5c3fd](https://github.com/quidow/gridfleet/commit/0b5c3fdac79a23898b6b81dfbb133c8e3be4adbb))
* **router:** preserve freshly-inserted route across reconcile swap ([b8e8628](https://github.com/quidow/gridfleet/commit/b8e8628f7e09b4b259fc6665c5a0423af6b555bb))
* **router:** prune and notify session_ended on any DELETE response ([56d8f3c](https://github.com/quidow/gridfleet/commit/56d8f3c11b818d3e9a4eccc4b3080a5995f42a8e))
* **router:** render FastAPI detail arrays as text in allocate 4xx messages ([1a050cc](https://github.com/quidow/gridfleet/commit/1a050cc5409cefdabf1636b4331c0fe4f80ae5f6))
* **router:** roll back created session when the client is gone ([aa023b1](https://github.com/quidow/gridfleet/commit/aa023b1eaef8294f6966bc86a6110c4896000319))
* **router:** third review wave hardening ([1e927fe](https://github.com/quidow/gridfleet/commit/1e927fe4c0d1ab91388c948409baf9f5cf8e5e52))
* **router:** treat empty backend auth env as absent ([aeecbf8](https://github.com/quidow/gridfleet/commit/aeecbf8f9dbf2c7815aca147f3fd7a48b90628df))
* wave-5 review hardening for the grid router migration ([e56ff27](https://github.com/quidow/gridfleet/commit/e56ff2705aa6099beaf070391c519092de82304b))


### Performance Improvements

* **router:** hot-path micro fixes from the wave-5 review ([07e8c1d](https://github.com/quidow/gridfleet/commit/07e8c1d7da13f5511a1299e60b2edcc7d3a78b4e))
