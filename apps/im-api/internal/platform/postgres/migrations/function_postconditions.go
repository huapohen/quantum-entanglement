package migrations

import (
	"context"
	"crypto/sha256"
	"encoding/hex"

	"github.com/jackc/pgx/v5"
)

const functionDefinitionDigestDomain = "wanwork.im/postgres-function-definition/1\n"

type storedAuthorityFunction struct {
	name               string
	arguments          string
	identityArguments  string
	result             string
	owner              string
	ownerIsCurrentUser bool
	language           string
	kind               string
	volatility         string
	strict             bool
	securityDefiner    bool
	parallel           string
	leakproof          bool
	configuration      string
	safeExecuteACL     bool
	definitionDigest   string
}

func storedAuthorityFunctionManifest() []storedAuthorityFunctionSpec {
	values := storedAuthorityFunctionManifestV5()
	values = append(values, storedAuthorityFunctionSpec{
		name: "write_event",
		arguments: "p_tenant_id text, p_workspace_id text, p_stream_id text, " +
			"p_expected_version bigint, p_event_id text, p_schema_version bigint, " +
			"p_event_type text, p_actor_id text, p_occurred_at timestamp with time zone, " +
			"p_correlation_id text, p_causation_id text, p_idempotency_key text, " +
			"p_traceparent text, p_payload_kind text, p_payload_inline text, " +
			"p_payload_storage text, p_payload_reference_id text, p_payload_byte_length bigint, " +
			"p_payload_digest text, p_append_digest text",
		identityArguments: "p_tenant_id text, p_workspace_id text, p_stream_id text, " +
			"p_expected_version bigint, p_event_id text, p_schema_version bigint, " +
			"p_event_type text, p_actor_id text, p_occurred_at timestamp with time zone, " +
			"p_correlation_id text, p_causation_id text, p_idempotency_key text, " +
			"p_traceparent text, p_payload_kind text, p_payload_inline text, " +
			"p_payload_storage text, p_payload_reference_id text, p_payload_byte_length bigint, " +
			"p_payload_digest text, p_append_digest text",
		result:           "boolean",
		definitionDigest: "75d2ae4387b1e07d1c05ea9631515c1d563912f0d206d061b1c3accda6d04029",
	})
	values = append(values, storedAuthorityFunctionSpec{
		name: "write_projection_checkpoint",
		arguments: "p_tenant_id text, p_workspace_id text, p_projection_id text, " +
			"p_expected_position bigint, p_expected_cursor text, p_expected_last_event_id text, " +
			"p_next_position bigint, p_next_cursor text, p_next_last_event_id text",
		identityArguments: "p_tenant_id text, p_workspace_id text, p_projection_id text, " +
			"p_expected_position bigint, p_expected_cursor text, p_expected_last_event_id text, " +
			"p_next_position bigint, p_next_cursor text, p_next_last_event_id text",
		result:           "boolean",
		definitionDigest: "82b7feec4d80b3cb0335780b0007086ac307e930afc126e5843465c3b31d7faf",
	})
	values = append(values, storedNativeIMInboxFunctionSpecV10())
	return append(values, storedAgentStoreWriteFunctionSpecs()...)
}

