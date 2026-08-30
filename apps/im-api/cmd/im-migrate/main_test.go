package main

import (
	"bytes"
	"errors"
	"testing"
)

func TestRunRejectsPartialCommandConfigBeforeMigration(t *testing.T) {
	for name, values := range map[string]map[string]string{
		"empty": {},
		"url only": {
			migrationURLVariable: "postgresql://wanwork_deploy@127.0.0.1:1/wanwork_im?sslmode=disable",
		},
		"manifest only": {
			authorityManifestVariable: validCommandManifest(),
		},
		"invalid local flag": {
			migrationURLVariable:           "postgresql://wanwork_deploy@127.0.0.1:1/wanwork_im?sslmode=disable",
			authorityManifestVariable:      validCommandManifest(),
			allowInsecureLocalTestVariable: "yes",
		},
	} {
		t.Run(name, func(t *testing.T) {
			var output bytes.Buffer
			if err := run(t.Context(), mapCommandLookup(values), &output); !errors.Is(
				err,
				ErrInvalidCommandConfig,
			) {
				t.Fatalf("invalid command error = %v, want %v", err, ErrInvalidCommandConfig)
			}
			if output.Len() != 0 {
				t.Fatalf("invalid command wrote output %q", output.String())
			}
		})
	}
}

func mapCommandLookup(values map[string]string) lookupEnv {
	return func(name string) (string, bool) {
		value, ok := values[name]
		return value, ok
	}
}

func validCommandManifest() string {
	return `{
        "databaseName":"wanwork_im",
        "databaseOwnerRole":"wanwork_im_provisioner",
        "ownerRole":"wanwork_im_owner",
        "migratorRole":"wanwork_im_migrator",
        "runtimeRole":"wanwork_im_runtime",
        "migrationLoginRoles":["wanwork_deploy"],
        "runtimeLoginRoles":["wanwork_app"]
    }`
}
