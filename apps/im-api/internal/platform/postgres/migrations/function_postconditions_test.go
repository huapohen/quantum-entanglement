package migrations

import "testing"

func TestExactStoredAuthorityFunctionsRejectsEveryManifestDrift(t *testing.T) {
	definition := "CREATE FUNCTION wanwork_im.write_test() RETURNS boolean ...\n"
	digest := digestFunctionDefinition(definition)
	specs := []storedAuthorityFunctionSpec{{
		name:              "write_test",
		arguments:         "p_tenant_id text",
		identityArguments: "text",
		result:            "boolean",
		definitionDigest:  digest,
	}}
	valid := storedAuthorityFunction{
		name:               "write_test",
		arguments:          "p_tenant_id text",
		identityArguments:  "text",
		result:             "boolean",
		owner:              "migration_owner",
		ownerIsCurrentUser: true,
		language:           "plpgsql",
		kind:               "f",
		volatility:         "v",
		strict:             true,
		securityDefiner:    true,
		parallel:           "u",
		configuration:      "search_path=pg_catalog",
		ownerOnlyExecute:   true,
		definitionDigest:   digest,
	}
	if !exactStoredAuthorityFunctions([]storedAuthorityFunction{valid}, specs) {
		t.Fatal("exact function manifest rejected")
	}
	if exactStoredAuthorityFunctions(nil, specs) ||
		exactStoredAuthorityFunctions([]storedAuthorityFunction{valid, valid}, specs) {
		t.Fatal("function manifest must reject missing and extra overloads")
	}

	mutations := map[string]func(*storedAuthorityFunction){
		"name":                func(value *storedAuthorityFunction) { value.name = "other" },
		"arguments":           func(value *storedAuthorityFunction) { value.arguments = "tenant text" },
		"identity arguments":  func(value *storedAuthorityFunction) { value.identityArguments = "uuid" },
		"result":              func(value *storedAuthorityFunction) { value.result = "text" },
		"missing owner":       func(value *storedAuthorityFunction) { value.owner = "" },
		"wrong owner":         func(value *storedAuthorityFunction) { value.ownerIsCurrentUser = false },
		"language":            func(value *storedAuthorityFunction) { value.language = "sql" },
		"kind":                func(value *storedAuthorityFunction) { value.kind = "p" },
		"volatility":          func(value *storedAuthorityFunction) { value.volatility = "s" },
		"not strict":          func(value *storedAuthorityFunction) { value.strict = false },
		"security invoker":    func(value *storedAuthorityFunction) { value.securityDefiner = false },
		"parallel safe":       func(value *storedAuthorityFunction) { value.parallel = "s" },
		"leakproof":           func(value *storedAuthorityFunction) { value.leakproof = true },
		"configuration":       func(value *storedAuthorityFunction) { value.configuration = "search_path=public" },
		"public execute":      func(value *storedAuthorityFunction) { value.ownerOnlyExecute = false },
		"definition checksum": func(value *storedAuthorityFunction) { value.definitionDigest = digestFunctionDefinition("other") },
	}
	for name, mutate := range mutations {
		t.Run(name, func(t *testing.T) {
			changed := valid
			mutate(&changed)
			if exactStoredAuthorityFunctions([]storedAuthorityFunction{changed}, specs) {
				t.Fatal("function manifest drift accepted")
			}
		})
	}
}

func TestFunctionDefinitionDigestIsDomainSeparatedAndDeterministic(t *testing.T) {
	definition := "CREATE FUNCTION wanwork_im.write_test() RETURNS boolean ...\n"
	first := digestFunctionDefinition(definition)
	if len(first) != 64 || first != digestFunctionDefinition(definition) ||
		first == digestFunctionDefinition(definition+" ") {
		t.Fatalf("unexpected function definition digest %q", first)
	}
}
