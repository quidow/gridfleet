# Changelog

## [0.5.0](https://github.com/quidow/gridfleet/compare/gridfleet-router-v0.4.0...gridfleet-router-v0.5.0) (2026-07-21)


### ⚠ BREAKING CHANGES

* **testkit:** `GridFleetClient.list_devices` no longer accepts the `hardware_telemetry_state` or `hardware_health_status` keyword arguments. Both filters were removed from `GET /api/devices` along with the device telemetry feature, so passing them raises `TypeError`. Remove the arguments from callers; there is no replacement filter.
* **testkit:** target devices with group keys

### Features

* **testkit:** drop hardware telemetry filters from list_devices ([0279208](https://github.com/quidow/gridfleet/commit/027920803fa2bddfc4e14bfbbfedb22b6296a475))
* **testkit:** target devices with group keys ([d094681](https://github.com/quidow/gridfleet/commit/d0946817f6bbd5a2707bf07877d30d9a6de1de9d))

## [0.4.0](https://github.com/quidow/gridfleet/compare/gridfleet-router-v0.3.0...gridfleet-router-v0.4.0) (2026-07-16)


### Features

* **backend:** add deadline-governed create retry and durable remediation ([d20ae1e](https://github.com/quidow/gridfleet/commit/d20ae1ed50d9b64b99f8323152593b515714f25e))
* **router:** pass remaining create budget to backend via header ([0ef218c](https://github.com/quidow/gridfleet/commit/0ef218cb795f8751abc86d7e21a2255dcf0f59b6))

## [0.3.0](https://github.com/quidow/gridfleet/compare/gridfleet-router-v0.2.0...gridfleet-router-v0.3.0) (2026-07-12)


### Features

* backend-owned appium session creation (WS-14.1) ([347599c](https://github.com/quidow/gridfleet/commit/347599ccac415cb330ad0a6f15be6effed688933))

## [0.2.0](https://github.com/quidow/gridfleet/compare/gridfleet-router-v0.1.0...gridfleet-router-v0.2.0) (2026-06-22)


### Features

* **router:** add inject_device_id w3c capability helper ([c2c1233](https://github.com/quidow/gridfleet/commit/c2c1233536eb61f07dbea9a47ac355d2701767e0))
* **router:** inject gridfleet:deviceId into new-session response caps ([9c3509c](https://github.com/quidow/gridfleet/commit/9c3509c6b9587e8cc25bd3cda49ab51b6b2140b1))
* **router:** inject the gridfleet deviceId cap into session responses ([dc6e341](https://github.com/quidow/gridfleet/commit/dc6e341340454b9441dfb22dbb3de454b7539ae8))
* **router:** pass negotiated capabilities to backend confirm ([ce15768](https://github.com/quidow/gridfleet/commit/ce157684d8547daf0e7a4a66eb1c3f14f3088056))
* sessions page rework — active/history tabs, capabilities, operator kill ([c8edbe9](https://github.com/quidow/gridfleet/commit/c8edbe9c4e52a87b561132e9166fe0544e53f7ac))
* thread gridfleet device id into session caps; retire by-connection-target lookup ([0fe77ce](https://github.com/quidow/gridfleet/commit/0fe77ced0475597213d4bbf1eadc694d78856680))