func storedAgentStoreWriteFunctionSpecs() []storedAuthorityFunctionSpec {
	return []storedAuthorityFunctionSpec{
		{
			name: "write_agent_definition_revision",
			arguments: "p_tenant_id text, p_definition_id text, p_expected_revision bigint, " +
				"p_next_revision bigint, p_payload text",
			identityArguments: "p_tenant_id text, p_definition_id text, p_expected_revision bigint, " +
				"p_next_revision bigint, p_payload text",
			result:           "boolean",
			definitionDigest: "beb11e8eb5e3bbf6d4c48ddde215a8a0267f7a0a9cb71e3efb0d55ed4a5cbf02",
		},
		{
			name: "write_agent_installation_revision",
			arguments: "p_tenant_id text, p_installation_id text, p_expected_revision bigint, " +
				"p_next_revision bigint, p_payload text",
			identityArguments: "p_tenant_id text, p_installation_id text, p_expected_revision bigint, " +
				"p_next_revision bigint, p_payload text",
			result:           "boolean",
			definitionDigest: "66a7ce8a6a66fa3ae61eb56f0072744b5fe280b76a086cbebea6454564135518",
		},
		{
			name: "write_agent_passport_revision",
			arguments: "p_tenant_id text, p_release_id text, p_expected_revision bigint, " +
				"p_next_revision bigint, p_payload text",
			identityArguments: "p_tenant_id text, p_release_id text, p_expected_revision bigint, " +
				"p_next_revision bigint, p_payload text",
			result:           "boolean",
			definitionDigest: "b1aeb46cee17cdbf553df0795b5161e8a9700f07151d6e5504679247dd67aa0b",
		},
		{
			name: "write_agent_release_revision",
			arguments: "p_tenant_id text, p_release_id text, p_expected_revision bigint, " +
				"p_next_revision bigint, p_payload text",
			identityArguments: "p_tenant_id text, p_release_id text, p_expected_revision bigint, " +
				"p_next_revision bigint, p_payload text",
			result:           "boolean",
			definitionDigest: "232a65dc3d91e02f6dd57ad44c7e2609fbd9bf566de1b3f5d31f4f3580efe287",
		},
		{
			name: "write_agent_provider_effect",
			arguments: "p_tenant_id text, p_workspace_id text, p_installation_id text, " +
				"p_effect_id text, p_effect_kind text, p_provider text, p_provider_realm_id text, " +
				"p_provider_subject_id text, p_operation_key text, p_request_ref text, p_request_sha256 text",
			identityArguments: "p_tenant_id text, p_workspace_id text, p_installation_id text, " +
				"p_effect_id text, p_effect_kind text, p_provider text, p_provider_realm_id text, " +
				"p_provider_subject_id text, p_operation_key text, p_request_ref text, p_request_sha256 text",
			result:           "text",
			definitionDigest: "75483056c74a10adb819a0596d049bd539bbc0783543e01059eddd9273a6973a",
		},
	}
}

func storedNativeIMInboxFunctionSpecV9() storedAuthorityFunctionSpec {
	return storedAuthorityFunctionSpec{
		name: "admit_native_im_inbox",
		arguments: "p_tenant_id text, p_workspace_id text, p_provider text, p_channel_id text, " +
			"p_event_id text, p_event_digest text, p_verification_id text, p_payload_kind text, " +
			"p_payload_inline text, p_payload_storage text, p_payload_reference_id text, " +
			"p_payload_byte_length bigint, p_payload_digest text",
		identityArguments: "p_tenant_id text, p_workspace_id text, p_provider text, p_channel_id text, " +
			"p_event_id text, p_event_digest text, p_verification_id text, p_payload_kind text, " +
			"p_payload_inline text, p_payload_storage text, p_payload_reference_id text, " +
			"p_payload_byte_length bigint, p_payload_digest text",
		result:           "text",
		definitionDigest: "113b90b791916efb61309d70ec18a83a250e56120e092a3b9fa81686e8149df1",
	}
}

func storedNativeIMInboxFunctionSpecV10() storedAuthorityFunctionSpec {
	return storedAuthorityFunctionSpec{
		name: "admit_native_im_inbox",
		arguments: "p_tenant_id text, p_workspace_id text, p_provider text, p_channel_id text, " +
			"p_event_id text, p_event_digest text, p_verification_id text, p_payload_kind text, " +
			"p_payload_inline text, p_payload_storage text, p_payload_reference_id text, " +
			"p_payload_byte_length bigint, p_payload_digest text",
		identityArguments: "p_tenant_id text, p_workspace_id text, p_provider text, p_channel_id text, " +
			"p_event_id text, p_event_digest text, p_verification_id text, p_payload_kind text, " +
			"p_payload_inline text, p_payload_storage text, p_payload_reference_id text, " +
			"p_payload_byte_length bigint, p_payload_digest text",
		result:           "text",
		definitionDigest: "5c171bcf7639bbfa071e5a77d2c3fe12fc98a541b97e2fa096d211a93821e6b1",
	}
}

func storedAuthorityFunctionSpecByName(name string) (storedAuthorityFunctionSpec, bool) {
	for _, spec := range storedAuthorityFunctionManifest() {
		if spec.name == name {
			return spec, true
		}
	}
	return storedAuthorityFunctionSpec{}, false
}

type storedAuthorityFunctionSpec struct {
	name              string
	arguments         string
	identityArguments string
	result            string
	definitionDigest  string
}

