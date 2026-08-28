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
	ownerOnlyExecute   bool
	definitionDigest   string
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
           WHERE acl.grantee <> procedure.proowner
              OR acl.privilege_type <> 'EXECUTE'
              OR NOT acl.is_grantable
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
             AND acl.is_grantable
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
			&function.ownerOnlyExecute,
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
	if len(functions) != len(specs) {
		return false
	}
	for index, function := range functions {
		spec := specs[index]
		if function.name != spec.name || function.arguments != spec.arguments ||
			function.identityArguments != spec.identityArguments || function.result != spec.result ||
			function.definitionDigest != spec.definitionDigest || function.owner == "" ||
			!function.ownerIsCurrentUser || function.language != "plpgsql" ||
			function.kind != "f" || function.volatility != "v" || !function.strict ||
			!function.securityDefiner || function.parallel != "u" || function.leakproof ||
			function.configuration != "search_path=pg_catalog" || !function.ownerOnlyExecute {
			return false
		}
	}
	return true
}

func digestFunctionDefinition(definition string) string {
	digest := sha256.Sum256([]byte(functionDefinitionDigestDomain + definition))
	return hex.EncodeToString(digest[:])
}
