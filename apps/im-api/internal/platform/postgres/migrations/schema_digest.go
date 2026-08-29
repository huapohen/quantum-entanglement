package migrations

import (
	"context"
	"crypto/sha256"
	"encoding/binary"
	"encoding/hex"
	"hash"
	"strconv"

	"github.com/jackc/pgx/v5"
)

const (
	schemaPostconditionDigestDomain       = "wanwork.im/postgres-schema-postconditions/1\n"
	identityAuthoritySchemaDigest         = "9a178617cbb463df31450f4302454ae4eba101dd2d2f8b2567dad7f49088c5d5"
	conversationSchemaDigest              = "17002b4c0b7a757e23a96418634af02c517aa85a4bae415175ab33e75cff8457"
	conversationAuthoritySchemaDigest     = "b500175ab19a74fdd1f4cf810906318f9d76e1f1113cad59ec9cd0aa1dde6d34"
	eventStoreSchemaDigestV6              = "98ac506434a8c589e87f769236664919f09b93cccb8b0ade2d46902cc07900b7"
	eventStoreSchemaDigestV7              = "37c332bdf52c8cf4ed463c9dedc715ab1e5f5c10ff3ae88356a41e339d5bedaa"
	eventProjectionCheckpointSchemaDigest = "3b7ac11608f3537efa6314b7afdad67c23e24f3b3c72025ab1bfdaf1664700a9"
	nativeIMInboxSchemaDigest             = "0ede38802eaac5c9d05b4e66094b76b7c1f1def5730f1e5b07e8f99c98b697c0"
)

var identityAuthorityTableNames = []string{
	"actor_heads",
	"actor_snapshots",
	"human_identity_binding_heads",
	"human_identity_binding_snapshots",
	"human_principal_heads",
	"human_principal_snapshots",
	"provider_actor_binding_heads",
	"provider_actor_binding_snapshots",
	"tenant_membership_heads",
	"tenant_membership_snapshots",
}

var conversationTableNames = []string{
	"conversation_heads",
	"conversation_snapshots",
	"provider_conversation_binding_heads",
	"provider_conversation_binding_snapshots",
}

var conversationAuthorityTableNames = []string{
	"conversation_access_heads",
	"conversation_access_snapshots",
	"conversation_membership_heads",
	"conversation_membership_snapshots",
	"tenant_command_receipts",
}

var eventStoreTableNames = []string{
	"event_log",
	"event_stream_heads",
	"event_tenant_heads",
}

var eventProjectionCheckpointTableNames = []string{
	"event_projection_checkpoints",
}

var nativeIMInboxTableNames = []string{
	"native_im_inbox",
}

func tableSchemaDigest(
	ctx context.Context,
	transaction pgx.Tx,
	tableNames []string,
) (string, error) {
	digest := sha256.New()
	writeDigestFields(digest, schemaPostconditionDigestDomain)
	if err := digestRelations(ctx, transaction, digest, tableNames); err != nil {
		return "", err
	}
	if err := digestColumns(ctx, transaction, digest, tableNames); err != nil {
		return "", err
	}
	if err := digestConstraints(ctx, transaction, digest, tableNames); err != nil {
		return "", err
	}
	if err := digestIndexes(ctx, transaction, digest, tableNames); err != nil {
		return "", err
	}
	if err := digestPolicies(ctx, transaction, digest, tableNames); err != nil {
		return "", err
	}
	return hex.EncodeToString(digest.Sum(nil)), nil
}