func readStoredAuthorityFunctions(
	ctx context.Context,
	transaction pgx.Tx,
	names []string,
) ([]storedAuthorityFunction, error) {
	rows, err := transaction.Query(ctx, `
SELECT procedure.proname,
       pg_catalog.pg_get_function_arguments(procedure.oid),
       pg_catalog.pg_get_function_identity_arguments(procedure.oid),
       pg_catalog.pg_get_function_result(procedure.oid),
       owner.rolname,
       owner.rolname = current_user,
       language.lanname,
       procedure.prokind::text,
       procedure.provolatile::text,
       procedure.proisstrict,
       procedure.prosecdef,
       procedure.proparallel::text,
       procedure.proleakproof,
       COALESCE(pg_catalog.array_to_string(procedure.proconfig, E'\n'), ''),
		       NOT EXISTS (
		           SELECT 1
		           FROM pg_catalog.aclexplode(
               COALESCE(
                   procedure.proacl,
                   pg_catalog.acldefault('f', procedure.proowner)
               )
		           ) AS acl
		           LEFT JOIN pg_catalog.pg_roles AS grantee_role ON grantee_role.oid = acl.grantee
		           WHERE acl.grantee = 0
		              OR acl.privilege_type <> 'EXECUTE'
		              OR acl.is_grantable
		              OR (
		                  acl.grantee <> procedure.proowner
		                  AND (
		                      grantee_role.oid IS NULL
		                      OR grantee_role.rolcanlogin
		                      OR grantee_role.rolsuper
		                      OR grantee_role.rolinherit
		                      OR grantee_role.rolcreatedb
		                      OR grantee_role.rolcreaterole
		                      OR grantee_role.rolreplication
		                      OR grantee_role.rolbypassrls
		                  )
		              )
       ) AND EXISTS (
           SELECT 1
           FROM pg_catalog.aclexplode(
               COALESCE(
                   procedure.proacl,
                   pg_catalog.acldefault('f', procedure.proowner)
               )
           ) AS acl
		   WHERE acl.grantee = procedure.proowner
		     AND acl.privilege_type = 'EXECUTE'
		     AND NOT acl.is_grantable
       ),
       pg_catalog.pg_get_functiondef(procedure.oid)
FROM pg_catalog.pg_proc AS procedure
JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid = procedure.pronamespace
JOIN pg_catalog.pg_roles AS owner ON owner.oid = procedure.proowner
JOIN pg_catalog.pg_language AS language ON language.oid = procedure.prolang
WHERE namespace.nspname = 'wanwork_im'
  AND procedure.proname = ANY($1::text[])
ORDER BY procedure.proname, pg_catalog.pg_get_function_identity_arguments(procedure.oid)`, names)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	functions := make([]storedAuthorityFunction, 0, len(names))
	for rows.Next() {
		var function storedAuthorityFunction
		var definition string
		if err := rows.Scan(
			&function.name,
			&function.arguments,
			&function.identityArguments,
			&function.result,
			&function.owner,
			&function.ownerIsCurrentUser,
			&function.language,
			&function.kind,
			&function.volatility,
			&function.strict,
			&function.securityDefiner,
			&function.parallel,
			&function.leakproof,
			&function.configuration,
			&function.safeExecuteACL,
			&definition,
		); err != nil {
			return nil, err
		}
		function.definitionDigest = digestFunctionDefinition(definition)
		functions = append(functions, function)
	}
	if err := rows.Err(); err != nil {
		return nil, err
	}
	return functions, nil
}

func exactStoredAuthorityFunctions(
	functions []storedAuthorityFunction,
	specs []storedAuthorityFunctionSpec,
) bool {
	return exactStoredAuthorityFunctionsForOwner(functions, specs, "")
}

func exactStoredAuthorityFunctionsForOwner(
	functions []storedAuthorityFunction,
	specs []storedAuthorityFunctionSpec,
	expectedOwner string,
) bool {
	if len(functions) != len(specs) {
		return false
	}
	for index, function := range functions {
		spec := specs[index]
		ownerMatches := function.ownerIsCurrentUser
		if expectedOwner != "" {
			ownerMatches = function.owner == expectedOwner
		}
		if function.name != spec.name || function.arguments != spec.arguments ||
			function.identityArguments != spec.identityArguments || function.result != spec.result ||
			function.definitionDigest != spec.definitionDigest || function.owner == "" ||
			!ownerMatches || function.language != "plpgsql" ||
			function.kind != "f" || function.volatility != "v" || !function.strict ||
			!function.securityDefiner || function.parallel != "u" || function.leakproof ||
			function.configuration != "search_path=pg_catalog" || !function.safeExecuteACL {
			return false
		}
	}
	return true
}

