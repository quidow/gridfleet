# Runbook: node-pull cutover (phase 8c) precondition

Before merging the push-path deletion PR, confirm the whole fleet runs the
node-pull agent release (orchestration contract v3, capability `node_desired_pull`).

## Audit query (run against the backend Postgres)

```sql
SELECT agent_id,
       agent_version,
       capabilities ->> 'orchestration_contract_version' AS contract,
       capabilities ->> 'node_desired_pull'              AS node_pull
FROM hosts
WHERE (capabilities ->> 'orchestration_contract_version')::int < 3
   OR  capabilities ->> 'node_desired_pull' IS NULL;
```

**Go condition:** zero rows. Any row is a host that will lose node
orchestration the moment push is deleted — upgrade or decommission it first.

## After the deletion PR merges

`MIN_ORCHESTRATION_CONTRACT_VERSION` is 3. A pre-pull agent (contract < 3) is
rejected at registration and marked `offline` at scheduler startup by
`_validate_online_agent_contracts`. This is the backstop, not the plan — the
audit above is the gate.