func digestRelations(
	ctx context.Context,
	transaction pgx.Tx,
	digest hash.Hash,
	tableNames []string,
) error {
	writeDigestFields(digest, "relations")
	rows, err := transaction.Query(ctx, `
SELECT relation.relname,
       relation_owner.rolname = current_user,
       relation.relkind::text,
       relation.relpersistence::text,
       relation.relreplident::text,
       relation.relrowsecurity,
       relation.relforcerowsecurity,
       relation.relispartition,
       COALESCE(array_to_string(relation.reloptions, ','), ''),
       NOT EXISTS (
           SELECT 1
           FROM pg_catalog.aclexplode(
               COALESCE(
                   relation.relacl,
                   pg_catalog.acldefault('r', relation.relowner)
               )
           ) AS acl
           WHERE acl.grantee = 0
       ),
       NOT EXISTS (
           SELECT 1
           FROM pg_catalog.pg_trigger AS trigger_value
           WHERE trigger_value.tgrelid = relation.oid
             AND NOT trigger_value.tgisinternal
       ),
       NOT EXISTS (
           SELECT 1
           FROM pg_catalog.pg_rewrite AS rewrite_value
           WHERE rewrite_value.ev_class = relation.oid
       ),
       NOT EXISTS (
           SELECT 1
           FROM pg_catalog.pg_publication_rel AS publication_value
           WHERE publication_value.prrelid = relation.oid
       ) AND NOT EXISTS (
           SELECT 1
           FROM pg_catalog.pg_publication AS publication_value
           WHERE publication_value.puballtables
       )
FROM pg_catalog.pg_class AS relation
JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid = relation.relnamespace
JOIN pg_catalog.pg_roles AS relation_owner ON relation_owner.oid = relation.relowner
WHERE namespace.nspname = 'wanwork_im'
  AND relation.relname = ANY($1::text[])
ORDER BY relation.relname`, tableNames)
	if err != nil {
		return err
	}
	defer rows.Close()
	for rows.Next() {
		var name, kind, persistence, replicaIdentity, options string
		var owner, rowSecurity, forceRowSecurity, partition bool
		var noPublicACL, noUserTriggers, noRewriteRules, noPublications bool
		if err := rows.Scan(
			&name,
			&owner,
			&kind,
			&persistence,
			&replicaIdentity,
			&rowSecurity,
			&forceRowSecurity,
			&partition,
			&options,
			&noPublicACL,
			&noUserTriggers,
			&noRewriteRules,
			&noPublications,
		); err != nil {
			return err
		}
		writeDigestFields(
			digest,
			name,
			strconv.FormatBool(owner),
			kind,
			persistence,
			replicaIdentity,
			strconv.FormatBool(rowSecurity),
			strconv.FormatBool(forceRowSecurity),
			strconv.FormatBool(partition),
			options,
			strconv.FormatBool(noPublicACL),
			strconv.FormatBool(noUserTriggers),
			strconv.FormatBool(noRewriteRules),
			strconv.FormatBool(noPublications),
		)
	}
	return rows.Err()
}

func digestColumns(
	ctx context.Context,
	transaction pgx.Tx,
	digest hash.Hash,
	tableNames []string,
) error {
	writeDigestFields(digest, "columns")
	rows, err := transaction.Query(ctx, `
SELECT relation.relname,
       attribute.attnum,
       attribute.attname,
       pg_catalog.format_type(attribute.atttypid, attribute.atttypmod),
       attribute.attnotnull,
       COALESCE(pg_catalog.pg_get_expr(default_value.adbin, default_value.adrelid), ''),
       COALESCE(collation_namespace.nspname, ''),
       COALESCE(collation_value.collname, ''),
       attribute.attidentity::text,
       attribute.attgenerated::text,
       NOT EXISTS (
           SELECT 1
           FROM pg_catalog.aclexplode(attribute.attacl) AS acl
           WHERE acl.grantee = 0
       )
FROM pg_catalog.pg_attribute AS attribute
JOIN pg_catalog.pg_class AS relation ON relation.oid = attribute.attrelid
JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid = relation.relnamespace
LEFT JOIN pg_catalog.pg_attrdef AS default_value
       ON default_value.adrelid = relation.oid AND default_value.adnum = attribute.attnum
LEFT JOIN pg_catalog.pg_collation AS collation_value
       ON collation_value.oid = attribute.attcollation
LEFT JOIN pg_catalog.pg_namespace AS collation_namespace
       ON collation_namespace.oid = collation_value.collnamespace
WHERE namespace.nspname = 'wanwork_im'
  AND relation.relname = ANY($1::text[])
  AND attribute.attnum > 0
  AND NOT attribute.attisdropped
ORDER BY relation.relname, attribute.attnum`, tableNames)
	if err != nil {
		return err
	}
	defer rows.Close()
	for rows.Next() {
		var tableName, name, formatType, defaultSQL, collationNamespace, collationName string
		var identityKind, generatedKind string
		var number int
		var notNull, noPublicACL bool
		if err := rows.Scan(
			&tableName,
			&number,
			&name,
			&formatType,
			&notNull,
			&defaultSQL,
			&collationNamespace,
			&collationName,
			&identityKind,
			&generatedKind,
			&noPublicACL,
		); err != nil {
			return err
		}
		writeDigestFields(
			digest,
			tableName,
			strconv.Itoa(number),
			name,
			formatType,
			strconv.FormatBool(notNull),
			defaultSQL,
			collationNamespace,
			collationName,
			identityKind,
			generatedKind,
			strconv.FormatBool(noPublicACL),
		)
	}
	return rows.Err()
}