func digestFunctionDefinition(definition string) string {
	digest := sha256.Sum256([]byte(functionDefinitionDigestDomain + definition))
	return hex.EncodeToString(digest[:])
}

func validateFunctionOnlyWrites(ctx context.Context, transaction pgx.Tx) error {
	return validateFunctionOnlyWritesForOwner(ctx, transaction, "")
}

func validateFunctionOnlyWritesForOwner(
	ctx context.Context,
	transaction pgx.Tx,
	expectedOwner string,
) error {
	specs := storedAuthorityFunctionManifestV5()
	names := make([]string, len(specs))
	for index, spec := range specs {
		names[index] = spec.name
	}
	functions, err := readStoredAuthorityFunctions(ctx, transaction, names)
	if err != nil || !exactStoredAuthorityFunctionsForOwner(functions, specs, expectedOwner) {
		return ErrMigrationSchema
	}
	return nil
}

func storedAuthorityFunctionManifestV5() []storedAuthorityFunctionSpec {
	return []storedAuthorityFunctionSpec{
		{
			name: "write_conversation_access_revision",
			arguments: "p_tenant_id text, p_conversation_id text, p_actor_id text, " +
				"p_expected_revision bigint, p_next_revision bigint, p_can_read boolean, " +
				"p_can_send_message boolean, p_can_manage_members boolean, " +
				"p_can_manage_conversation boolean, p_can_invoke_agent boolean, " +
				"p_can_publish_artifact_reference boolean",
			identityArguments: "p_tenant_id text, p_conversation_id text, p_actor_id text, " +
				"p_expected_revision bigint, p_next_revision bigint, p_can_read boolean, " +
				"p_can_send_message boolean, p_can_manage_members boolean, " +
				"p_can_manage_conversation boolean, p_can_invoke_agent boolean, " +
				"p_can_publish_artifact_reference boolean",
			result:           "boolean",
			definitionDigest: "5d01ba7ed5a3d23d4a39429fadc1b13b74b0b19b5f1b6481f7089a77cded5624",
		},
		{
			name: "write_conversation_membership_revision",
			arguments: "p_tenant_id text, p_conversation_id text, p_actor_id text, " +
				"p_expected_revision bigint, p_next_revision bigint, p_role text, p_status text",
			identityArguments: "p_tenant_id text, p_conversation_id text, p_actor_id text, " +
				"p_expected_revision bigint, p_next_revision bigint, p_role text, p_status text",
			result:           "boolean",
			definitionDigest: "e767afdb78fc0c503ef712da50f6a74758be03b209e2f131b79db54344e16ee3",
		},
		{
			name: "write_conversation_revision",
			arguments: "p_tenant_id text, p_conversation_id text, p_expected_revision bigint, " +
				"p_next_revision bigint, p_workspace_id text, p_conversation_type text, p_status text",
			identityArguments: "p_tenant_id text, p_conversation_id text, p_expected_revision bigint, " +
				"p_next_revision bigint, p_workspace_id text, p_conversation_type text, p_status text",
			result:           "boolean",
			definitionDigest: "d7261cd6d418136abbc372b486106526d21f1ecd22fd49f6d372c9fda6592702",
		},
		{
			name: "write_provider_conversation_binding_revision",
			arguments: "p_tenant_id text, p_provider text, p_realm_id text, " +
				"p_provider_conversation_id text, p_expected_revision bigint, " +
				"p_next_revision bigint, p_conversation_id text, p_status text",
			identityArguments: "p_tenant_id text, p_provider text, p_realm_id text, " +
				"p_provider_conversation_id text, p_expected_revision bigint, " +
				"p_next_revision bigint, p_conversation_id text, p_status text",
			result:           "boolean",
			definitionDigest: "a3ed1538ff9f61c2289fb9068f97cc4010fabbc4e2f86088f2012f44ef5c4c9c",
		},
		{
			name: "write_tenant_command_receipt",
			arguments: "p_tenant_id text, p_command_kind text, p_idempotency_key text, " +
				"p_request_sha256 text, p_result_sha256 text",
			identityArguments: "p_tenant_id text, p_command_kind text, p_idempotency_key text, " +
				"p_request_sha256 text, p_result_sha256 text",
			result:           "timestamp with time zone",
			definitionDigest: "9d2854dadf7f5bb3bbce2b2385e4a69b3ce2138ffb6389e554b6328f22f22d62",
		},
	}
}
