package migrations

import (
	"encoding/json"
	"errors"
	"strings"
	"testing"
)

func TestCurrentAuthorityCutoverSpecificationFreezesDedicatedCellGraph(t *testing.T) {
	manifest := specificationTestManifest()
	specification, err := CurrentAuthorityCutoverSpecification(
		manifest,
		"provisioner_login",
		"iac_bootstrap_role",
	)
	if err != nil {
		t.Fatalf("CurrentAuthorityCutoverSpecification: %v", err)
	}
	if specification.Format != AuthorityCutoverSpecificationFormat ||
		specification.PostgreSQLMajor != AuthorityAccessPostgreSQLMajor ||
		specification.Topology != AuthorityCutoverTopology ||
		!canonicalSHA256Digest.MatchString(specification.ManagedAuthoritySpecificationDigest) {
		t.Fatalf("cutover identity = %#v", specification)
	}
	if specification.DatabaseOwner != (AuthorityDatabaseOwnerSpecification{
		ConnectionLimit:   -1,
		CreateRole:        true,
		Database:          manifest.DatabaseName,
		PreflightRequired: true,
		Role:              manifest.DatabaseOwnerRole,
	}) {
		t.Fatalf("database owner = %#v", specification.DatabaseOwner)
	}
	if specification.Provisioner != (AuthorityRoleSpecification{
		Name:            "provisioner_login",
		Login:           true,
		ConnectionLimit: -1,
	}) {
		t.Fatalf("provisioner = %#v", specification.Provisioner)
	}
	if specification.Membership != (AuthorityMembershipSpecification{
		GrantedRole: manifest.DatabaseOwnerRole,
		MemberRole:  "provisioner_login",
		GrantorRole: "iac_bootstrap_role",
		SetOption:   true,
	}) {
		t.Fatalf("membership = %#v", specification.Membership)
	}
	if specification.ProvisionerConnect != (AuthorityPrivilegeSpecification{
		Scope:       AuthorityPrivilegeDatabase,
		Object:      manifest.DatabaseName,
		GranteeRole: "provisioner_login",
		GrantorRole: manifest.DatabaseOwnerRole,
		Privilege:   "CONNECT",
	}) {
		t.Fatalf("CONNECT privilege = %#v", specification.ProvisionerConnect)
	}
	if specification.UnexpectedScopedMemberships || specification.UnexpectedProvisionerPrivileges {
		t.Fatalf("negative constraints are not fail closed: %#v", specification)
	}

	managed, err := CurrentAuthorityAccessSpecification(manifest)
	if err != nil {
		t.Fatalf("CurrentAuthorityAccessSpecification: %v", err)
	}
	managedDigest, err := DigestAuthorityAccessSpecification(managed)
	if err != nil {
		t.Fatalf("DigestAuthorityAccessSpecification: %v", err)
	}
	if specification.ManagedAuthoritySpecificationDigest != managedDigest {
		t.Fatalf("managed digest = %q, want %q", specification.ManagedAuthoritySpecificationDigest, managedDigest)
	}

	payload, err := json.Marshal(specification)
	if err != nil {
		t.Fatalf("marshal specification: %v", err)
	}
	for _, forbidden := range []string{"password", "dsn", "connectionstring", "privatekey", "token"} {
		if strings.Contains(strings.ToLower(string(payload)), forbidden) {
			t.Fatalf("cutover specification contains forbidden field %q: %s", forbidden, payload)
		}
	}
}

func TestAuthorityCutoverSpecificationIsDeterministicAndIndependentlyDigested(t *testing.T) {
	manifest := specificationTestManifest()
	first, err := CurrentAuthorityCutoverSpecification(manifest, "provisioner_login", "iac_bootstrap_role")
	if err != nil {
		t.Fatalf("first specification: %v", err)
	}
	second, err := CurrentAuthorityCutoverSpecification(manifest, "provisioner_login", "iac_bootstrap_role")
	if err != nil {
		t.Fatalf("second specification: %v", err)
	}
	firstDigest, err := DigestAuthorityCutoverSpecification(first)
	if err != nil {
		t.Fatalf("first digest: %v", err)
	}
	secondDigest, err := DigestAuthorityCutoverSpecification(second)
	if err != nil {
		t.Fatalf("second digest: %v", err)
	}
	if firstDigest != secondDigest || !canonicalSHA256Digest.MatchString(firstDigest) {
		t.Fatalf("non-deterministic digests first=%q second=%q", firstDigest, secondDigest)
	}
	managed, err := CurrentAuthorityAccessSpecification(manifest)
	if err != nil {
		t.Fatalf("managed specification: %v", err)
	}
	managedDigest, err := DigestAuthorityAccessSpecification(managed)
	if err != nil {
		t.Fatalf("managed digest: %v", err)
	}
	if firstDigest == managedDigest {
		t.Fatal("cutover and managed specifications shared a digest domain")
	}

	const want = "sha256:f918cb59e780de5a804867174cba789697c12e75eadaf688d1d48bf9630d1905"
	if firstDigest != want {
		t.Fatalf("golden digest = %q, want %q", firstDigest, want)
	}
}