func digestConstraints(
	ctx context.Context,
	transaction pgx.Tx,
	digest hash.Hash,
	tableNames []string,
) error {
	writeDigestFields(digest, "constraints")
	rows, err := transaction.Query(ctx, `
SELECT relation.relname,
       constraint_value.conname,
       constraint_value.contype::text,
       pg_catalog.pg_get_constraintdef(constraint_value.oid, false),
       constraint_value.convalidated,
       constraint_value.condeferrable,
       constraint_value.condeferred
FROM pg_catalog.pg_constraint AS constraint_value
JOIN pg_catalog.pg_class AS relation ON relation.oid = constraint_value.conrelid
JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid = relation.relnamespace
WHERE namespace.nspname = 'wanwork_im'
  AND relation.relname = ANY($1::text[])
ORDER BY relation.relname, constraint_value.conname`, tableNames)
	if err != nil {
		return err
	}
	defer rows.Close()
	for rows.Next() {
		var tableName, name, kind, definition string
		var validated, deferrable, deferred bool
		if err := rows.Scan(
			&tableName,
			&name,
			&kind,
			&definition,
			&validated,
			&deferrable,
			&deferred,
		); err != nil {
			return err
		}
		writeDigestFields(
			digest,
			tableName,
			name,
			kind,
			definition,
			strconv.FormatBool(validated),
			strconv.FormatBool(deferrable),
			strconv.FormatBool(deferred),
		)
	}
	return rows.Err()
}

func digestIndexes(
	ctx context.Context,
	transaction pgx.Tx,
	digest hash.Hash,
	tableNames []string,
) error {
	writeDigestFields(digest, "indexes")
	rows, err := transaction.Query(ctx, `
SELECT relation.relname,
       index_relation.relname,
       pg_catalog.pg_get_indexdef(index_value.indexrelid, 0, false),
       index_value.indisprimary,
       index_value.indisunique,
       index_value.indisvalid,
       index_value.indisready,
       index_value.indislive,
       index_value.indnullsnotdistinct,
       index_value.indnkeyatts,
       index_value.indnatts
FROM pg_catalog.pg_index AS index_value
JOIN pg_catalog.pg_class AS relation ON relation.oid = index_value.indrelid
JOIN pg_catalog.pg_class AS index_relation ON index_relation.oid = index_value.indexrelid
JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid = relation.relnamespace
WHERE namespace.nspname = 'wanwork_im'
  AND relation.relname = ANY($1::text[])
ORDER BY relation.relname, index_relation.relname`, tableNames)
	if err != nil {
		return err
	}
	defer rows.Close()
	for rows.Next() {
		var tableName, name, definition string
		var primary, unique, valid, ready, live, nullsNotDistinct bool
		var keyAttributes, attributes int
		if err := rows.Scan(
			&tableName,
			&name,
			&definition,
			&primary,
			&unique,
			&valid,
			&ready,
			&live,
			&nullsNotDistinct,
			&keyAttributes,
			&attributes,
		); err != nil {
			return err
		}
		writeDigestFields(
			digest,
			tableName,
			name,
			definition,
			strconv.FormatBool(primary),
			strconv.FormatBool(unique),
			strconv.FormatBool(valid),
			strconv.FormatBool(ready),
			strconv.FormatBool(live),
			strconv.FormatBool(nullsNotDistinct),
			strconv.Itoa(keyAttributes),
			strconv.Itoa(attributes),
		)
	}
	return rows.Err()
}

func digestPolicies(
	ctx context.Context,
	transaction pgx.Tx,
	digest hash.Hash,
	tableNames []string,
) error {
	writeDigestFields(digest, "policies")
	rows, err := transaction.Query(ctx, `
SELECT relation.relname,
       policy_value.polname,
       policy_value.polcmd::text,
       policy_value.polpermissive,
       policy_value.polroles::text,
       COALESCE(pg_catalog.pg_get_expr(policy_value.polqual, policy_value.polrelid), ''),
       COALESCE(pg_catalog.pg_get_expr(policy_value.polwithcheck, policy_value.polrelid), '')
FROM pg_catalog.pg_policy AS policy_value
JOIN pg_catalog.pg_class AS relation ON relation.oid = policy_value.polrelid
JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid = relation.relnamespace
WHERE namespace.nspname = 'wanwork_im'
  AND relation.relname = ANY($1::text[])
ORDER BY relation.relname, policy_value.polname`, tableNames)
	if err != nil {
		return err
	}
	defer rows.Close()
	for rows.Next() {
		var tableName, name, command, roles, usingSQL, checkSQL string
		var permissive bool
		if err := rows.Scan(
			&tableName,
			&name,
			&command,
			&permissive,
			&roles,
			&usingSQL,
			&checkSQL,
		); err != nil {
			return err
		}
		writeDigestFields(
			digest,
			tableName,
			name,
			command,
			strconv.FormatBool(permissive),
			roles,
			usingSQL,
			checkSQL,
		)
	}
	return rows.Err()
}

func writeDigestFields(digest hash.Hash, values ...string) {
	var length [8]byte
	for _, value := range values {
		binary.BigEndian.PutUint64(length[:], uint64(len(value)))
		_, _ = digest.Write(length[:])
		_, _ = digest.Write([]byte(value))
	}
}
