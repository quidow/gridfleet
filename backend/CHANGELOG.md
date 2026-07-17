# Changelog — GridFleet Backend

All notable changes to the GridFleet backend (FastAPI manager, control plane) are documented here.

## Unreleased

### Features

- Add `GRIDFLEET_AGENT_RECOMMENDED_VERSION` setting and expose `recommended_agent_version` / `agent_update_available` fields on the host API, enabling upgrade awareness for connected agents.
- Add configurable terminal WebSocket scheme (`GRIDFLEET_TERMINAL_WS_SCHEME`).

### Fixes

- Bracket-wrap IPv6 addresses in agent terminal URLs so `ws://[::1]:5100/...` is valid.
- Close drain-transition race by committing draining state before `try_complete_drain`, preventing concurrent `assert_runnable` from starting new work during a drain.

## [0.5.0](https://github.com/quidow/gridfleet/compare/gridfleet-backend-v0.4.0...gridfleet-backend-v0.5.0) (2026-07-17)


### ⚠ BREAKING CHANGES

* **backend:** remove manifest doctor block and host_fields_schema
* **backend:** drop six presentation/plumbing settings rows
* **backend:** node health debounce becomes duration-based
* **backend:** device-check debounce becomes duration-based
* **backend:** 10 plumbing settings are removed from the registry and fixed as code constants (values unchanged from their defaults): general.heartbeat_interval_sec, general.partition_probe_interval_sec, general.intent_reconcile_interval_sec, grid.session_poll_interval_sec, appium_reconciler.host_parallelism, agent.http_pool_enabled, agent.http_pool_max_keepalive, agent.http_pool_idle_seconds, agent.circuit_breaker_failure_threshold, agent.circuit_breaker_cooldown_seconds. Operator-tuned rows for these keys are deleted by migration.
* **agent:** GET /agent/pack/devices/{target}/properties and /telemetry are gone — these observations ride the consolidated status push (contract v6); backends on the flag-day release no longer dial them. Their now-unreferenced response schemas are dropped from agent schemas and the backend's regenerated agent_comm/generated.py.
* **backend:** agents must speak orchestration contract v6 (local observation probes in the status push) — older agents are rejected at registration with 426. The general.node_check_interval_sec, device_check_interval_sec, host_resource_telemetry_interval_sec, hardware_telemetry_interval_sec, property_refresh_interval_sec, and probe_concurrency_per_host settings are removed (probe cadences are now agent-side constants); a migration deletes their rows.
* **backend:** device detail ?include=orchestration intent entries rename axis to kind; device_intents requires the 2026_07_10_intent_kind migration.
* **agent:** push one consolidated status; stop pushing pack status separately
* **backend:** derive host liveness from status-push recency
* **agent:** converge restarts from the spawn-time watermark
* **backend:** replace the transition-token lease with a restart watermark
* **main:** prune zero-user driver-pack verticals ([#771](https://github.com/quidow/gridfleet/issues/771))
* **backend:** require orchestration contract v3 (rejects pre-node-pull agents)
* **backend:** drop agent_reconfigure_outbox table and model
* **backend:** delete reconfigure-outbox delivery; poke is the only agent wake
* **backend:** reconcile_host()/converge_host_rows() drop the node_pull parameter; reconciler_agent no longer exports start_remote_node/stop_remote_node.

### Features

* add agent-local observation probes ([5b2e750](https://github.com/quidow/gridfleet/commit/5b2e7503959a8e16a7da8f4b14f2f3136718796f))
* **agent:** advertise node_desired_pull capability when pull is enabled ([5a24ec5](https://github.com/quidow/gridfleet/commit/5a24ec57dafa9f79d7f57d15981cd6024042008c))
* **agent:** converge restarts from the spawn-time watermark ([b432bd9](https://github.com/quidow/gridfleet/commit/b432bd941d5db0aaa318d1a589065d742b596504))
* **agent:** emit boot_id and per-section dedup token on status push ([802a72c](https://github.com/quidow/gridfleet/commit/802a72c16b48513bdaad52282845e78dda2ae399))
* **agent:** node desired-state pull loop (phase 8a) ([a5e5ec6](https://github.com/quidow/gridfleet/commit/a5e5ec6cddbf9eb28dd7ef1b8ece4249b9be8634))
* **agent:** node refresh poke endpoint and applied-generation health reporting ([a1f8203](https://github.com/quidow/gridfleet/commit/a1f820393c8e6e6ff4730df5daf79003edc312bc))
* **agent:** push one consolidated status; stop pushing pack status separately ([4e2f7d8](https://github.com/quidow/gridfleet/commit/4e2f7d8ed8a8430605d62ea69b53cedc842f041a))
* **agent:** remove dead appium start/stop/reconfigure surface ([1ba8283](https://github.com/quidow/gridfleet/commit/1ba82836980dff413cc12f0fac51bcea85f67abe))
* **agent:** remove dialed device properties and telemetry probe routes ([1ec4c8b](https://github.com/quidow/gridfleet/commit/1ec4c8b060e129d883263221904b9b5740db43e3))
* **agent:** run adapter hooks in pack worker subprocesses ([3cbbd42](https://github.com/quidow/gridfleet/commit/3cbbd4277e3646a5a10d6347b0885c72c0285e3e))
* backend-owned appium session creation (WS-14.1) ([347599c](https://github.com/quidow/gridfleet/commit/347599ccac415cb330ad0a6f15be6effed688933))
* **backend:** add appium node observed_pack_release column ([c866d64](https://github.com/quidow/gridfleet/commit/c866d64a3cfb816ed682fae18c252e774e74387e))
* **backend:** add boot fence and per-section dedup cursor to status push ([4058ebe](https://github.com/quidow/gridfleet/commit/4058ebe3bb22fce34e78ede54279f15f276453ad))
* **backend:** add deadline-governed create retry and durable remediation ([d20ae1e](https://github.com/quidow/gridfleet/commit/d20ae1ed50d9b64b99f8323152593b515714f25e))
* **backend:** add derived remediation stop and start rungs ([5c6a6ad](https://github.com/quidow/gridfleet/commit/5c6a6ad9a652d3f829e84cf68d0ba5a13d558f23))
* **backend:** add device_health fold receipt columns to device ([aae545f](https://github.com/quidow/gridfleet/commit/aae545fcc7f9e83cc707c49dc9030f9fda6b77bb))
* **backend:** add device_health_remediation durable worker ([6451837](https://github.com/quidow/gridfleet/commit/64518377dde0469374938136bae12c9cad63ef45))
* **backend:** add device_health_remediation job kind and dedupe schema ([360a96b](https://github.com/quidow/gridfleet/commit/360a96bcd28b1216766e5123f64f634aa36c93b9))
* **backend:** add device_remediation_log table and retention window ([7d0e4c8](https://github.com/quidow/gridfleet/commit/7d0e4c8b306bebc090c39a2460936dfe72b264a9))
* **backend:** add explicit decision ladders for device desired state ([7cdb496](https://github.com/quidow/gridfleet/commit/7cdb496150c6a9bd3460bad235cd7678d5471cec))
* **backend:** add facts-only device_health fold for the status fold loop ([607b3bf](https://github.com/quidow/gridfleet/commit/607b3bf2173c325bb8fe147a634206c41f22db25))
* **backend:** add facts-only device_health_remediation enqueue helper ([4e38043](https://github.com/quidow/gridfleet/commit/4e380437b539c991c444d2959f2c7b0bced71cea))
* **backend:** add lifecycle_state_capable to probe-targets dto ([8a1facb](https://github.com/quidow/gridfleet/commit/8a1facb8c461665c1082d3b7b194f34201a14856))
* **backend:** add raw-body appium session create for the router flow ([5153636](https://github.com/quidow/gridfleet/commit/51536364e3e095672420fd5b706dbb9111c6c705))
* **backend:** add read-time recovery availability projection ([8596d36](https://github.com/quidow/gridfleet/commit/8596d368c8f5646c9c8705debdcbe261d3580eab))
* **backend:** add release rollout janitor stage ([bba748c](https://github.com/quidow/gridfleet/commit/bba748c122cb1e9e4ec927ff46f1106f05ed5286))
* **backend:** add release_rollout command to the node decision ladder ([619140b](https://github.com/quidow/gridfleet/commit/619140be23b46e091fb6836d7dcdef7a767d7990))
* **backend:** add shared pushed device_health parser ([243075b](https://github.com/quidow/gridfleet/commit/243075b626b6b8a1cc64355f322391a8e8767c9b))
* **backend:** add shared remediation escalation module ([a108341](https://github.com/quidow/gridfleet/commit/a108341b9f7104cd0bdf54d20322d38a1b700e6a))
* **backend:** add status-push fold reconciler foundation ([c8ade5f](https://github.com/quidow/gridfleet/commit/c8ade5fb72d5e36741e6b7550c53adfd32b4f4e7))
* **backend:** add the consolidated agent status-push ingest route ([8d41c8b](https://github.com/quidow/gridfleet/commit/8d41c8b3be42c4557085c2a89534f00e662832c8))
* **backend:** add two-axis observation-revision guard for health folds ([2879d24](https://github.com/quidow/gridfleet/commit/2879d24a5b789eff4b0fcf7512dd4a63105ef6f1))
* **backend:** agent-facing desired appium-nodes endpoint ([ba27086](https://github.com/quidow/gridfleet/commit/ba2708669660f617c7ded459f9f4b92d85a3fa24))
* **backend:** agent-pull node desired state — phase 8b (backend mode switch) ([ab471d7](https://github.com/quidow/gridfleet/commit/ab471d78a046198fc6f708d0b5e2ac187e3bb23e))
* **backend:** apply pushed emulator_state synchronously on the push path ([2d7bc1d](https://github.com/quidow/gridfleet/commit/2d7bc1d109037317e953832c6e0bf17d0226ebd3))
* **backend:** bounded deadline-governed create-retry with target exclusion ([ad09257](https://github.com/quidow/gridfleet/commit/ad09257aacc655c8128aa8d773983aa9dd3f18ad))
* **backend:** create-and-promote orchestrator for backend-owned session creation ([1c18729](https://github.com/quidow/gridfleet/commit/1c187299ec53ec12c1d69283cfd7f1644f6548be))
* **backend:** decide desired state from commands plus facts ([a4cb172](https://github.com/quidow/gridfleet/commit/a4cb1728e1b7eec9fd24f3545670a62fbb4ad684))
* **backend:** delete reconfigure-outbox delivery; poke is the only agent wake ([5735033](https://github.com/quidow/gridfleet/commit/573503327ab43d72cfcb9209fb0f7ea9ddb615ae))
* **backend:** delete the intent dirty queue ([0ee0077](https://github.com/quidow/gridfleet/commit/0ee00773fd488cc3d084f06fb7a8dd3c51a760ac))
* **backend:** demote registration to enrollment-only; ingest telemetry from the push ([47b8861](https://github.com/quidow/gridfleet/commit/47b8861159823f854f9151fe910a09604e4ed412))
* **backend:** derive host liveness from status-push recency ([cf56e05](https://github.com/quidow/gridfleet/commit/cf56e05812c92f3c7455b90aceaf8fdd3119cadc))
* **backend:** derive node-process directive and deferred-stop from the remediation log ([65cbc7b](https://github.com/quidow/gridfleet/commit/65cbc7b7c6f828cd65617c49a61be1b635797d4f))
* **backend:** derive operational bus-event severity from the transition alone ([d7743ef](https://github.com/quidow/gridfleet/commit/d7743efebfd2ade7cfd363c2e74fbe937373d78f))
* **backend:** device-check debounce becomes duration-based ([8e2d8c8](https://github.com/quidow/gridfleet/commit/8e2d8c8ca8d687de245fc1b6206d8d96c7e35cf5))
* **backend:** drop agent_reconfigure_outbox table and model ([c14b106](https://github.com/quidow/gridfleet/commit/c14b1064c958a44d7d259eca78a80da1c5683593))
* **backend:** drop device_intent_dirty table and full-scan cadence setting ([e84bdf5](https://github.com/quidow/gridfleet/commit/e84bdf5e3760c04ddcca2451f060a5a6e6168e97))
* **backend:** drop recovery_allowed/recovery_blocked_reason columns ([b63b305](https://github.com/quidow/gridfleet/commit/b63b3058dd869496cc4962d6b8930373f2dee3a8))
* **backend:** drop six presentation/plumbing settings rows ([15a25b5](https://github.com/quidow/gridfleet/commit/15a25b597e73ed8b1b83edac43dcf52ac82f2bf7))
* **backend:** drop the claimed ticket state ([60ffa93](https://github.com/quidow/gridfleet/commit/60ffa937872644bf18d26f6632a8ac3f52478995))
* **backend:** drop the claimed ticket state ([db2d2c7](https://github.com/quidow/gridfleet/commit/db2d2c71ae856cd934b91c98804e8eb1c9e9a7cd))
* **backend:** drop the intent dirty queue ([066d666](https://github.com/quidow/gridfleet/commit/066d66648ef3cd78d87b43da9c4f7a2b31ffbe83))
* **backend:** enforce the orchestration contract on every status push ([370764a](https://github.com/quidow/gridfleet/commit/370764a744210dda430f919302283d7edfa371c6))
* **backend:** escalate appium start failures on the shared ladder ([50c97c4](https://github.com/quidow/gridfleet/commit/50c97c4ff3074e8e498132402c8671aa3b8db0ab))
* **backend:** fold device_health off the request path onto the loop ([1ccee73](https://github.com/quidow/gridfleet/commit/1ccee73dd947173924f6d41a76291d6535956393))
* **backend:** fold host_telemetry per push; delete the sweep's global-stage machinery ([b00d4e5](https://github.com/quidow/gridfleet/commit/b00d4e523f007f6b4521ee04387719357dd72b2a))
* **backend:** fold pushed device_health; retire the connectivity dial pass ([4464c3f](https://github.com/quidow/gridfleet/commit/4464c3fc1f35b172460a04117112cb1d26b06320))
* **backend:** fold pushed device_properties; drop the property refresh dial ([5e0de53](https://github.com/quidow/gridfleet/commit/5e0de5323bc00e754083fe9c4c0a2704ff6cf0f7))
* **backend:** fold pushed device_telemetry; drop the hardware telemetry dial ([35814e9](https://github.com/quidow/gridfleet/commit/35814e9fe9199dbb339ef7729fea58b39c4110f1))
* **backend:** fold pushed node_health observations in host_sweep ([069b788](https://github.com/quidow/gridfleet/commit/069b7888674073b23c768ca88f8eebd0f9fba13a))
* **backend:** fold pushed observations at ingest ([ed86da6](https://github.com/quidow/gridfleet/commit/ed86da69464ecafeab7ff4801569edebdb12e9ca))
* **backend:** fold pushed observations into durable facts at ingest ([aeaa622](https://github.com/quidow/gridfleet/commit/aeaa622b596993be29c84338955cb9e97b30aad8))
* **backend:** fold the agent-observed pack release into appium nodes ([29fffe9](https://github.com/quidow/gridfleet/commit/29fffe9db89d1185802b884a55efda3fff545bc8))
* **backend:** gate node-health restarts behind shared escalation ([00ceced](https://github.com/quidow/gridfleet/commit/00cecedd7fb076a6503e61297b6fbdab93e15966))
* **backend:** gate recovery via the projection; drop stored suppression ([b82a6b5](https://github.com/quidow/gridfleet/commit/b82a6b5e7e4ad501e14e59564a4c05df5380e00c))
* **backend:** grid create-failure node-down helper via guarded writer ([ebad5fd](https://github.com/quidow/gridfleet/commit/ebad5fdc90b79fa98a34711872196c400350d4b0))
* **backend:** inline fold consumes pushed device presence and lifecycle by device_id ([9e7236c](https://github.com/quidow/gridfleet/commit/9e7236c71bd8f4255b5491c484f4ea00834ec6c5))
* **backend:** intent reconciler scans every device every tick ([994ce7b](https://github.com/quidow/gridfleet/commit/994ce7b2520cc4f0d92d0c9b5110f26b48bcb0c1))
* **backend:** janitor background loop scaffold with stage_due stages ([af3b221](https://github.com/quidow/gridfleet/commit/af3b221716298804d7d62e4bc6728aef6be8bbe7))
* **backend:** janitor loop replaces four standalone loops and the flusher ([966506e](https://github.com/quidow/gridfleet/commit/966506e25d9aea370f3bf2f262da28af206f6498))
* **backend:** lifecycle write-on-diff and repeat-safe remediation gate ([74a4e20](https://github.com/quidow/gridfleet/commit/74a4e208be18f4a1d7ae7b489a21b812e46125f7))
* **backend:** m2 ordering for pushed emulator_state writes ([b0ad859](https://github.com/quidow/gridfleet/commit/b0ad8596a79b16b7836d5d68d595647193805615))
* **backend:** mint and clear failure_episode_id on device health transitions ([3d00786](https://github.com/quidow/gridfleet/commit/3d00786302801747189f45c8dedd346ece3b2a89))
* **backend:** mirror agent-reported appium spawn time into started_at ([9c9b09d](https://github.com/quidow/gridfleet/commit/9c9b09d49ed9bd8efbbec32332f31add3fe0f204))
* **backend:** move node_health folding onto a level-triggered StatusFoldLoop ([7d7fd46](https://github.com/quidow/gridfleet/commit/7d7fd465a4fd6b9e047d2e966b699da1962845e4))
* **backend:** node health debounce becomes duration-based ([0230173](https://github.com/quidow/gridfleet/commit/023017330daa37f15c970e02c9bb42c6c185a986))
* **backend:** node-pull is the only reconcile mode; delete push start/stop clients ([f7c5d94](https://github.com/quidow/gridfleet/commit/f7c5d9472c860f8501f49688e16a8bc8826d5a6b))
* **backend:** observe-only node convergence for pull-capable hosts ([a83d058](https://github.com/quidow/gridfleet/commit/a83d058c80cf40bbbf0f2d3b37a7677ecaf8a69e))
* **backend:** one scheduling idiom — janitor loop merge ([fcf1ea1](https://github.com/quidow/gridfleet/commit/fcf1ea17edf38b318b217a5cd72ad83e04776e3a))
* **backend:** own grid session creation ([b0506c3](https://github.com/quidow/gridfleet/commit/b0506c32e056cfa53d77983fbb06beea810e1492))
* **backend:** pack release rollout ([66114f5](https://github.com/quidow/gridfleet/commit/66114f5b3054b0d2337892c040b0ed279c8eb6a9))
* **backend:** parse pack_release from running-node snapshots ([139a248](https://github.com/quidow/gridfleet/commit/139a248f5c38cd05b26b572b1cdc83e4690beae4))
* **backend:** poke agents only when reconcile changed node state ([a0ab62b](https://github.com/quidow/gridfleet/commit/a0ab62bcab115b8b6339d537989aaa1a90777b7b))
* **backend:** poke replaces reconfigure delivery for pull-capable hosts ([2cafa3a](https://github.com/quidow/gridfleet/commit/2cafa3a4f24380ae60171b28a1ac80c9c3caa639))
* **backend:** port-conflict re-pin and backoff from agent-reported start failures ([f7805f5](https://github.com/quidow/gridfleet/commit/f7805f5235e7704788e2d384454f4a7f5b5013a5))
* **backend:** probe sessions carry a claim row from birth (WS-16.1) ([576e82f](https://github.com/quidow/gridfleet/commit/576e82f9c31cc3a07265a4b6e316cbf2b51db868))
* **backend:** probe-target roster pull and declared status-push observation fields ([1d6c1a8](https://github.com/quidow/gridfleet/commit/1d6c1a8f1a4fa97925d6d8a3492d1b376c3bd599))
* **backend:** project the recovery badge and blocked state from facts ([9bfc780](https://github.com/quidow/gridfleet/commit/9bfc7809d080ea8860d492d4c0fc56b9bc326ea1))
* **backend:** pure remediation-ladder derivation and policy-view synthesizer ([a3b1519](https://github.com/quidow/gridfleet/commit/a3b151999e4992b9d715ad06de06e07371ccefd5))
* **backend:** record event causes at observation sites, drop observed_reason threading ([caf5117](https://github.com/quidow/gridfleet/commit/caf51173b55ddb4c0f5df3fba42caefe6c5819a6))
* **backend:** record maintenance audit rows in the maintenance service ([796485e](https://github.com/quidow/gridfleet/commit/796485e6d7903009ac43929e1de695e63a50b734))
* **backend:** recovery availability as a read-time projection ([9766763](https://github.com/quidow/gridfleet/commit/97667639c44bac6daf1747626f090f3794d066a7))
* **backend:** registration writes enrollment only; push owns runtime facts ([3f7d408](https://github.com/quidow/gridfleet/commit/3f7d40854836f9b718171001e11a2387d1f51f44))
* **backend:** reject non-repeat-safe remediation actions at manifest load ([cc0a707](https://github.com/quidow/gridfleet/commit/cc0a7079c9a3aa171559e15e98e0918b7f1caba5))
* **backend:** release paths complete pack drains inline; janitor stage is backstop ([031be45](https://github.com/quidow/gridfleet/commit/031be45c00724181d0f9d28945dd069160703a6f))
* **backend:** remediation-log appends and ladder loaders ([7db3aa2](https://github.com/quidow/gridfleet/commit/7db3aa2d1da455cc49112913667022798571ae55))
* **backend:** remove manifest doctor block and host_fields_schema ([f5245ff](https://github.com/quidow/gridfleet/commit/f5245ffc11b2da3507894e6488b07b915facb93e))
* **backend:** remove obsolete start-failure threshold setting ([52a1709](https://github.com/quidow/gridfleet/commit/52a170991bbcf62e24381899e13483cb4423610e))
* **backend:** replace the transition-token lease with a restart watermark ([37e83ff](https://github.com/quidow/gridfleet/commit/37e83ffeb4526b89fdfffd3eb516f4e498757410))
* **backend:** require orchestration contract v3 (rejects pre-node-pull agents) ([2c9dd2e](https://github.com/quidow/gridfleet/commit/2c9dd2eba36091676ea9dfecde3b613a5e8e2c5f))
* **backend:** require orchestration contract v7 for host enrollment ([63da5fc](https://github.com/quidow/gridfleet/commit/63da5fcd5b38cc47d648f35320fc81f31d151e47))
* **backend:** require probe-pushing agents; delete dial-out probe settings ([5aec8fa](https://github.com/quidow/gridfleet/commit/5aec8fa3e8da8abf5c3aa985dc7d77c6ba60e791))
* **backend:** resume lost allocations from the session row ([ce281eb](https://github.com/quidow/gridfleet/commit/ce281ebb95a26c4cd28c1088046ebc428ab7fa72))
* **backend:** route lifecycle recovery failures through shared escalation ([8f31346](https://github.com/quidow/gridfleet/commit/8f313465705d2abd5de7c92547711d705fe52b0e))
* **backend:** serve host online/offline as a read-time recency projection ([27578c1](https://github.com/quidow/gridfleet/commit/27578c19f4b8267bbd357ff8699ba8f18141dc91))
* **backend:** settings diet — plumbing to constants, no env seeding, secure auto-accept ([e57b814](https://github.com/quidow/gridfleet/commit/e57b81441e32be6ae0f3ce0b4266095adb62a520))
* **backend:** single host-liveness edge detector in the sweep ([7f636ef](https://github.com/quidow/gridfleet/commit/7f636efe98561cb6db1b3657338fbeb7c2fd2c30))
* **backend:** stamp allocation sessions with their queue ticket id ([81bdfd3](https://github.com/quidow/gridfleet/commit/81bdfd340af3ab96098c65b8f2a4622d00f64782))
* **backend:** stamp stable rollout restart watermark ([fb6cf5f](https://github.com/quidow/gridfleet/commit/fb6cf5fde63f250cd74da1d32071b4b24527e774))
* **backend:** telemetry sample folds are idempotent under re-processing ([f58ae41](https://github.com/quidow/gridfleet/commit/f58ae41c1b9647ba95fed3e9d0db6944424b7205))
* **backend:** thread request-local exclusion set through try_allocate ([fcb0f86](https://github.com/quidow/gridfleet/commit/fcb0f86cdc27e67a5769d938c431ed8d964bbb42))
* **backend:** treat outcome-stamped verification leases as tombstones ([3f83da1](https://github.com/quidow/gridfleet/commit/3f83da164ef8a1bea63cd7486e1fcb1076812744))
* **backend:** typed create outcome classifier with per-attempt metric ([b6cc4d9](https://github.com/quidow/gridfleet/commit/b6cc4d94bb7130b7a7419229223d481e9787c8d6))
* **backend:** unify automated remediation escalation ([43dc128](https://github.com/quidow/gridfleet/commit/43dc128e96608008b0e5effab2bf042e32e809f0))
* **backend:** verification probes claim with a session row from birth ([9988116](https://github.com/quidow/gridfleet/commit/99881162fed1943d375f817c08edc7184c3dde2f))
* **backend:** viability probes claim their device with a session row from birth ([3a01979](https://github.com/quidow/gridfleet/commit/3a019796753ee2cacf48dcdbf8c47c5af8bb54b8))
* **frontend:** toast behavior comes from built-in defaults ([c045be8](https://github.com/quidow/gridfleet/commit/c045be85dd0fa1d1ff67a01544260f48235c132d))
* **main:** move device health reconciliation onto status fold loop ([75b629b](https://github.com/quidow/gridfleet/commit/75b629b505cac2f84cc34774973b86978a2718ba))
* **main:** prune zero-user driver-pack verticals ([#771](https://github.com/quidow/gridfleet/issues/771)) ([064c67f](https://github.com/quidow/gridfleet/commit/064c67f4359e67819dde87ee452417bc72090c9e))
* read-time host liveness + enrollment-only registration (WS-2.3/2.4) ([3629e56](https://github.com/quidow/gridfleet/commit/3629e56141b275e3f0d275db683434c14e74972f))
* ws-14.2 pack zero-user surface deletion ([762e186](https://github.com/quidow/gridfleet/commit/762e1860e456cd6c199ca6a664209ae4e45cab7c))


### Bug Fixes

* **agent:** fail safe on incomplete device observations ([8e8b922](https://github.com/quidow/gridfleet/commit/8e8b922965df92d25eb232b14f93539f43fa8fd1))
* **agent:** stop phantom iOS/tvOS simulator launches on real-device hosts ([4cfd852](https://github.com/quidow/gridfleet/commit/4cfd852e755630b45d532a7e28497e56cb7ac673))
* **backend:** align presentation migration metadata ([5794bcf](https://github.com/quidow/gridfleet/commit/5794bcfaefb5b9cbbbce1627e715497279e308ea))
* **backend:** batch the maintenance remediation-log prune ([2dea1c4](https://github.com/quidow/gridfleet/commit/2dea1c41cc83f1578ca486dac47940756815590b))
* **backend:** bound node-refresh poke timeout and fan out run-create pokes concurrently ([aeef5f7](https://github.com/quidow/gridfleet/commit/aeef5f7622f2f3ae7b5aed143f4091d3e576d70f))
* **backend:** bound probe create timeout below the grid claim window ([aa9e941](https://github.com/quidow/gridfleet/commit/aa9e9412bdeebf19e0b6dfa1f58275fea6900f33))
* **backend:** close checkpoint verification regressions ([094310f](https://github.com/quidow/gridfleet/commit/094310f6edbc6a3a7bb1afb05b39186c28fb3f97))
* **backend:** complete orphaned remediation jobs as no-ops ([ba287a4](https://github.com/quidow/gridfleet/commit/ba287a4b5f46e77519d69abc10aa07dffa8a432a))
* **backend:** count distinct telemetry observations for hardware hysteresis ([3093df0](https://github.com/quidow/gridfleet/commit/3093df0c0ab6c4066c980c8da30a39759774a4e2))
* **backend:** enforce create-retry budget after allocation polling ([c2c9008](https://github.com/quidow/gridfleet/commit/c2c900851b54e34037e04f646f7396fd23b00e4e))
* **backend:** fold emulator node observations by port so recovery can converge ([068e4b7](https://github.com/quidow/gridfleet/commit/068e4b7afb1446cbb713503472e6d52da0bedd8a))
* **backend:** force-close leaked running probe rows in the liveness sweep ([8ac725c](https://github.com/quidow/gridfleet/commit/8ac725c1accb21ba97d22db0c6f5ba6e263e725b))
* **backend:** guarantee session-viability probe teardown ([9101837](https://github.com/quidow/gridfleet/commit/910183728e931090f3e1c3879f4b3c8fb0ddd52d))
* **backend:** harden pack release rollout stamping and revoke protocol ([6101cf1](https://github.com/quidow/gridfleet/commit/6101cf1262f7030133074455102518130369450e))
* **backend:** harden status push fold publication ([220c042](https://github.com/quidow/gridfleet/commit/220c042b0653c1363df797610b4b805cfcaa8dc2))
* **backend:** harden status-push health reconciliation ([e4ebe30](https://github.com/quidow/gridfleet/commit/e4ebe3039e4a49e9cbc61ffffd3b146e6c174463))
* **backend:** keep leader lock connection out of an open transaction ([df6503b](https://github.com/quidow/gridfleet/commit/df6503bf88892874fd7cdb7a38df8ba2d7477fde))
* **backend:** keep run-create pokes sequential; shared AsyncSession is not concurrency-safe ([83d7278](https://github.com/quidow/gridfleet/commit/83d7278e8027d1264b83b283a7473d413b020798))
* **backend:** make appium resource-claim port uniqueness host-wide ([d12c424](https://github.com/quidow/gridfleet/commit/d12c4242900e97139ab945cd9a1681bb29607be9))
* **backend:** migrate pull-mode stale-clear call site to details= signature ([e91cef9](https://github.com/quidow/gridfleet/commit/e91cef9dd9402840d54a00748442277a839a807e))
* **backend:** preserve disconnected node stop contract ([cf393a1](https://github.com/quidow/gridfleet/commit/cf393a18cf73287015fc7acffe516020baff1956))
* **backend:** preserve drain flags in pulled launch payloads ([d4f60e4](https://github.com/quidow/gridfleet/commit/d4f60e4c2043cba020f2fc5e89864d0e80250987))
* **backend:** preserve grid create response wire statuses ([e2a3ff9](https://github.com/quidow/gridfleet/commit/e2a3ff90daf25c7c36c5ec3bd4d4cf44e95bc48c))
* **backend:** preserve remediation dispatch context in worker ([d54cb1d](https://github.com/quidow/gridfleet/commit/d54cb1dbcf3ef9c8f46a36d7667767a28b0cc589))
* **backend:** project reserved parallel-resource caps into pull desired launch ([789af1f](https://github.com/quidow/gridfleet/commit/789af1fa1a16e713c6910d827a5390e17e41ece2))
* **backend:** prune remediation logs for maintenance devices ([67244f1](https://github.com/quidow/gridfleet/commit/67244f17de0e1cdd0d14cb13d332d89338157182))
* **backend:** refuse launch payloads torn by a concurrent release switch ([988da6f](https://github.com/quidow/gridfleet/commit/988da6f10dfbc3c1d3f79168c58c9ec6394c4f67))
* **backend:** reject inactive fold lock proof ([02b8d04](https://github.com/quidow/gridfleet/commit/02b8d040d0a5a21f66a74f61d488c2ec65da049b))
* **backend:** require active target for release rollout ([e19a5cb](https://github.com/quidow/gridfleet/commit/e19a5cb89d93ca0c52376c1ef9f8e3a1386dfdeb))
* **backend:** reserve parallel-resource ports at operator node start ([376bcff](https://github.com/quidow/gridfleet/commit/376bcff174999effe96666a603a69c6106a58be4))
* **backend:** reset the remediation episode when entering maintenance ([1f8214e](https://github.com/quidow/gridfleet/commit/1f8214e7f430c04cb8029cda5d6456b01ef2316f))
* **backend:** retain remediation jobs whose failure episode is still open ([d21a948](https://github.com/quidow/gridfleet/commit/d21a9488ee9af3e8cdb98cac8804d79202f0915b))
* **backend:** skip maintenance devices in the connectivity fold ([3ffdaf9](https://github.com/quidow/gridfleet/commit/3ffdaf9007bfd72623e19ec5172d9de7eaabba59))
* **backend:** stamp pack release into node launch payloads ([b6f7ccd](https://github.com/quidow/gridfleet/commit/b6f7ccdc8ba1a2d05d2a715cf44f1e374e22be58))
* **backend:** stop discovery presence from disconnecting healthy devices ([b089a8c](https://github.com/quidow/gridfleet/commit/b089a8c1c3e9d8f43343fe6f112db32c630c3a02))
* **backend:** stop probing devices that are in maintenance ([052774d](https://github.com/quidow/gridfleet/commit/052774d3f8442e66ffd8ec30bfac41fd06300b2f))
* **backend:** tighten create protocol and deadline gates ([abfb042](https://github.com/quidow/gridfleet/commit/abfb0422aebba70ed83c8aedc1c67eaf7f09fb76))
* presence discovery must not disconnect healthy registered devices ([0bc7ac2](https://github.com/quidow/gridfleet/commit/0bc7ac2d4962e88591292a837d861892aa17c216))


### Performance Improvements

* **backend:** cache device health fold state presence ([d10654c](https://github.com/quidow/gridfleet/commit/d10654c81caf7e3459303c5c766d9d59fd44c112))
* **backend:** defer unused live_capabilities JSONB in the status-push folds ([2648842](https://github.com/quidow/gridfleet/commit/26488427eb83edfbe2cf4cb2269576d08b21e4e7))
* **backend:** drop the dead device eager-load in the node-health fold ([86d9988](https://github.com/quidow/gridfleet/commit/86d9988c09b80c168752b6e0c3a950bd3bc07c63))
* **backend:** drop the unused sessions eager-load in the node-health fold ([637c2ae](https://github.com/quidow/gridfleet/commit/637c2aefb08c5b7d425a951fc8dac0e8cd034155))
* **backend:** index sessions(started_at,id) for the session-list endpoints ([228cd79](https://github.com/quidow/gridfleet/commit/228cd7982c21565ca66493bece472b150541f23d))
* **backend:** keep unhealthy fold transition atomic ([30ce4a0](https://github.com/quidow/gridfleet/commit/30ce4a0adb078a38015cf4480e49162b912ea52c))
* **backend:** preload pack catalog for health folds ([730d440](https://github.com/quidow/gridfleet/commit/730d440817d48b405146b84e5a573882e0b1e3a4))
* **backend:** reuse device health fold locks ([9c455cb](https://github.com/quidow/gridfleet/commit/9c455cbc6aa06aa2ca3bb23b867116f683f7c9c8))
* **backend:** reuse device lock for self-heal reconciliation ([026e9cf](https://github.com/quidow/gridfleet/commit/026e9cf214d77bccfd8767a503016c25d990857d))
* **backend:** reuse fold lock for intent reconciliation ([50f8afb](https://github.com/quidow/gridfleet/commit/50f8afb180749510a6dd7774c27a5d6d9ba251e6))
* **backend:** reuse the pack catalog across a status-push device fold ([ceddace](https://github.com/quidow/gridfleet/commit/ceddace5e9a391b00205eff76ee9baa8a55cc06f))
* **backend:** skip identity-map sync on control-plane state deletes ([6dcd1a8](https://github.com/quidow/gridfleet/commit/6dcd1a8c9e0ec2c5b26bdb3b76519f4dec7a8c66))
* **backend:** snapshot control-plane state keys once per device fold ([7485e1f](https://github.com/quidow/gridfleet/commit/7485e1fff070eef168afaee645467da8a242d7c7))
* **backend:** stop persisting unread status-push snapshot sections ([6184e93](https://github.com/quidow/gridfleet/commit/6184e93a9ed3dc3382cf02a6ab4fdac0bf72f439))


### Dependencies

* **deps:** bump httpx2 in /backend in the python-dependencies group ([#834](https://github.com/quidow/gridfleet/issues/834)) ([34f0a7c](https://github.com/quidow/gridfleet/commit/34f0a7cf27286bb47da0b033e3b2ddb814a5ab9f))
* **deps:** bump mypy in /backend in the python-dependencies group ([#821](https://github.com/quidow/gridfleet/issues/821)) ([1dc2e4a](https://github.com/quidow/gridfleet/commit/1dc2e4a99549dfed4ec6274796635a9233dad49e))
* **deps:** bump ruff in /backend in the python-dependencies group ([#787](https://github.com/quidow/gridfleet/issues/787)) ([29e2ffa](https://github.com/quidow/gridfleet/commit/29e2ffa676311bbfa8bd73c44689c9e97734d61c))
* **deps:** bump the python-dependencies group ([#756](https://github.com/quidow/gridfleet/issues/756)) ([ac6c00d](https://github.com/quidow/gridfleet/commit/ac6c00d7da28fbd930f84ffaa83f45218533f090))
* **deps:** bump the python-dependencies group ([#844](https://github.com/quidow/gridfleet/issues/844)) ([31df2bc](https://github.com/quidow/gridfleet/commit/31df2bca5a6ca48811d7280005ff69b7a0feec4f))
* **deps:** bump uvicorn[standard] ([#767](https://github.com/quidow/gridfleet/issues/767)) ([e793f06](https://github.com/quidow/gridfleet/commit/e793f06e8426893bdd5d4168da9da0033c5bf74c))


### Documentation

* **backend:** correct the batched-prune migration COMMIT comment ([080846c](https://github.com/quidow/gridfleet/commit/080846cc8624cd63104dc17ead7dfa08f3214ac1))
* **backend:** disambiguate the two reconciler module families ([4086cdc](https://github.com/quidow/gridfleet/commit/4086cdc24a82d2875d0f8ce85ece797ff1fa7dd8))
* **backend:** document read-time operational-state projection ([326812a](https://github.com/quidow/gridfleet/commit/326812afee65110f6064f6e164d305c503f8d9b2))
* **backend:** document the restart watermark; drop token surfaces from ui ([a68330e](https://github.com/quidow/gridfleet/commit/a68330e1d41fcd9ce26d545a1fc237f1b49fd21a))
* **backend:** point pool-idle comments at the plumbing constant ([a5b8927](https://github.com/quidow/gridfleet/commit/a5b8927254b36fb4daf131b490315c65bf5e72ca))
* **backend:** record the device operational_state constructor alias as a deliberate shim ([2135e3c](https://github.com/quidow/gridfleet/commit/2135e3c2bf281b2e25f1b6f62f1da1bc3b1ff6cb))
* **docs:** reconcile design docs 01–05 to code truth + pin two enumerations ([ae34806](https://github.com/quidow/gridfleet/commit/ae3480671963190363553a40fb175d203829a6af))
* **main:** fix stale comments and settings doc drift ([508e051](https://github.com/quidow/gridfleet/commit/508e051f685eef325aaa88e1b892b633a1201b2b))
* **main:** resync settings.md and e2e mock after the registry residue deletion ([d5cf274](https://github.com/quidow/gridfleet/commit/d5cf274c5404b5a1d0eb72e8864c6e980e0f8915))


### Code Refactoring

* **backend:** store intent command kind as a column; drop the intent axis ([c9db02e](https://github.com/quidow/gridfleet/commit/c9db02e2c2f14ed7e9b0ac0fd1f4460692ac6014))

## [0.4.0](https://github.com/quidow/gridfleet/compare/gridfleet-backend-v0.3.0...gridfleet-backend-v0.4.0) (2026-06-26)


### ⚠ BREAKING CHANGES

* **backend:** OpenAPI schema BulkMaintenanceEnter removed; enter-maintenance endpoints now reference BulkDeviceIds.
* **agent:** the /agent/plugins and /agent/plugins/sync endpoints are removed and AppiumStartRequest no longer accepts a plugins field.
* **backend:** the /api/plugins and /api/hosts/{id}/plugins endpoints and the appium.default_plugins setting are removed.
* **backend:** the /api/webhooks endpoints and the webhooks / webhook_deliveries tables are removed.

### Features

* **agent:** remove appium plugins endpoints and runtime wiring ([45b78f6](https://github.com/quidow/gridfleet/commit/45b78f6f95add5bf32d0bd85c92bf48794b3fb79))
* **backend:** add eligible count and node routability reason to grid router ([6e945f6](https://github.com/quidow/gridfleet/commit/6e945f6e1f2dd8fd1b14fa5afd10b8ce42735a66))
* **backend:** remove appium plugins feature and drop its tables ([a1b513f](https://github.com/quidow/gridfleet/commit/a1b513fa4afe46b11616a1dde473f42912225e5b))
* **backend:** remove webhooks feature and drop its tables ([5382dda](https://github.com/quidow/gridfleet/commit/5382ddafc6236cb7995cbee8927ec3b60d83462c))


### Bug Fixes

* **backend:** unify driver-version drift so catalog and per-host surfaces agree ([4bf4de7](https://github.com/quidow/gridfleet/commit/4bf4de779bb801a95ffb364f3631dee12b9cce18))
* **backend:** update reconfigure call assertions for explicit timeout ([7934d3f](https://github.com/quidow/gridfleet/commit/7934d3f3ac3ea71f1d34412842c2497d57c248b9))


### Performance Improvements

* **backend:** drop never-surfaced per-stage verification data payload ([755528f](https://github.com/quidow/gridfleet/commit/755528f96bcb1407c570b676d880d2121a94979c))
* **backend:** fold orphan-claim reaping into reap_expired ([3fa9b77](https://github.com/quidow/gridfleet/commit/3fa9b77e4571f16a5e0362baa67528f48831fb82))
* **backend:** gate DeviceDetail.orchestration behind ?include=orchestration ([9a029b4](https://github.com/quidow/gridfleet/commit/9a029b42b7321d282ff4bb2899e555033a6cbfb9))
* **backend:** run orphaned-intent sweeps only on full-scan cycles ([4510ad7](https://github.com/quidow/gridfleet/commit/4510ad7f19a37ee72c74ec2dd973350e09e4413a))
* **backend:** skip no-op device row lock in confirm_running steady state ([84db417](https://github.com/quidow/gridfleet/commit/84db417d57aecaefd5fa521cdb32163c05641d06))
* **backend:** trim FleetOverview to the two fields the dashboard reads ([fae48d2](https://github.com/quidow/gridfleet/commit/fae48d270d4e894e19fdec51707606c9690cc8e0))


### Dependencies

* **backend:** bump pyjwt to 2.13.0 ([c88444d](https://github.com/quidow/gridfleet/commit/c88444d1a432bf349401657eeae0bb33df0e18d2))
* **deps:** bump ruff ([#674](https://github.com/quidow/gridfleet/issues/674)) ([b5fb7be](https://github.com/quidow/gridfleet/commit/b5fb7be8b4a5808869a8aa1c3ae7889e770a8c23))
* **deps:** bump the python-dependencies group ([#656](https://github.com/quidow/gridfleet/issues/656)) ([d5f3e3f](https://github.com/quidow/gridfleet/commit/d5f3e3f46375d6b1532194f807d1f21b6aab6156))
* **deps:** bump the python-dependencies group ([#698](https://github.com/quidow/gridfleet/issues/698)) ([4bdd76c](https://github.com/quidow/gridfleet/commit/4bdd76c18f378e98db85aae726b4e34d47241bbc))


### Documentation

* **backend:** correct stale metrics and grid router_internal docstrings ([4fa8f2f](https://github.com/quidow/gridfleet/commit/4fa8f2fa386e9984fccd32855f33f247179088bf))
* **backend:** fix record_background_loop_overrun docstring fragment ([8de2548](https://github.com/quidow/gridfleet/commit/8de2548d2a1976754f9918f767cc1684b085a094))


### Code Refactoring

* **backend:** fold bulk-maintenance-enter into bulk-device-ids ([e7abf48](https://github.com/quidow/gridfleet/commit/e7abf48ead0d3aa15aa6579a786d7cb2102e6291))

## [0.4.0](https://github.com/quidow/gridfleet/compare/gridfleet-backend-v0.3.0...gridfleet-backend-v0.4.0) (2026-06-26)


### ⚠ BREAKING CHANGES

* **backend:** OpenAPI schema BulkMaintenanceEnter removed; enter-maintenance endpoints now reference BulkDeviceIds.
* **agent:** the /agent/plugins and /agent/plugins/sync endpoints are removed and AppiumStartRequest no longer accepts a plugins field.
* **backend:** the /api/plugins and /api/hosts/{id}/plugins endpoints and the appium.default_plugins setting are removed.
* **backend:** the /api/webhooks endpoints and the webhooks / webhook_deliveries tables are removed.

### Features

* **agent:** remove appium plugins endpoints and runtime wiring ([45b78f6](https://github.com/quidow/gridfleet/commit/45b78f6f95add5bf32d0bd85c92bf48794b3fb79))
* **backend:** add eligible count and node routability reason to grid router ([6e945f6](https://github.com/quidow/gridfleet/commit/6e945f6e1f2dd8fd1b14fa5afd10b8ce42735a66))
* **backend:** remove appium plugins feature and drop its tables ([a1b513f](https://github.com/quidow/gridfleet/commit/a1b513fa4afe46b11616a1dde473f42912225e5b))
* **backend:** remove webhooks feature and drop its tables ([5382dda](https://github.com/quidow/gridfleet/commit/5382ddafc6236cb7995cbee8927ec3b60d83462c))


### Bug Fixes

* **backend:** unify driver-version drift so catalog and per-host surfaces agree ([4bf4de7](https://github.com/quidow/gridfleet/commit/4bf4de779bb801a95ffb364f3631dee12b9cce18))
* **backend:** update reconfigure call assertions for explicit timeout ([7934d3f](https://github.com/quidow/gridfleet/commit/7934d3f3ac3ea71f1d34412842c2497d57c248b9))


### Performance Improvements

* **backend:** drop never-surfaced per-stage verification data payload ([755528f](https://github.com/quidow/gridfleet/commit/755528f96bcb1407c570b676d880d2121a94979c))
* **backend:** fold orphan-claim reaping into reap_expired ([3fa9b77](https://github.com/quidow/gridfleet/commit/3fa9b77e4571f16a5e0362baa67528f48831fb82))
* **backend:** gate DeviceDetail.orchestration behind ?include=orchestration ([9a029b4](https://github.com/quidow/gridfleet/commit/9a029b42b7321d282ff4bb2899e555033a6cbfb9))
* **backend:** run orphaned-intent sweeps only on full-scan cycles ([4510ad7](https://github.com/quidow/gridfleet/commit/4510ad7f19a37ee72c74ec2dd973350e09e4413a))
* **backend:** skip no-op device row lock in confirm_running steady state ([84db417](https://github.com/quidow/gridfleet/commit/84db417d57aecaefd5fa521cdb32163c05641d06))
* **backend:** trim FleetOverview to the two fields the dashboard reads ([fae48d2](https://github.com/quidow/gridfleet/commit/fae48d270d4e894e19fdec51707606c9690cc8e0))


### Dependencies

* **backend:** bump pyjwt to 2.13.0 ([c88444d](https://github.com/quidow/gridfleet/commit/c88444d1a432bf349401657eeae0bb33df0e18d2))
* **deps:** bump ruff ([#674](https://github.com/quidow/gridfleet/issues/674)) ([b5fb7be](https://github.com/quidow/gridfleet/commit/b5fb7be8b4a5808869a8aa1c3ae7889e770a8c23))
* **deps:** bump the python-dependencies group ([#656](https://github.com/quidow/gridfleet/issues/656)) ([d5f3e3f](https://github.com/quidow/gridfleet/commit/d5f3e3f46375d6b1532194f807d1f21b6aab6156))
* **deps:** bump the python-dependencies group ([#698](https://github.com/quidow/gridfleet/issues/698)) ([4bdd76c](https://github.com/quidow/gridfleet/commit/4bdd76c18f378e98db85aae726b4e34d47241bbc))


### Documentation

* **backend:** correct stale metrics and grid router_internal docstrings ([4fa8f2f](https://github.com/quidow/gridfleet/commit/4fa8f2fa386e9984fccd32855f33f247179088bf))
* **backend:** fix record_background_loop_overrun docstring fragment ([8de2548](https://github.com/quidow/gridfleet/commit/8de2548d2a1976754f9918f767cc1684b085a094))


### Code Refactoring

* **backend:** fold bulk-maintenance-enter into bulk-device-ids ([e7abf48](https://github.com/quidow/gridfleet/commit/e7abf48ead0d3aa15aa6579a786d7cb2102e6291))

## [0.3.0](https://github.com/quidow/gridfleet/compare/gridfleet-backend-v0.2.0...gridfleet-backend-v0.3.0) (2026-06-22)


### ⚠ BREAKING CHANGES

* **backend:** clients pinning a device or routing by tag must send gridfleet:deviceId / gridfleet:tag:* instead of appium:gridfleet:*; the old prefix is rejected with an actionable error.
* **backend:** the `/api/diagnostics/devices/{device_id}/export`, `/snapshots`, and `/snapshots/{snapshot_id}` endpoints are removed.
* **backend:** POST /api/runs no longer accepts ?include=; the config, live_capabilities, test_data, and unavailable_includes fields are removed from reserved-device responses. Fetch device config/test_data/capabilities via the per-device GET endpoints instead.
* **backend:** flatten grid status response, drop synthetic selenium-hub envelope
* **backend:** drop dead session requested_* lane columns
* **backend:** remove requested_* scalar fields from SessionRead/SessionDetail
* **backend:** drop dead requested_* scalar fields from session event payloads
* **backend:** remove devices by-connection-target lookup endpoint
* **backend:** remove client session register/finished endpoints
* **backend:** GET /api/devices rejects status=reserved (422); use reserved=true instead. status=available now includes devices held by an active reservation; combine with reserved=false for the old behavior.
* **backend:** emit verdict payload on device.health_changed
* **backend:** key needs_attention and api schema on health verdicts
* **backend:** replace merged health summary with per-signal verdicts

### Features

* add Router page (grid visibility console) ([c4a0318](https://github.com/quidow/gridfleet/commit/c4a031836b447de19fe9992a9ccb79731d97f7a8))
* **agent:** carry generic recommended_action through pack health contract ([70291b5](https://github.com/quidow/gridfleet/commit/70291b5102a2d607040018b53282a72f113c93d6))
* **backend:** add allocatability projection helper and unavailable-reason enum ([a245604](https://github.com/quidow/gridfleet/commit/a245604e86cefafb069b91e6ef37d8673ff35a24))
* **backend:** add diagnostic metrics for 500s, db pool, allocate, loop overrun ([0630266](https://github.com/quidow/gridfleet/commit/06302668138cb45aef2ab9aa3dec6fd9c4962452))
* **backend:** add forced-release and appium-terminate failure counters ([7e42255](https://github.com/quidow/gridfleet/commit/7e422555d5882d709264d17f0f65d6d8f4f1d719))
* **backend:** add generic link-repair dispatch and attempt-budget helper ([d0e70d9](https://github.com/quidow/gridfleet/commit/d0e70d9fe7f4a9887caa0d526f91668201a1016a))
* **backend:** add GET /api/grid/router grid console endpoint ([70886be](https://github.com/quidow/gridfleet/commit/70886be71f72c555db7667efbcea3485bb00008c))
* **backend:** add operator session kill endpoint ([4f45863](https://github.com/quidow/gridfleet/commit/4f4586388ecd9b20483ad75fd09de3858ac0ed4e))
* **backend:** add per-device prefer_devicectl toggle for tvos ([2fe1f5a](https://github.com/quidow/gridfleet/commit/2fe1f5ad84ca7fb3822df4f59d48fea4729c5d5e))
* **backend:** add per-phase duration metrics to slow observation loops ([54758b9](https://github.com/quidow/gridfleet/commit/54758b9ad983935a15d6a734569e55f198052a8e))
* **backend:** add per-signal health filters to devices list ([e0bdc5f](https://github.com/quidow/gridfleet/commit/e0bdc5f466c2531b114e6788c9360bc3120ed210))
* **backend:** add preparation_failure_escalates_to_maintenance setting ([99b3968](https://github.com/quidow/gridfleet/commit/99b396896387fcf16bda508814755711be2040c8))
* **backend:** add probe-unanswered setting, repair metrics and event types ([d6ce2f0](https://github.com/quidow/gridfleet/commit/d6ce2f02339efae3ce1c2be1371000038384317b))
* **backend:** add release_device_from_run reservation primitive ([f88c29a](https://github.com/quidow/gridfleet/commit/f88c29aa101ad2cc4dd6ad730bef1e4bfa75cc43))
* **backend:** auto-dispatch adapter-recommended link repair from connectivity loop ([e5e1de7](https://github.com/quidow/gridfleet/commit/e5e1de7753182a008db68e7182d6fb6d95a8b035))
* **backend:** cap-aware grid idle reap honoring client newCommandTimeout ([3d6fc4b](https://github.com/quidow/gridfleet/commit/3d6fc4b1d3207f488a164ac4bba2df4acb6b415e))
* **backend:** conditional field requiredness for tvos wdaBaseUrl ([24fe6f7](https://github.com/quidow/gridfleet/commit/24fe6f769e258695f706a1b782faf1578882c801))
* **backend:** configurable uvicorn workers + parallel connectivity probes ([eca8e6a](https://github.com/quidow/gridfleet/commit/eca8e6afd069516bc74845caff9b700f9bd4a0ea))
* **backend:** cooldown-threshold escalation releases the device via shared routine ([3b09185](https://github.com/quidow/gridfleet/commit/3b09185471ce314dea8fae0fe6f7a6c0b9f632f0))
* **backend:** DB retention + index hygiene ([d35134c](https://github.com/quidow/gridfleet/commit/d35134c5385c9261e70eac081a8fc8bb4884d7ab))
* **backend:** derive needs_attention from the operational axis ([2f744ba](https://github.com/quidow/gridfleet/commit/2f744ba75ebd76c671692552e2a31b096ba18791))
* **backend:** detect and cure orphan adb-server systemPort socket ([4bc42f9](https://github.com/quidow/gridfleet/commit/4bc42f97de89818c52f66d89ca16a2a963c366c8))
* **backend:** diagnose run-create shortfall per gate and align availability ([726b7eb](https://github.com/quidow/gridfleet/commit/726b7ebeb2db711511f4302e3f6d1f9e61d553e5))
* **backend:** dispatch port-release cure with fresh session facts and rung instrumentation ([e2d8191](https://github.com/quidow/gridfleet/commit/e2d81910443bd6652587c19f0650684722cc6f37))
* **backend:** drop dead requested_* scalar fields from session event payloads ([7334d1b](https://github.com/quidow/gridfleet/commit/7334d1b516d939c16bb88a06f562341fb1458d95))
* **backend:** drop dead session requested_* lane columns ([265b12b](https://github.com/quidow/gridfleet/commit/265b12b89fa61e22b953260930303bf03bb9b123))
* **backend:** emit verdict payload on device.health_changed ([4e5e391](https://github.com/quidow/gridfleet/commit/4e5e3912bb29ac7856e2a0fe58491bdec4ede48b))
* **backend:** expose allocatable and unavailable_reason on device read dto ([bf4c474](https://github.com/quidow/gridfleet/commit/bf4c4740a500ca9da50c42bf8a7ff176d22b370d))
* **backend:** expose device allocatable and unavailable_reason projection ([dee5571](https://github.com/quidow/gridfleet/commit/dee557133acad7655a5c2f9b25b7525503b00e63))
* **backend:** flatten grid status response, drop synthetic selenium-hub envelope ([3b86fe8](https://github.com/quidow/gridfleet/commit/3b86fe8e6270a9b96f082b5a94abf3f90346aad9))
* **backend:** force-release verify-then-stop + collapse redundant grid intents (Stage 3) ([65a6ef3](https://github.com/quidow/gridfleet/commit/65a6ef38a80a023fc989a3756bcccb0ae66b2a1c))
* **backend:** gate grid allocation on appium node accepting_new_sessions ([056c780](https://github.com/quidow/gridfleet/commit/056c780b4f35cfdc16be1c83024fd13db6559701))
* **backend:** gate preparation-failure maintenance escalation on setting ([ce347ed](https://github.com/quidow/gridfleet/commit/ce347edfb288b33e6546a74bd6924089ff690fcc))
* **backend:** hard-stop on force-release only when the session survives the delete ([7b22b0a](https://github.com/quidow/gridfleet/commit/7b22b0af40750a59aa2026a54a556fe4a396a571))
* **backend:** honor client appium:newCommandTimeout in the grid idle reap under a hard ceiling ([7f76ecb](https://github.com/quidow/gridfleet/commit/7f76ecba8d71b3f8a5570d2fe7111d377d8addf2))
* **backend:** key needs_attention and api schema on health verdicts ([4429896](https://github.com/quidow/gridfleet/commit/442989636d9ac434c22bc5c3733c7065d4aa58ba))
* **backend:** make per-host probe concurrency a settings knob, default 4 ([5ed5a4c](https://github.com/quidow/gridfleet/commit/5ed5a4c60625fd16e24e224f98b3e74cc59ac571))
* **backend:** make preparation-failure maintenance escalation configurable ([3aeccc8](https://github.com/quidow/gridfleet/commit/3aeccc8bc3ee93360219b96f4ecbb776f85a3e45))
* **backend:** make uvicorn worker count configurable ([f6783a7](https://github.com/quidow/gridfleet/commit/f6783a7a77a4bd0c70271470701b75aefa44c81e))
* **backend:** mark devices unhealthy after consecutive unanswered probes ([8d6df3c](https://github.com/quidow/gridfleet/commit/8d6df3ca34e8ffe4f6127bcd1f8fbfa5bdb1a207))
* **backend:** park cooldown devices warm via the soft-gate ([ccfe3b1](https://github.com/quidow/gridfleet/commit/ccfe3b1c6bd82f765ecf8583346de4dfe9a51270))
* **backend:** prep-failure releases the device from its run via shared escalation routine ([0537e88](https://github.com/quidow/gridfleet/commit/0537e88a189367b4a5ffeed4a19cf7ed628136ed))
* **backend:** probe run-session survival after w3c delete on release ([02f7c11](https://github.com/quidow/gridfleet/commit/02f7c11adc8710b89b60d16270126992d12ae97e))
* **backend:** project cooldown warm-park as an unavailable reason ([0e2058c](https://github.com/quidow/gridfleet/commit/0e2058c9da9a3a57f58e50b2cb6d750c79247171))
* **backend:** prune system_events, terminal test_runs, and terminal jobs in data cleanup ([776dc06](https://github.com/quidow/gridfleet/commit/776dc068b9a33780926b1932074cfdd17a182dba))
* **backend:** publish lifecycle incidents to the event bus (F1) ([a8e3b05](https://github.com/quidow/gridfleet/commit/a8e3b0596c1db66bb127909618757dff8af6b5b4))
* **backend:** register retention knobs for system events, test runs, jobs ([8026d04](https://github.com/quidow/gridfleet/commit/8026d04f594532f375f5bf80a628279638553444))
* **backend:** remove client session register/finished endpoints ([a358d3d](https://github.com/quidow/gridfleet/commit/a358d3dd02c9667534f112b63000d43f4718a6e5))
* **backend:** remove device diagnostics feature ([6c73c47](https://github.com/quidow/gridfleet/commit/6c73c471f0404fe66f45c7df335da237c2308a68))
* **backend:** remove devices by-connection-target lookup endpoint ([e5dccfc](https://github.com/quidow/gridfleet/commit/e5dccfc7a2680490b88d2ed9fc66e2201d49638f))
* **backend:** remove requested_* scalar fields from SessionRead/SessionDetail ([d156888](https://github.com/quidow/gridfleet/commit/d1568887f51f7c8cfd360c50ed9492d65c01c36a))
* **backend:** rename device routing caps to the gridfleet vendor prefix ([e5a26ff](https://github.com/quidow/gridfleet/commit/e5a26ffb980e680ce546c3a13c311ad41d19440d))
* **backend:** replace merged health summary with per-signal verdicts ([6f90fae](https://github.com/quidow/gridfleet/commit/6f90faefdbf4811215f7d056bbccd1d6b1c3be25))
* **backend:** replace status=reserved device filter with orthogonal reserved boolean ([30d715d](https://github.com/quidow/gridfleet/commit/30d715d060aaf95945eb0188be65daec94695863))
* **backend:** report duration_seconds in system.cleanup_completed event ([59f97b3](https://github.com/quidow/gridfleet/commit/59f97b39e18cf6e62366584d630bafd0b6c6ee99))
* **backend:** report transitioning unavailable_reason for nodes mid-restart ([0b3c494](https://github.com/quidow/gridfleet/commit/0b3c494f105b0b56804c748391b3af66d8e79883))
* **backend:** retire runs ?include feature ([0d06451](https://github.com/quidow/gridfleet/commit/0d0645135284fa21875171f3e284d95388f57a39))
* **backend:** return device_id from internal grid allocate ([eae89b9](https://github.com/quidow/gridfleet/commit/eae89b97bc861ea19a34ba35c73877821e8f7e05))
* **backend:** shared deadlock/serialization retry helper ([9395f87](https://github.com/quidow/gridfleet/commit/9395f872269d09611917c5468755ab0a740bae76))
* **backend:** stage 4 — transitioning projection (P6) + post-grid cruft cleanup (P7) ([19183cf](https://github.com/quidow/gridfleet/commit/19183cf12746e2202dfeb46c60e7de340dbce211))
* **backend:** supply claimed ports and live-session facts to device health probes ([86addda](https://github.com/quidow/gridfleet/commit/86addda3c4c72524014b72af77699179243b3f44))
* **backend:** tvos appium_env rename and prefer_devicectl toggle ([2e52296](https://github.com/quidow/gridfleet/commit/2e5229642f69d13e5ba32510d3aacab434718cb4))
* **backend:** warm-park cooldown via the accepting_new_sessions soft-gate (Stage 2) ([7130430](https://github.com/quidow/gridfleet/commit/7130430c34fbed4550c198b4a0351a81e24be17d))
* **backend:** wire claimed ports and live-session fact into pack health probes ([abc9a15](https://github.com/quidow/gridfleet/commit/abc9a156cb4e69e53371421dfe47d78e55750b71))
* derive needs_attention from the operational axis and align the dashboard card ([c9d6a24](https://github.com/quidow/gridfleet/commit/c9d6a247fb5288a4de6ea300d190c812053966e1))
* **main:** add runtime_packages manifest field for required appium deps ([01383cd](https://github.com/quidow/gridfleet/commit/01383cd4b7d725572fe4c4056b70bca228797d97))
* sessions page rework — active/history tabs, capabilities, operator kill ([c8edbe9](https://github.com/quidow/gridfleet/commit/c8edbe9c4e52a87b561132e9166fe0544e53f7ac))
* split device health into per-signal verdicts (device / node / viability) ([ab11a4b](https://github.com/quidow/gridfleet/commit/ab11a4b91ff56f6af1806103157355ba5f37ab23))
* thread gridfleet device id into session caps; retire by-connection-target lookup ([0fe77ce](https://github.com/quidow/gridfleet/commit/0fe77ced0475597213d4bbf1eadc694d78856680))


### Bug Fixes

* **agent:** keep runtime-id stable for packs without runtime_packages ([06ab1d4](https://github.com/quidow/gridfleet/commit/06ab1d47fe069fb64d3104be7f948655b45cd80a))
* **agent:** surface os_version_display from normalize_device ([44365cb](https://github.com/quidow/gridfleet/commit/44365cb45d42128cf862a687498e5c2321a23485))
* **backend:** address grid router review (counts, ordering, trim, dedupe) ([f806b4f](https://github.com/quidow/gridfleet/commit/f806b4fafbe948dd87f75c6ff082d9e712dc5e6f))
* **backend:** allow run-scoped sessions during the preparing phase ([da77ea5](https://github.com/quidow/gridfleet/commit/da77ea58e946d8dea85e01b6ba71caf85a6d48b0))
* **backend:** allow run-scoped sessions during the preparing phase ([9d47783](https://github.com/quidow/gridfleet/commit/9d4778362771d827e3d27a1b793bedb199e030d8))
* **backend:** bound auto-recovery intents by deadline to stop premature reap ([6e12ba7](https://github.com/quidow/gridfleet/commit/6e12ba73ea052fc4195a6f16b1837fd5e0429593))
* **backend:** break import cycle from device-list serialization context ([cfce611](https://github.com/quidow/gridfleet/commit/cfce6113f7ea6eb4e8ffa9f39703e0394a7e970b))
* **backend:** bump xcuitest driver to 10.43.1 for remotexpc tunnel support ([30356e4](https://github.com/quidow/gridfleet/commit/30356e414eaa037a89cc07a3e54924e4d0703c8e))
* **backend:** chain actual_capabilities migration after merged repair-events migration ([bd01bd2](https://github.com/quidow/gridfleet/commit/bd01bd22325e8a22a3c84b26ef0c1926d69454f0))
* **backend:** check grid_allocation_reaper heartbeat in readiness (BL[#1](https://github.com/quidow/gridfleet/issues/1)) ([95e10ef](https://github.com/quidow/gridfleet/commit/95e10efa0cf1acf054443d504a9a7a60f2692ffc))
* **backend:** clear prior exclusion when releasing a device from its run ([402ba89](https://github.com/quidow/gridfleet/commit/402ba89fb544d8d0001b60b07838575e47505535))
* **backend:** clear repair/unanswered state-store keys on device delete ([4a4239b](https://github.com/quidow/gridfleet/commit/4a4239b734204c6b29d71879e660ff1168fa7b9b))
* **backend:** close leader advisory connection on non-adopt paths ([37f79ea](https://github.com/quidow/gridfleet/commit/37f79eab069a373604d2c0302e8502fedfe5a631))
* **backend:** close stale node observation wedge behind exit-maintenance ([2a47b16](https://github.com/quidow/gridfleet/commit/2a47b169e96ef93f1701673ba8c7524612953ca6))
* **backend:** coerce bool device_config fields at write boundaries ([97a256f](https://github.com/quidow/gridfleet/commit/97a256f2b1d30a69c4ec5a2556dd9240f8c976fb))
* **backend:** compare appium_env device gates against schema field defaults ([b85d552](https://github.com/quidow/gridfleet/commit/b85d55211179012fce13a5ca77b0af7960d412a5))
* **backend:** cooldown escalation status reflects the maintenance toggle ([7e43d2e](https://github.com/quidow/gridfleet/commit/7e43d2e3b3a77ad6ff89018dadd9b33d430ec96b))
* **backend:** correct node-state gate to allow recovery via health_running=None ([e33001f](https://github.com/quidow/gridfleet/commit/e33001ff4d13adf245ee8f3fe987083760f271fc))
* **backend:** debounce flaky health checks and restore self-healed devices to their run ([51a11b7](https://github.com/quidow/gridfleet/commit/51a11b786791fe646b829f68cc78cbd2d4ae3955))
* **backend:** debounce transient health-check failures before run exclusion ([80f1952](https://github.com/quidow/gridfleet/commit/80f1952ea48b6f969d7bb648a447e9d9ce24bb6a))
* **backend:** defer device claims while a viability probe is in flight ([b73e571](https://github.com/quidow/gridfleet/commit/b73e571db2fda46ce02bc282ab4144e3d84fc234))
* **backend:** delete device without busy-waiting on node stop ([402181d](https://github.com/quidow/gridfleet/commit/402181d0f33618793529026bd883a7094aa16218))
* **backend:** delete device without busy-waiting on node stop ([a1f86b4](https://github.com/quidow/gridfleet/commit/a1f86b45ceff582a65fe4c5a4f34014e11b12571))
* **backend:** eager-fill node viability marker on restart_succeeded (I11/N15) ([7042dd0](https://github.com/quidow/gridfleet/commit/7042dd09376bbcf2243c3ddc9f5b1488e0109831))
* **backend:** easy open-issues batch (audit rows, config coercion, env gate defaults) ([7396db1](https://github.com/quidow/gridfleet/commit/7396db1f6c4f56e9cef1e1d2c15fe25d1dec15a5))
* **backend:** eliminate session-teardown deadlock 500s ([fdd29c0](https://github.com/quidow/gridfleet/commit/fdd29c058810adfb0a7153f303050b87415c962e))
* **backend:** emit canonical state event on session start via reconciler-derived busy ([01307a6](https://github.com/quidow/gridfleet/commit/01307a683d4ba4a9720ddf0a23b2bd2694ff10ca))
* **backend:** exclude sse event stream from http duration histogram ([cde5877](https://github.com/quidow/gridfleet/commit/cde58779f8497dc31008368436f91147c3b06073))
* **backend:** exempt grid allocate long-poll from request timeout middleware ([eaf8a52](https://github.com/quidow/gridfleet/commit/eaf8a52128aa55c2b4e61e7b82541b45aec62e34))
* **backend:** expire grid ticket in testkit session-close paths ([f0788dc](https://github.com/quidow/gridfleet/commit/f0788dc2d5e79b862472eed854a345871bc5be4d))
* **backend:** expire grid ticket in testkit session-close paths (audit M2) ([3abfdf6](https://github.com/quidow/gridfleet/commit/3abfdf641f823f7edc3aac502ace8aa5ae789613))
* **backend:** expose released_at in reserved-device info so the run API distinguishes released devices ([1dffb93](https://github.com/quidow/gridfleet/commit/1dffb939c58ccdd35834f6a75cd7b534420400e0))
* **backend:** failed verification shelves via review_required, not operator:stop ([42dcad3](https://github.com/quidow/gridfleet/commit/42dcad3890dd68e9d83a5731d385cfeb44fafa6c))
* **backend:** failed verification shelves via review_required, not operator:stop ([8e15f63](https://github.com/quidow/gridfleet/commit/8e15f63dbb94d3732e03e1d242e3758e1f17d107))
* **backend:** fall back to DB load when prefetched pack catalog lacks a pack_id ([3fa511c](https://github.com/quidow/gridfleet/commit/3fa511cb5934b4355863ed6c175df6199c93640b))
* **backend:** guard --workers injection to the uvicorn command ([436e685](https://github.com/quidow/gridfleet/commit/436e685fce6478bbb208b97afd2dfbb92a2e2411))
* **backend:** guard recovery job device_id read so malformed payloads fail the job ([cb7cfd2](https://github.com/quidow/gridfleet/commit/cb7cfd246ce6e5d50dcfcaa66de1debbf1b43d10))
* **backend:** hard-stop force-released sessions with an unresolvable appium target ([151dbd6](https://github.com/quidow/gridfleet/commit/151dbd6f74e6294966f631583c90d0b60b798ca9))
* **backend:** harden reconfigure 404 path, repair re-probe, and link-repair args ([0db7faa](https://github.com/quidow/gridfleet/commit/0db7faa19d6dc0a71a198e92816568d131f2dfce))
* **backend:** keep force-released sessions marked error by closing rows after cancellation ([71fb0b9](https://github.com/quidow/gridfleet/commit/71fb0b9aa21e10bfa3c3aa00eccc2405e30c33c7))
* **backend:** lease-backed verifying entry closes update-mode derivation race ([db606f7](https://github.com/quidow/gridfleet/commit/db606f77e2bff29939effb2236709afdb91b6a4f))
* **backend:** lock device before session row in close_running_session ([8ae8b07](https://github.com/quidow/gridfleet/commit/8ae8b07273e8cc6e0d2572d157e1ea2268830682))
* **backend:** lock device row before revoking failure-stop intents on verify ([19170c9](https://github.com/quidow/gridfleet/commit/19170c9fe19079a3f4b474c785f2804585ac40e0))
* **backend:** lock device row before session row writes in close paths ([1f8f0d1](https://github.com/quidow/gridfleet/commit/1f8f0d1f361618b3637f10e387d9df678b07ca49))
* **backend:** lock device row before verification update-path desired-state write ([afa3776](https://github.com/quidow/gridfleet/commit/afa37761bbc21f1ec7254868a73dce2c5aab4d57))
* **backend:** make device allocatability gate-honest and fail-closed ([bb14267](https://github.com/quidow/gridfleet/commit/bb142676b5e85cddf050c32acc5f87dfdd4c8cf7))
* **backend:** make grid router nodes/queue required for non-optional TS types ([7b56763](https://github.com/quidow/gridfleet/commit/7b567630fef6c0a523575112fc439b9ebfc6be67))
* **backend:** map passed session status to success event severity ([9a634a1](https://github.com/quidow/gridfleet/commit/9a634a1df745eec8237573fe0f1a4098e20fc342))
* **backend:** migrate deviceeventtype enum for repair audit values ([729c4c4](https://github.com/quidow/gridfleet/commit/729c4c43104d35904f9339a6e8b0a188e0c4d2b6))
* **backend:** no-op reconcile_device when the device row vanished ([265a593](https://github.com/quidow/gridfleet/commit/265a5930a25ac27caf35af94203940b0cf262f7e))
* **backend:** observation-loop tuning, request-timeout safety, and metric attribution ([91cbf6d](https://github.com/quidow/gridfleet/commit/91cbf6d27a845a87a379c1ae88c44b2617cec1e3))
* **backend:** percent-encode session id in appium terminate url ([dfd0033](https://github.com/quidow/gridfleet/commit/dfd00338ca094cbaa4ba74bb8d79c5432d998e95))
* **backend:** populate Session.test_name from gridfleet:testName in grid allocation ([18ac776](https://github.com/quidow/gridfleet/commit/18ac776c0344897f184ab53d9d814d444a75181c))
* **backend:** pre-register wake-source metric label values ([1cbc709](https://github.com/quidow/gridfleet/commit/1cbc7098b0d4fff587237d46b8ba4974829a4e26))
* **backend:** re-read committed run state when closing a session ([ef3e60b](https://github.com/quidow/gridfleet/commit/ef3e60b2cdfb63aaf938b4d7e9f5abeb43efa627))
* **backend:** re-read committed run state when closing a session ([e214d97](https://github.com/quidow/gridfleet/commit/e214d97eeb8e80f537d34546dd487220b43ed226))
* **backend:** record device events for operator stop and free-session start and end ([4d36420](https://github.com/quidow/gridfleet/commit/4d3642012de499882e89a73ecdd2c25ddee6f97f))
* **backend:** release skip_when-excluded resource port claims on node start ([d029c8f](https://github.com/quidow/gridfleet/commit/d029c8ffe9db6b5395a06883ca0538ba66582aeb))
* **backend:** release skip_when-excluded resource port claims on node start ([fa5809b](https://github.com/quidow/gridfleet/commit/fa5809bdb5c094999187181efcdddfeb7cc3e505))
* **backend:** release viability probe lock on post-claim gating failure ([9a884a6](https://github.com/quidow/gridfleet/commit/9a884a6d04d72614dab642a61567c11ebbf8e41f))
* **backend:** release_device_from_run revokes full intent set, leaves row not-excluded ([3f946fd](https://github.com/quidow/gridfleet/commit/3f946fdeb55e3ca22f5c874255d4726d80585e86))
* **backend:** reset agent circuit breaker on host re-registration ([558f02a](https://github.com/quidow/gridfleet/commit/558f02acb426e1c5975bdd4d31df139ff2768a00))
* **backend:** resolve open grid-findings review issues (round 2) ([fdfc4bd](https://github.com/quidow/gridfleet/commit/fdfc4bd7f1c24d40bf53f271e9292afd33bb6949))
* **backend:** restore run reservation when an excluded device self-heals ([421e2b5](https://github.com/quidow/gridfleet/commit/421e2b5c2ba955a5c1fda40537b31426e981a730))
* **backend:** retry run allocation when skip-locked drops a transiently locked device ([90eb979](https://github.com/quidow/gridfleet/commit/90eb9795f48ca6f73ece85a8df30312614731626))
* **backend:** retry run allocation when skip-locked drops a transiently locked device ([da2977f](https://github.com/quidow/gridfleet/commit/da2977f441a7f3f669a1bb75eca5ef4b7e0b1871))
* **backend:** retry session-teardown handlers on transient deadlock ([7b6bc86](https://github.com/quidow/gridfleet/commit/7b6bc86b03fc9e217c154e19bf0960ddbbd2b97f))
* **backend:** retry stale-keepalive disconnect on pooled idempotent agent calls ([00493e7](https://github.com/quidow/gridfleet/commit/00493e799beffaf5f405eb25d552427834b4585f))
* **backend:** retry terminal run transitions on postgres deadlock ([8abd6eb](https://github.com/quidow/gridfleet/commit/8abd6eb127a50a99c67fa407d4dc8a3cd9b28f50))
* **backend:** revoke health-failure stop intents on verification node start ([c067d33](https://github.com/quidow/gridfleet/commit/c067d3377b1540cd94d8b51d29df41599cd37c4e))
* **backend:** revoke health-failure stop intents on verification node start ([8c8c073](https://github.com/quidow/gridfleet/commit/8c8c073cb6add3b456cfa350718f93102c506204))
* **backend:** run teardown deadlock leaking reservations + shortfall diagnostics ([14eed21](https://github.com/quidow/gridfleet/commit/14eed21530a61fb6a6e4f1fba2f91c785bd1f425))
* **backend:** run-failure escalation no longer marks the device unhealthy ([0d9d89e](https://github.com/quidow/gridfleet/commit/0d9d89e84587661ccec51c5d240f40221ae1dd3f))
* **backend:** skip mjpegServerPort allocation for tvos devicectl devices ([2054231](https://github.com/quidow/gridfleet/commit/20542313f4a9ab8a1b12ef8c34704fb676a2665a))
* **backend:** skip mjpegServerPort allocation for tvos devicectl devices ([2e42700](https://github.com/quidow/gridfleet/commit/2e42700c91854cb0961f1f3cd0a794c54810aef4))
* **backend:** sleep interval minus cycle elapsed in BackgroundLoop ([68a419b](https://github.com/quidow/gridfleet/commit/68a419b1f5dcd998acd6fc0318ef97f5b765e788))
* **backend:** sleep interval minus cycle elapsed in BackgroundLoop (audit H1) ([2b0a046](https://github.com/quidow/gridfleet/commit/2b0a04628dac5eac959d95ac4287bdd736ccb673))
* **backend:** stop node_health escalating restarts on intentionally-stopped nodes ([a8aef14](https://github.com/quidow/gridfleet/commit/a8aef14d0b47a0cc7d78d171ae1d74e5e3e40fb2))
* **backend:** sweep orphaned claimed queue tickets in the reaper ([21be8db](https://github.com/quidow/gridfleet/commit/21be8dba2b6de9e564acc6cd90c5ff8ffed4bb55))
* **backend:** treat recovery probe-collision skip as benign, not suppressed ([52e8f2c](https://github.com/quidow/gridfleet/commit/52e8f2ca1df7bb7afa5e2ce3cc6e3e29ffe634af))
* **backend:** update grid cooldown mocks and testkit type for 5-tuple/released status ([3a51fd8](https://github.com/quidow/gridfleet/commit/3a51fd898c62826d8428631f20d245e78a3bb7ba))
* **backend:** widen create_run re-match retry to bridge measured lock holds ([c821d9f](https://github.com/quidow/gridfleet/commit/c821d9fcfac9d160e6cb0a6b93069fdd82ce6c3b))
* **backend:** wire device-state gating-violation tripwire at session registration ([00165b4](https://github.com/quidow/gridfleet/commit/00165b4f017f4dbfdc3d08be201947d18d5de48e))
* **backend:** wire device-state gating-violation tripwire at session registration ([2932aee](https://github.com/quidow/gridfleet/commit/2932aee58ca801b17a9d7caec6507e4a93e59cca))
* batch of open grid/lifecycle issues (recovery-skip, queue hygiene, BL[#1](https://github.com/quidow/gridfleet/issues/1), F1, I11/N15, test_name, frontend) ([673c4d8](https://github.com/quidow/gridfleet/commit/673c4d8a5c911da38cd7dcb39d966d949b97dd77))
* stop spurious 409 on Test Session from leaked viability probe lock ([1a0731b](https://github.com/quidow/gridfleet/commit/1a0731b5341c669570e903129487cb94bbbc5e94))


### Performance Improvements

* **backend:** batch device load for run-release target resolution ([e9620e7](https://github.com/quidow/gridfleet/commit/e9620e70f294257d48fcc29f164748d9a980d115))
* **backend:** batch driver-pack catalog in reconciler loops ([3c79cd0](https://github.com/quidow/gridfleet/commit/3c79cd04e745b12a501a592ea72580d2ecfc7aab))
* **backend:** batch driver-pack lookups in device list serialization ([0405dc5](https://github.com/quidow/gridfleet/commit/0405dc572d96c0c68b4ee587f7baae9ed0fe2346))
* **backend:** batch driver-pack lookups in device list serialization ([6c8cd59](https://github.com/quidow/gridfleet/commit/6c8cd59f35ddf81a23ff8ed1109378c81a5d3a17))
* **backend:** cap device row-lock windows in connectivity sweep + widen create_run retry ([86a4707](https://github.com/quidow/gridfleet/commit/86a47072407a851cc610edb7d8504dfd5a225b28))
* **backend:** check viability probe lock before device row lock in grid claim ([f3d0227](https://github.com/quidow/gridfleet/commit/f3d0227b0f44190b1c2741b2d0780f1d309b4eea))
* **backend:** commit per device in connectivity sweep to cap row-lock windows ([1551ef9](https://github.com/quidow/gridfleet/commit/1551ef931c3b6f654bcf4fa99ff1a1c5e171cb44))
* **backend:** cut device_intent_reconciler per-device DB cost ([c9feff0](https://github.com/quidow/gridfleet/commit/c9feff0de2edd87796e9858b734b24e7f3f008ec))
* **backend:** drop per-reconcile COUNT(*); publish intent gauge per scrape ([8ac95ba](https://github.com/quidow/gridfleet/commit/8ac95ba29bb08fc8dd17fd500534df7bfae52942))
* **backend:** drop redundant single-column indexes shadowed by composites ([72d0383](https://github.com/quidow/gridfleet/commit/72d0383efbe360600b16dabd529c574266a140f4))
* **backend:** fetch lifecycle states concurrently in connectivity probe phase ([43466e7](https://github.com/quidow/gridfleet/commit/43466e75dd71235157906872c5e772eb3fb3fc91))
* **backend:** freeze startup heap and raise gen0 gc threshold ([c28ddfa](https://github.com/quidow/gridfleet/commit/c28ddfad902eeef869311d4a698952a86cae5e92))
* **backend:** gate device-checks healthy mark_dirty on verdict change ([62a47de](https://github.com/quidow/gridfleet/commit/62a47de8ef82164c5bf9aaf9be581e86bc310fa5))
* **backend:** gate session-viability passed mark_dirty on verdict change ([433ed1f](https://github.com/quidow/gridfleet/commit/433ed1f36707df900b0b824857bb577732c6d0a8))
* **backend:** index fk columns hit by parent-row deletes ([34396ac](https://github.com/quidow/gridfleet/commit/34396ac18fd7f18c97f1bfe8e6253e93510e8b98))
* **backend:** parallelize device connectivity health probes per host ([392cfe6](https://github.com/quidow/gridfleet/commit/392cfe67fe20458b76c7979afe1e389afd1f5f59))
* **backend:** parallelize device_connectivity across hosts; drop per-reconcile COUNT(*) ([3232f2c](https://github.com/quidow/gridfleet/commit/3232f2ccf1c9ac622fb1fc8c99a2e65985e601c8))
* **backend:** probe device_connectivity across hosts in one gather ([3b67f1b](https://github.com/quidow/gridfleet/commit/3b67f1b50685bf9257d1cd5ba1ed179bfda7e925))
* **backend:** reduce reconciler churn via transition-gated mark_dirty ([526b3b2](https://github.com/quidow/gridfleet/commit/526b3b2c896c2e6f827f7e8c706f96d8e5acdfc1))
* **backend:** run the two per-device connectivity probe reads concurrently ([d6f8c0e](https://github.com/quidow/gridfleet/commit/d6f8c0e9397940132ce024867df2a3d9b9ede443))
* **backend:** run the two per-device connectivity probe reads concurrently (audit M3 step 1) ([1ec8729](https://github.com/quidow/gridfleet/commit/1ec872965ae06fe2da78e7ab0814d78334b8f5e8))
* **backend:** skip node-state reconcile when health columns unchanged ([2f36945](https://github.com/quidow/gridfleet/commit/2f369459ee525e6e28942f138db39f6d2a307ebb))
* **backend:** skip redundant device reload in gather_device_state_facts ([d5e554a](https://github.com/quidow/gridfleet/commit/d5e554abc837df6955c57bb0a714c513bfffd0eb))
* **backend:** skip redundant reconcile for already-recorded disconnect ([0d8e0e4](https://github.com/quidow/gridfleet/commit/0d8e0e4fd79fe89a750139caa7f31f3722a2239d))


### Dependencies

* **backend:** migrate from httpx to httpx2 ([dfdc0f3](https://github.com/quidow/gridfleet/commit/dfdc0f399fa56dbc4cc1030904b488c09d56eb27))
* **deps:** bump datamodel-code-generator ([df68d31](https://github.com/quidow/gridfleet/commit/df68d312ef4f4942992ee13be4f029409d563f24))
* **deps:** bump datamodel-code-generator ([#563](https://github.com/quidow/gridfleet/issues/563)) ([0a304b0](https://github.com/quidow/gridfleet/commit/0a304b017765f98b0531a65ef925f9b4ec18bd5e))
* **deps:** bump pydantic-settings from 2.14.1 to 2.14.2 in /backend ([#643](https://github.com/quidow/gridfleet/issues/643)) ([32130e8](https://github.com/quidow/gridfleet/commit/32130e8c508fdde9c0e0e44f5d9d679f22521963))
* **deps:** bump ruff in /backend in the python-dependencies group ([#630](https://github.com/quidow/gridfleet/issues/630)) ([26cecd2](https://github.com/quidow/gridfleet/commit/26cecd2eadc70d22417cb295463729c2b4c46b0a))
* **deps:** bump starlette from 1.0.1 to 1.3.1 in /backend ([#607](https://github.com/quidow/gridfleet/issues/607)) ([8e1505f](https://github.com/quidow/gridfleet/commit/8e1505f40dd5d96337721ea46fd2c989b94530ab))
* **deps:** bump the python-dependencies group ([#543](https://github.com/quidow/gridfleet/issues/543)) ([11a4448](https://github.com/quidow/gridfleet/commit/11a44482d6de8fd0aa55d9d362eb4dfd8a714011))
* **deps:** bump the python-dependencies group ([#586](https://github.com/quidow/gridfleet/issues/586)) ([8d284e5](https://github.com/quidow/gridfleet/commit/8d284e5be58ab5539b3f2a1786b97cac7cb4e787))
* **deps:** bump the python-dependencies group ([#617](https://github.com/quidow/gridfleet/issues/617)) ([64fad08](https://github.com/quidow/gridfleet/commit/64fad08cd7026c40dfa04c922dc26661009727ae))
* **deps:** bump the python-dependencies group across 1 directory with 4 updates ([#604](https://github.com/quidow/gridfleet/issues/604)) ([fb2d46d](https://github.com/quidow/gridfleet/commit/fb2d46d18d86af34d1c6b20cd5c84430d8ce99f2))


### Documentation

* **backend:** correct W3C matcher attribution in viability probe comment ([c9519d2](https://github.com/quidow/gridfleet/commit/c9519d2ef4055d5127c091c49ab97b922e2573fd))
* **backend:** de-relay stale selenium hub/relay comments ([fd35002](https://github.com/quidow/gridfleet/commit/fd350029af83a95684c618a5bae7d8ebe026c6e0))
* **backend:** de-relay stale selenium hub/relay comments ([56d950f](https://github.com/quidow/gridfleet/commit/56d950fd58c54e7de6cbde5dc68cfa54e8798ce1))
* **backend:** mark http-pool-disabled agent calls as dev-only ([6701584](https://github.com/quidow/gridfleet/commit/67015841b7a1ec15dd6232a44ed7576f9c838f99))
* **backend:** note node_health doorbell has no production ringer ([e16997b](https://github.com/quidow/gridfleet/commit/e16997b5cee169cf9608800f88c51b0b9ef2ee56))
* **docs:** document agent keep-alive env var and shim transition status ([290646e](https://github.com/quidow/gridfleet/commit/290646e15effeacc49c44f4a3aac0cd22e38a453))
* **docs:** retire Selenium Grid-era session-registration references ([34a11d7](https://github.com/quidow/gridfleet/commit/34a11d717057ad7756c7f01115bcf1464caff24d))

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
