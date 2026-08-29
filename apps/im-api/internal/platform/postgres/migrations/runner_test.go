package migrations

import (
	"errors"
	"testing"
)

func TestValidateAppliedAcceptsOnlyExactContiguousPrefix(t *testing.T) {
	catalog, err := Catalog()
	if err != nil {
		t.Fatalf("load catalog: %v", err)
	}
	exact := []AppliedMigration{{
		Version: catalog[0].Version, Name: catalog[0].Name, Checksum: catalog[0].Checksum,
	}}
	if err := validateApplied(catalog, nil); err != nil {
		t.Fatalf("empty prefix must be valid: %v", err)
	}
	if err := validateApplied(catalog, exact); err != nil {
		t.Fatalf("exact prefix must be valid: %v", err)
	}

	for _, fixture := range []struct {
		name    string
		applied []AppliedMigration
		want    error
	}{
		{name: "version gap", applied: []AppliedMigration{{Version: 0}}, want: ErrLedgerDrift},
		{name: "name drift", applied: []AppliedMigration{{Version: 1, Name: "other", Checksum: catalog[0].Checksum}}, want: ErrLedgerDrift},
		{name: "checksum drift", applied: []AppliedMigration{{Version: 1, Name: catalog[0].Name, Checksum: "0"}}, want: ErrLedgerDrift},
		{name: "future version", applied: []AppliedMigration{{Version: 10}}, want: ErrFutureSchema},
		{
			name: "extra rows",
			applied: append(
				exact,
				AppliedMigration{
					Version: 2, Name: catalog[1].Name, Checksum: catalog[1].Checksum,
				},
				AppliedMigration{
					Version: 3, Name: catalog[2].Name, Checksum: catalog[2].Checksum,
				},
				AppliedMigration{
					Version: 4, Name: catalog[3].Name, Checksum: catalog[3].Checksum,
				},
				AppliedMigration{
					Version: 5, Name: catalog[4].Name, Checksum: catalog[4].Checksum,
				},
				AppliedMigration{Version: 6, Name: catalog[5].Name, Checksum: catalog[5].Checksum},
				AppliedMigration{Version: 10},
			),
			want: ErrFutureSchema,
		},
	} {
		t.Run(fixture.name, func(t *testing.T) {
			if err := validateApplied(catalog, fixture.applied); !errors.Is(err, fixture.want) {
				t.Fatalf("validateApplied() error = %v, want %v", err, fixture.want)
			}
		})
	}
}

func TestExactLedgerColumnsRejectsWeakenedShape(t *testing.T) {
	defaultSQL := "clock_timestamp()"
	pgCatalog := "pg_catalog"
	cCollation := "C"
	valid := []ledgerColumn{
		{name: "version", formatType: "bigint", notNull: true},
		{
			name: "name", formatType: "text", notNull: true,
			collationNamespace: &pgCatalog, collationName: &cCollation,
		},
		{
			name: "checksum", formatType: "text", notNull: true,
			collationNamespace: &pgCatalog, collationName: &cCollation,
		},
		{name: "applied_at", formatType: "timestamp with time zone", notNull: true, defaultSQL: &defaultSQL},
	}
	if !exactLedgerColumns(valid) {
		t.Fatal("expected exact ledger shape to pass")
	}
	for index := range valid {
		weakened := append([]ledgerColumn(nil), valid...)
		weakened[index].notNull = false
		if exactLedgerColumns(weakened) {
			t.Fatalf("nullable column %d must be rejected", index)
		}
	}
	wrongDefault := "now()"
	weakened := append([]ledgerColumn(nil), valid...)
	weakened[3].defaultSQL = &wrongDefault
	if exactLedgerColumns(weakened) {
		t.Fatal("weakened default must be rejected")
	}
	defaultCollation := "default"
	weakened = append([]ledgerColumn(nil), valid...)
	weakened[1].collationName = &defaultCollation
	if exactLedgerColumns(weakened) {
		t.Fatal("non-C text collation must be rejected")
	}
	weakened = append([]ledgerColumn(nil), valid...)
	weakened[2].identityKind = "d"
	if exactLedgerColumns(weakened) {
		t.Fatal("identity column must be rejected")
	}
	weakened = append([]ledgerColumn(nil), valid...)
	weakened[3].generatedKind = "s"
	if exactLedgerColumns(weakened) {
		t.Fatal("generated column must be rejected")
	}
}

