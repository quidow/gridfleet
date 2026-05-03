# Changelog — GridFleet Testkit

All notable changes to the GridFleet testkit (`gridfleet-testkit` on PyPI) are documented here.

## [0.2.2](https://github.com/quidow/gridfleet/compare/gridfleet-testkit-v0.2.1...gridfleet-testkit-v0.2.2) (2026-05-03)


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