func TestCurrentAuthorityCutoverSpecificationRejectsIdentityOverlap(t *testing.T) {
	manifest := specificationTestManifest()
	tests := map[string]struct {
		provisioner string
		grantor     string
	}{
		"provisioner is database owner": {manifest.DatabaseOwnerRole, "iac_bootstrap_role"},
		"provisioner is managed login":  {manifest.MigrationLoginRoles[0], "iac_bootstrap_role"},
		"grantor is database owner":     {"provisioner_login", manifest.DatabaseOwnerRole},
		"grantor is managed role":       {"provisioner_login", manifest.RuntimeRole},
		"grantor is provisioner":        {"provisioner_login", "provisioner_login"},
		"invalid provisioner":           {"Provisioner", "iac_bootstrap_role"},
		"invalid grantor":               {"provisioner_login", "iac/bootstrap"},
	}
	for name, test := range tests {
		t.Run(name, func(t *testing.T) {
			_, err := CurrentAuthorityCutoverSpecification(manifest, test.provisioner, test.grantor)
			if !errors.Is(err, ErrAuthorityCutoverSpecification) {
				t.Fatalf("error = %v, want %v", err, ErrAuthorityCutoverSpecification)
			}
		})
	}
}

func TestDigestAuthorityCutoverSpecificationRejectsDrift(t *testing.T) {
	mutations := map[string]func(*AuthorityCutoverSpecification){
		"shared topology": func(value *AuthorityCutoverSpecification) {
			value.Topology = "shared-postgres-cluster"
		},
		"provisioner inherit": func(value *AuthorityCutoverSpecification) {
			value.Provisioner.Inherit = true
		},
		"provisioner create role": func(value *AuthorityCutoverSpecification) {
			value.Provisioner.CreateRole = true
		},
		"membership admin": func(value *AuthorityCutoverSpecification) {
			value.Membership.AdminOption = true
		},
		"membership inherit": func(value *AuthorityCutoverSpecification) {
			value.Membership.InheritOption = true
		},
		"membership cannot set": func(value *AuthorityCutoverSpecification) {
			value.Membership.SetOption = false
		},
		"connect grantable": func(value *AuthorityCutoverSpecification) {
			value.ProvisionerConnect.Grantable = true
		},
		"connect wrong grantor": func(value *AuthorityCutoverSpecification) {
			value.ProvisionerConnect.GrantorRole = "other_iac_role"
		},
		"unexpected membership": func(value *AuthorityCutoverSpecification) {
			value.UnexpectedScopedMemberships = true
		},
		"unexpected privilege": func(value *AuthorityCutoverSpecification) {
			value.UnexpectedProvisionerPrivileges = true
		},
	}
	for name, mutate := range mutations {
		t.Run(name, func(t *testing.T) {
			specification, err := CurrentAuthorityCutoverSpecification(
				specificationTestManifest(),
				"provisioner_login",
				"iac_bootstrap_role",
			)
			if err != nil {
				t.Fatalf("CurrentAuthorityCutoverSpecification: %v", err)
			}
			mutate(&specification)
			if _, err := DigestAuthorityCutoverSpecification(specification); !errors.Is(
				err,
				ErrAuthorityCutoverSpecification,
			) {
				t.Fatalf("digest error = %v, want %v", err, ErrAuthorityCutoverSpecification)
			}
		})
	}
}

func TestAuthorityCutoverDigestBindsExactMembershipGrantor(t *testing.T) {
	specification, err := CurrentAuthorityCutoverSpecification(
		specificationTestManifest(),
		"provisioner_login",
		"iac_bootstrap_role",
	)
	if err != nil {
		t.Fatalf("CurrentAuthorityCutoverSpecification: %v", err)
	}
	first, err := DigestAuthorityCutoverSpecification(specification)
	if err != nil {
		t.Fatalf("DigestAuthorityCutoverSpecification: %v", err)
	}
	specification.Membership.GrantorRole = "other_iac_role"
	second, err := DigestAuthorityCutoverSpecification(specification)
	if err != nil {
		t.Fatalf("DigestAuthorityCutoverSpecification changed grantor: %v", err)
	}
	if first == second {
		t.Fatal("exact membership grantor did not change cutover specification digest")
	}
}
