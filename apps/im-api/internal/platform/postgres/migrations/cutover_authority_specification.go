package migrations

import "errors"

const (
	AuthorityCutoverSpecificationFormat = "wanwork.im.postgres-cutover-authority-spec/1"
	AuthorityCutoverTopology            = "dedicated-postgres-cluster-cell"
)

var ErrAuthorityCutoverSpecification = errors.New("invalid PostgreSQL cutover authority specification")

// AuthorityCutoverSpecification freezes the temporary authority graph used before the managed
// migration/runtime graph exists. It is intentionally independent from AuthorityAccessSpecification:
// the transient provisioner must never become a managed runtime role by accident.
type AuthorityCutoverSpecification struct {
	Format                              string                              `json:"format"`
	PostgreSQLMajor                     int                                 `json:"postgresqlMajor"`
	Topology                            string                              `json:"topology"`
	ManagedAuthoritySpecificationDigest string                              `json:"managedAuthoritySpecificationDigest"`
	DatabaseOwner                       AuthorityDatabaseOwnerSpecification `json:"databaseOwner"`
	Provisioner                         AuthorityRoleSpecification          `json:"provisioner"`
	Membership                          AuthorityMembershipSpecification    `json:"membership"`
	ProvisionerConnect                  AuthorityPrivilegeSpecification     `json:"provisionerConnect"`
	UnexpectedScopedMemberships         bool                                `json:"unexpectedScopedMemberships"`
	UnexpectedProvisionerPrivileges     bool                                `json:"unexpectedProvisionerPrivileges"`
}

// CurrentAuthorityCutoverSpecification resolves the only supported dedicated-cell cutover graph.
// provisionerGrantorRole is an exact, stable IaC/bootstrap identity: PostgreSQL records it in both
// the SET-only membership and the direct CONNECT ACL, so it cannot be inferred or ignored.
func CurrentAuthorityCutoverSpecification(
	manifest AuthorityAccessManifest,
	provisionerLoginRole string,
	provisionerGrantorRole string,
) (AuthorityCutoverSpecification, error) {
	if manifest.Validate() != nil ||
		!validCutoverExternalRoles(manifest, provisionerLoginRole, provisionerGrantorRole) {
		return AuthorityCutoverSpecification{}, ErrAuthorityCutoverSpecification
	}
	managed, err := CurrentAuthorityAccessSpecification(manifest)
	if err != nil {
		return AuthorityCutoverSpecification{}, ErrAuthorityCutoverSpecification
	}
	managedDigest, err := DigestAuthorityAccessSpecification(managed)
	if err != nil {
		return AuthorityCutoverSpecification{}, ErrAuthorityCutoverSpecification
	}
	specification := AuthorityCutoverSpecification{
		Format:                              AuthorityCutoverSpecificationFormat,
		PostgreSQLMajor:                     AuthorityAccessPostgreSQLMajor,
		Topology:                            AuthorityCutoverTopology,
		ManagedAuthoritySpecificationDigest: managedDigest,
		DatabaseOwner:                       authorityDatabaseOwnerSpecification(manifest),
		Provisioner: AuthorityRoleSpecification{
			Name:            provisionerLoginRole,
			Login:           true,
			ConnectionLimit: -1,
		},
		Membership: AuthorityMembershipSpecification{
			GrantedRole: manifest.DatabaseOwnerRole,
			MemberRole:  provisionerLoginRole,
			GrantorRole: provisionerGrantorRole,
			SetOption:   true,
		},
		ProvisionerConnect: AuthorityPrivilegeSpecification{
			Scope:       AuthorityPrivilegeDatabase,
			Object:      manifest.DatabaseName,
			GranteeRole: provisionerLoginRole,
			GrantorRole: manifest.DatabaseOwnerRole,
			Privilege:   "CONNECT",
		},
	}
	if !validAuthorityCutoverSpecification(specification) {
		return AuthorityCutoverSpecification{}, ErrAuthorityCutoverSpecification
	}
	return specification, nil
}

func validAuthorityCutoverSpecification(specification AuthorityCutoverSpecification) bool {
	if specification.Format != AuthorityCutoverSpecificationFormat ||
		specification.PostgreSQLMajor != AuthorityAccessPostgreSQLMajor ||
		specification.Topology != AuthorityCutoverTopology ||
		!canonicalSHA256Digest.MatchString(specification.ManagedAuthoritySpecificationDigest) ||
		specification.UnexpectedScopedMemberships || specification.UnexpectedProvisionerPrivileges {
		return false
	}
	manifest := AuthorityAccessManifest{
		DatabaseName:      specification.DatabaseOwner.Database,
		DatabaseOwnerRole: specification.DatabaseOwner.Role,
	}
	if specification.DatabaseOwner != authorityDatabaseOwnerSpecification(manifest) ||
		!canonicalAccessRoleName.MatchString(specification.Provisioner.Name) ||
		!canonicalAccessRoleName.MatchString(specification.Membership.GrantorRole) ||
		specification.Provisioner != (AuthorityRoleSpecification{
			Name:            specification.Provisioner.Name,
			Login:           true,
			ConnectionLimit: -1,
		}) ||
		specification.Membership != (AuthorityMembershipSpecification{
			GrantedRole: specification.DatabaseOwner.Role,
			MemberRole:  specification.Provisioner.Name,
			GrantorRole: specification.Membership.GrantorRole,
			SetOption:   true,
		}) ||
		specification.ProvisionerConnect != (AuthorityPrivilegeSpecification{
			Scope:       AuthorityPrivilegeDatabase,
			Object:      specification.DatabaseOwner.Database,
			GranteeRole: specification.Provisioner.Name,
			GrantorRole: specification.DatabaseOwner.Role,
			Privilege:   "CONNECT",
		}) {
		return false
	}
	return uniqueCanonicalAccessRoles([]string{
		specification.DatabaseOwner.Role,
		specification.Provisioner.Name,
		specification.Membership.GrantorRole,
	})
}

func validCutoverExternalRoles(
	manifest AuthorityAccessManifest,
	provisionerLoginRole string,
	provisionerGrantorRole string,
) bool {
	roles := []string{
		manifest.DatabaseOwnerRole,
		manifest.OwnerRole,
		manifest.MigratorRole,
		manifest.RuntimeRole,
		provisionerLoginRole,
		provisionerGrantorRole,
	}
	roles = append(roles, manifest.MigrationLoginRoles...)
	roles = append(roles, manifest.RuntimeLoginRoles...)
	return uniqueCanonicalAccessRoles(roles)
}
