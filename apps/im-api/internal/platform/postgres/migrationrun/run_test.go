package migrationrun

import (
	"errors"
	"strings"
	"testing"
	"time"

	"github.com/huapohen/quantum-entanglement/apps/im-api/internal/platform/postgres/migrations"
)

func TestRunRejectsInvalidConfigWithoutConnecting(t *testing.T) {
	manifest := migrations.DefaultAuthorityAccessManifest()
	manifest.MigrationLoginRoles = []string{"wanwork_deploy_a"}
	manifest.RuntimeLoginRoles = []string{"wanwork_app_a"}
	for name, mutate := range map[string]func(*Config){
		"nil context": func(*Config) {},
		"invalid manifest": func(value *Config) {
			value.Manifest.MigrationLoginRoles = nil
		},
		"wrong login": func(value *Config) {
			value.ConnectionString = "postgresql://wrong@127.0.0.1:1/wanwork_im?sslmode=disable"
		},
		"zero timeout": func(value *Config) { value.ConnectTimeout = 0 },
	} {
		t.Run(name, func(t *testing.T) {
			input := Config{
				ConnectionString:       "postgresql://wanwork_deploy_a@127.0.0.1:1/wanwork_im?sslmode=disable",
				Manifest:               manifest,
				ConnectTimeout:         time.Millisecond,
				AllowInsecureLocalhost: true,
			}
			mutate(&input)
			ctx := t.Context()
			if name == "nil context" {
				ctx = nil
			}
			_, err := Run(ctx, input)
			if !errors.Is(err, ErrInvalidConfig) {
				t.Fatalf("invalid migration run error = %v, want %v", err, ErrInvalidConfig)
			}
		})
	}
}

func TestRunMapsConnectionFailureWithoutCredentialLeak(t *testing.T) {
	const credentialCanary = "migration-secret-canary"
	manifest := migrations.DefaultAuthorityAccessManifest()
	manifest.MigrationLoginRoles = []string{"wanwork_deploy_a"}
	manifest.RuntimeLoginRoles = []string{"wanwork_app_a"}
	_, err := Run(t.Context(), Config{
		ConnectionString: "postgresql://wanwork_deploy_a:" + credentialCanary +
			"@127.0.0.1:1/wanwork_im?sslmode=disable",
		Manifest:               manifest,
		ConnectTimeout:         10 * time.Millisecond,
		AllowInsecureLocalhost: true,
	})
	if !errors.Is(err, ErrUnavailable) || strings.Contains(err.Error(), credentialCanary) {
		t.Fatalf("connection failure error = %v, want fixed unavailable sentinel", err)
	}
}