func TestExactLedgerRelationRejectsOwnerACLAndStorageDrift(t *testing.T) {
	valid := ledgerRelation{
		currentUser:        "wanwork_migrator",
		schemaOwner:        "wanwork_migrator",
		relationOwner:      "wanwork_migrator",
		relationKind:       "r",
		persistence:        "p",
		ownerOnlySchemaACL: true,
		ownerOnlyTableACL:  true,
		noColumnACL:        true,
		noUserTriggers:     true,
		noRewriteRules:     true,
		noPolicies:         true,
		noPublications:     true,
		onlyPrimaryIndex:   true,
	}
	if !exactLedgerRelation(valid) {
		t.Fatal("expected exact ledger relation to pass")
	}
	fixtures := []struct {
		name   string
		mutate func(*ledgerRelation)
	}{
		{name: "schema owner", mutate: func(value *ledgerRelation) { value.schemaOwner = "other" }},
		{name: "table owner", mutate: func(value *ledgerRelation) { value.relationOwner = "other" }},
		{name: "view", mutate: func(value *ledgerRelation) { value.relationKind = "v" }},
		{name: "temporary", mutate: func(value *ledgerRelation) { value.persistence = "t" }},
		{name: "row security", mutate: func(value *ledgerRelation) { value.rowSecurity = true }},
		{name: "forced row security", mutate: func(value *ledgerRelation) { value.forceRowSecurity = true }},
		{name: "schema grant", mutate: func(value *ledgerRelation) { value.ownerOnlySchemaACL = false }},
		{name: "table grant", mutate: func(value *ledgerRelation) { value.ownerOnlyTableACL = false }},
		{name: "column grant", mutate: func(value *ledgerRelation) { value.noColumnACL = false }},
		{name: "user trigger", mutate: func(value *ledgerRelation) { value.noUserTriggers = false }},
		{name: "rewrite rule", mutate: func(value *ledgerRelation) { value.noRewriteRules = false }},
		{name: "policy", mutate: func(value *ledgerRelation) { value.noPolicies = false }},
		{name: "publication", mutate: func(value *ledgerRelation) { value.noPublications = false }},
		{name: "extra index", mutate: func(value *ledgerRelation) { value.onlyPrimaryIndex = false }},
	}
	for _, fixture := range fixtures {
		t.Run(fixture.name, func(t *testing.T) {
			weakened := valid
			fixture.mutate(&weakened)
			if exactLedgerRelation(weakened) {
				t.Fatal("weakened ledger relation must be rejected")
			}
		})
	}
}

func TestExactLedgerConstraintsRejectsSameNameWeakDefinitions(t *testing.T) {
	valid := []ledgerConstraint{
		{
			name: "schema_migrations_applied_at_not_null", kind: "n",
			definition: "NOT NULL applied_at", validated: true,
		},
		{
			name: "schema_migrations_checksum_check", kind: "c",
			definition: `CHECK ((checksum ~ '^[0-9a-f]{64}$'::text))`, validated: true,
		},
		{
			name: "schema_migrations_checksum_not_null", kind: "n",
			definition: "NOT NULL checksum", validated: true,
		},
		{
			name: "schema_migrations_name_check", kind: "c",
			definition: `CHECK ((name ~ '^[a-z][a-z0-9]*(_[a-z0-9]+)*$'::text))`, validated: true,
		},
		{
			name: "schema_migrations_name_not_null", kind: "n",
			definition: "NOT NULL name", validated: true,
		},
		{
			name: "schema_migrations_pkey", kind: "p",
			definition: "PRIMARY KEY (version)", validated: true,
		},
		{
			name: "schema_migrations_version_check", kind: "c",
			definition: "CHECK ((version > 0))", validated: true,
		},
		{
			name: "schema_migrations_version_not_null", kind: "n",
			definition: "NOT NULL version", validated: true,
		},
	}
	if !exactLedgerConstraints(valid) {
		t.Fatal("expected exact ledger constraints to pass")
	}
	for _, fixture := range []struct {
		name   string
		mutate func([]ledgerConstraint)
	}{
		{
			name: "weak checksum regex",
			mutate: func(value []ledgerConstraint) {
				value[1].definition = `CHECK ((checksum <> ''::text))`
			},
		},
		{
			name: "wrong primary key",
			mutate: func(value []ledgerConstraint) {
				value[5].definition = "PRIMARY KEY (name)"
			},
		},
		{
			name:   "not validated",
			mutate: func(value []ledgerConstraint) { value[6].validated = false },
		},
		{
			name:   "deferrable",
			mutate: func(value []ledgerConstraint) { value[5].deferrable = true },
		},
	} {
		t.Run(fixture.name, func(t *testing.T) {
			weakened := append([]ledgerConstraint(nil), valid...)
			fixture.mutate(weakened)
			if exactLedgerConstraints(weakened) {
				t.Fatal("weakened ledger constraints must be rejected")
			}
		})
	}
}

func TestApplyRejectsNilConnection(t *testing.T) {
	if _, err := Apply(t.Context(), nil); !errors.Is(err, ErrInvalidConnection) {
		t.Fatalf("Apply(nil connection) error = %v", err)
	}
}
