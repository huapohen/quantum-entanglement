package migrations

import (
	"errors"
	"testing"
)

func TestAuthorityAccessManifestRequiresDistinctCanonicalRoles(t *testing.T) {
	valid := DefaultAuthorityAccessManifest()
	valid.MigrationLoginRoles = []string{"wanwork_im_deploy_login"}
	valid.RuntimeLoginRoles = []string{"wanwork_im_app_login"}
	if !validAuthorityAccessManifest(valid) {
		t.Fatal("valid authority access manifest rejected")
	}
	if err := valid.Validate(); err != nil {
		t.Fatalf("valid authority access manifest error = %v", err)
	}
	for name, mutate := range map[string]func(*AuthorityAccessManifest){
		"empty database":         func(value *AuthorityAccessManifest) { value.DatabaseName = "" },
		"uppercase database":     func(value *AuthorityAccessManifest) { value.DatabaseName = "WanWork" },
		"empty owner":            func(value *AuthorityAccessManifest) { value.OwnerRole = "" },
		"empty migration logins": func(value *AuthorityAccessManifest) { value.MigrationLoginRoles = nil },
		"empty runtime logins":   func(value *AuthorityAccessManifest) { value.RuntimeLoginRoles = nil },
		"uppercase role":         func(value *AuthorityAccessManifest) { value.RuntimeRole = "Runtime" },
		"duplicate core":         func(value *AuthorityAccessManifest) { value.RuntimeRole = value.OwnerRole },
		"duplicate login": func(value *AuthorityAccessManifest) {
			value.RuntimeLoginRoles = append(value.RuntimeLoginRoles, value.RuntimeLoginRoles[0])
		},
		"login equals core":    func(value *AuthorityAccessManifest) { value.RuntimeLoginRoles[0] = value.RuntimeRole },
		"login in both groups": func(value *AuthorityAccessManifest) { value.RuntimeLoginRoles[0] = value.MigrationLoginRoles[0] },
	} {
		t.Run(name, func(t *testing.T) {
			changed := valid
			changed.MigrationLoginRoles = append([]string(nil), valid.MigrationLoginRoles...)
			changed.RuntimeLoginRoles = append([]string(nil), valid.RuntimeLoginRoles...)
			mutate(&changed)
			if validAuthorityAccessManifest(changed) {
				t.Fatal("invalid authority access manifest accepted")
			}
			if err := changed.Validate(); !errors.Is(err, ErrInvalidAuthorityAccessManifest) {
				t.Fatalf("invalid manifest error = %v", err)
			}
		})
	}
}

func TestValidateAuthorityAccessRejectsInvalidInputs(t *testing.T) {
	if err := ValidateAuthorityAccess(t.Context(), nil, DefaultAuthorityAccessManifest()); !errors.Is(
		err,
		ErrInvalidAuthorityAccessManifest,
	) {
		t.Fatalf("nil connection error = %v", err)
	}
	if err := ValidateAuthorityAccess(nil, nil, AuthorityAccessManifest{}); !errors.Is(
		err,
		ErrInvalidAuthorityAccessManifest,
	) {
		t.Fatalf("nil context error = %v", err)
	}
	if err := ValidateRuntimeAuthorityAccess(
		t.Context(),
		nil,
		DefaultAuthorityAccessManifest(),
	); !errors.Is(err, ErrInvalidAuthorityAccessManifest) {
		t.Fatalf("nil runtime connection error = %v", err)
	}
}

func TestAuthorityAccessTableManifestIsSortedAndComplete(t *testing.T) {
	names := authorityAccessTableNames()
	if len(names) != 25 {
		t.Fatalf("authority table count = %d, want 25", len(names))
	}
	for index := 1; index < len(names); index++ {
		if names[index-1] >= names[index] {
			t.Fatalf("authority table manifest is not strictly sorted: %v", names)
		}
	}
}
