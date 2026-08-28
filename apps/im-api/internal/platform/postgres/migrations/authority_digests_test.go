package migrations

import (
	"errors"
	"testing"
)

func TestAuthorityDigestsAreCanonicalAndSemanticallyBound(t *testing.T) {
	manifest := specificationTestManifest()
	manifest.MigrationLoginRoles = []string{"migration_login_b", "migration_login_a"}
	manifest.RuntimeLoginRoles = []string{"runtime_login_b", "runtime_login_a"}
	firstManifestDigest, err := DigestAuthorityAccessManifest(manifest)
	if err != nil {
		t.Fatalf("first manifest digest: %v", err)
	}
	reordered := manifest
	reordered.MigrationLoginRoles = []string{"migration_login_a", "migration_login_b"}
	reordered.RuntimeLoginRoles = []string{"runtime_login_a", "runtime_login_b"}
	secondManifestDigest, err := DigestAuthorityAccessManifest(reordered)
	if err != nil {
		t.Fatalf("second manifest digest: %v", err)
	}
	if firstManifestDigest != secondManifestDigest ||
		!canonicalSHA256Digest.MatchString(firstManifestDigest) {
		t.Fatalf("semantic set ordering changed manifest digest: %q != %q", firstManifestDigest, secondManifestDigest)
	}
	changed := reordered
	changed.RuntimeLoginRoles = append(changed.RuntimeLoginRoles, "runtime_login_c")
	changedDigest, err := DigestAuthorityAccessManifest(changed)
	if err != nil {
		t.Fatalf("changed manifest digest: %v", err)
	}
	if changedDigest == firstManifestDigest {
		t.Fatal("semantic manifest change did not change digest")
	}

	firstCatalogDigest, err := CurrentMigrationCatalogDigest()
	if err != nil {
		t.Fatalf("first catalog digest: %v", err)
	}
	secondCatalogDigest, err := CurrentMigrationCatalogDigest()
	if err != nil {
		t.Fatalf("second catalog digest: %v", err)
	}
	if firstCatalogDigest != secondCatalogDigest ||
		!canonicalSHA256Digest.MatchString(firstCatalogDigest) {
		t.Fatalf("catalog digest is not deterministic: %q != %q", firstCatalogDigest, secondCatalogDigest)
	}

	specification, err := CurrentAuthorityAccessSpecification(reordered)
	if err != nil {
		t.Fatalf("specification: %v", err)
	}
	firstSpecificationDigest, err := DigestAuthorityAccessSpecification(specification)
	if err != nil {
		t.Fatalf("first specification digest: %v", err)
	}
	secondSpecificationDigest, err := DigestAuthorityAccessSpecification(specification)
	if err != nil {
		t.Fatalf("second specification digest: %v", err)
	}
	if firstSpecificationDigest != secondSpecificationDigest ||
		!canonicalSHA256Digest.MatchString(firstSpecificationDigest) {
		t.Fatalf("specification digest is not deterministic: %q != %q", firstSpecificationDigest, secondSpecificationDigest)
	}
}

func TestAuthorityDigestsRejectInvalidValues(t *testing.T) {
	if _, err := DigestAuthorityAccessManifest(AuthorityAccessManifest{}); !errors.Is(
		err,
		ErrInvalidAuthorityAccessManifest,
	) {
		t.Fatalf("invalid manifest error = %v", err)
	}
	specification, err := CurrentAuthorityAccessSpecification(specificationTestManifest())
	if err != nil {
		t.Fatalf("specification: %v", err)
	}
	specification.MigrationCatalogDigest = "sha256:invalid"
	if _, err := DigestAuthorityAccessSpecification(specification); !errors.Is(
		err,
		ErrAuthorityAccessSpecification,
	) {
		t.Fatalf("invalid specification error = %v", err)
	}
}
