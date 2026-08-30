package localdemo

import (
	"context"
	"encoding/json"
	"strings"
	"testing"
	"time"

	"github.com/huapohen/quantum-entanglement/apps/im-api/internal/agentstore"
)

func TestAgentStoreProjectionExposesSafeVersionProvenance(t *testing.T) {
	t.Parallel()
	service, err := New()
	if err != nil {
		t.Fatal(err)
	}
	page, err := service.ListAgents(context.Background(), LocalBearerToken)
	if err != nil {
		t.Fatal(err)
	}
	if len(page.Agents) != len(service.agentCatalog) {
		t.Fatalf("projection count = %d, catalog count = %d", len(page.Agents), len(service.agentCatalog))
	}

	for _, view := range page.Agents {
		var record agentCatalogRecord
		found := false
		for _, candidate := range service.agentCatalog {
			if candidate.passport.Definition().ID().String() == view.DefinitionID {
				record, found = candidate, true
				break
			}
		}
		if !found {
			t.Fatalf("missing catalog record for %s", view.DefinitionID)
		}
		release := record.passport.Release()
		definition := record.passport.Definition()

		for name, value := range map[string]string{
			"artifact": view.ArtifactDigest,
			"manifest": view.ManifestDigest,
			"persona":  view.PersonaDigest,
		} {
			parsed, parseErr := agentstore.ParseSHA256Digest(value)
			if parseErr != nil || parsed.IsZero() {
				t.Fatalf("%s digest = %q, parse error = %v", name, value, parseErr)
			}
		}
		if view.ArtifactDigest != release.ArtifactDigest().Hex() ||
			view.ManifestDigest != release.ManifestDigest().Hex() ||
			view.PersonaDigest != release.PersonaDigest().Hex() {
			t.Fatalf("digest projection drift for %s: %#v", view.DefinitionID, view)
		}

		provenance := view.VersionProvenance
		if provenance.PublisherID != definition.PublisherID().String() ||
			provenance.DefinitionRevision != definition.Revision() ||
			provenance.ReleaseRevision != release.Revision() ||
			provenance.PassportRevision != record.passport.Revision() ||
			provenance.DigestAlgorithm != "sha256" {
			t.Fatalf("version provenance drift for %s: %#v", view.DefinitionID, provenance)
		}
		publishedAt, parseErr := time.Parse(time.RFC3339Nano, provenance.PublishedAt)
		if parseErr != nil || publishedAt.IsZero() || publishedAt.Location() != time.UTC {
			t.Fatalf("publishedAt = %q, parse error = %v", provenance.PublishedAt, parseErr)
		}

		encoded, marshalErr := json.Marshal(view)
		if marshalErr != nil {
			t.Fatal(marshalErr)
		}
		wire := strings.ToLower(string(encoded))
		for _, forbidden := range []string{"api_key", "apikey", "password", "secret", "credential", "access_token"} {
			if strings.Contains(wire, forbidden) {
				t.Fatalf("agent projection contains forbidden credential marker %q: %s", forbidden, encoded)
			}
		}
	}
}
