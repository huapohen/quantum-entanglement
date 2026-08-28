# Approval policy control store deployment

This deployment unit creates the PostgreSQL durability boundary for authority approval-policy
activation. It is intentionally separate from the target IM database: restoring, replacing, or
cutting over the IM target must not roll back this database at the same time.

The store has three database roles:

- `owner_role` is a dedicated `NOLOGIN` role that owns the database, schema, tables, and fixed
  `SECURITY DEFINER` functions.
- `reader_role` is a dedicated `LOGIN` role with `CONNECT`, schema `USAGE`, and exact `EXECUTE`
  grants on the identity/current-state functions.
- `activator_role` is a separate `LOGIN` role with the reader surface plus exact `EXECUTE` on the
  CAS function. Neither login receives direct table or sequence privileges. The ordinary IM
  runtime must never receive the activator credential.

Provision the roles and database from an administrative connection. Supply the writer credential
through the deployment secret manager; never place it in command history or these files.

```bash
psql "$POSTGRES_ADMIN_URL" \
  --set=control_database=wanwork_policy_control_prod \
  --set=owner_role=wanwork_policy_control_owner \
  --set=reader_role=wanwork_policy_control_reader \
  --set=activator_role=wanwork_policy_control_activator \
  --file=deploy/postgres/approval-policy-control-store/bootstrap_cluster.psql
```

Grant the provisioner permission to `SET ROLE` to the owner without allowing the writer to do so.
Then connect to the dedicated database and install the create-only schema:

```bash
psql "$POSTGRES_CONTROL_ADMIN_URL" \
  --set=owner_role=wanwork_policy_control_owner \
  --set=reader_role=wanwork_policy_control_reader \
  --set=activator_role=wanwork_policy_control_activator \
  --file=deploy/postgres/approval-policy-control-store/schema.psql
```

Both scripts fail closed on existing objects. Upgrades require a new reviewed migration and a new
schema contract digest; operators must not use `CREATE OR REPLACE`, repair functions, or direct
archive mutation. The Go service verifies the expected database, login, owner, TLS identity,
PostgreSQL major, physical system identifier, compatibility digest, and catalog surface on every
connection before reading or activating a policy.

This database prevents an IM target-only restore from rolling approval policy back. Its owner and
the platform's whole-cluster restore authority remain trusted boundaries. Production promotion
also requires an off-host immutable high-water/WORM anchor so a control-database snapshot rollback
is detectable; this deployment unit does not claim to defeat its own infrastructure administrator.
