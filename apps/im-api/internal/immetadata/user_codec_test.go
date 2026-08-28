package immetadata

import (
	"errors"
	"strings"
	"testing"

	"github.com/huapohen/quantum-entanglement/apps/im-api/internal/im"
)

func TestUserProjectionCanonicalGoldenBytesAndRoundTrip(t *testing.T) {
	t.Parallel()

	for _, test := range []struct {
		name       string
		projection UserProjection
		golden     string
	}{
		{
			name:       "human",
			projection: mustUserProjection(t, im.SubjectHuman, "usr_alice", "", ""),
			golden:     `{"platformActorId":"usr_alice","schemaVersion":1,"subjectType":"human"}`,
		},
		{
			name:       "agent",
			projection: mustUserProjection(t, im.SubjectAgent, "agt_finance", "agd_finance", "1.2.3-rc.1"),
			golden:     `{"agentDefinitionId":"agd_finance","agentVersion":"1.2.3-rc.1","platformActorId":"agt_finance","schemaVersion":1,"subjectType":"agent"}`,
		},
	} {
		test := test
		t.Run(test.name, func(t *testing.T) {
			t.Parallel()
			encoded, err := EncodeUserProjection(test.projection)
			if err != nil || encoded != test.golden {
				t.Fatalf("EncodeUserProjection() = (%q, %v), want (%q, nil)", encoded, err, test.golden)
			}
			decoded, err := DecodeUserProjection(encoded)
			if err != nil || decoded != test.projection || decoded.IsZero() {
				t.Fatalf("DecodeUserProjection() = (%#v, %v), want %#v", decoded, err, test.projection)
			}
			reencoded, err := EncodeUserProjection(decoded)
			if err != nil || reencoded != encoded {
				t.Fatalf("canonical re-encode = (%q, %v), want %q", reencoded, err, encoded)
			}
		})
	}
}

func TestUserProjectionRejectsSubjectPrefixAndAgentFieldDrift(t *testing.T) {
	t.Parallel()

	humanID := mustActorID(t, "usr_alice")
	agentID := mustActorID(t, "agt_finance")
	agentDefinitionID := mustAgentDefinitionID(t, "agd_finance")
	agentVersion := mustAgentVersion(t, "1.0.0")
	for _, test := range []struct {
		name              string
		subjectType       im.SubjectType
		actorID           im.ActorID
		agentDefinitionID im.AgentDefinitionID
		agentVersion      im.AgentVersion
	}{
		{name: "human prefix cannot claim agent", subjectType: im.SubjectAgent, actorID: humanID, agentDefinitionID: agentDefinitionID, agentVersion: agentVersion},
		{name: "agent prefix cannot claim human", subjectType: im.SubjectHuman, actorID: agentID},
		{name: "human forbids agent definition", subjectType: im.SubjectHuman, actorID: humanID, agentDefinitionID: agentDefinitionID},
		{name: "human forbids agent version", subjectType: im.SubjectHuman, actorID: humanID, agentVersion: agentVersion},
		{name: "agent requires definition", subjectType: im.SubjectAgent, actorID: agentID, agentVersion: agentVersion},
		{name: "agent requires version", subjectType: im.SubjectAgent, actorID: agentID, agentDefinitionID: agentDefinitionID},
		{name: "system is not provider chat user", subjectType: im.SubjectSystem, actorID: mustActorID(t, "sys_projection")},
		{name: "service is not provider chat user", subjectType: im.SubjectService, actorID: mustActorID(t, "svc_adapter")},
	} {
		test := test
		t.Run(test.name, func(t *testing.T) {
			t.Parallel()
			projection, err := NewUserProjection(
				test.subjectType,
				test.actorID,
				test.agentDefinitionID,
				test.agentVersion,
			)
			if !errors.Is(err, ErrInvalidProviderMetadata) || !projection.IsZero() {
				t.Fatalf("NewUserProjection() = (%#v, %v), want zero and ErrInvalidProviderMetadata", projection, err)
			}
		})
	}

	if encoded, err := EncodeUserProjection(UserProjection{}); !errors.Is(err, ErrInvalidProviderMetadata) || encoded != "" {
		t.Fatalf("EncodeUserProjection(zero) = (%q, %v), want empty and ErrInvalidProviderMetadata", encoded, err)
	}
}

func TestUserProjectionStrictDecoderRejectsStructuralAndCanonicalDrift(t *testing.T) {
	t.Parallel()

	for _, raw := range []string{
		"",
		" ",
		"null",
		"[]",
		`"user"`,
		"1",
		"true",
		"\ufeff" + `{"platformActorId":"usr_alice","schemaVersion":1,"subjectType":"human"}`,
		`{"platformActorId":"usr_alice","schemaVersion":1,"subjectType":"human"} `,
		` {"platformActorId":"usr_alice","schemaVersion":1,"subjectType":"human"}`,
		`{"platformActorId":"usr_alice","schemaVersion":1,"subjectType":"human"}x`,
		`{"platformActorId":"usr_alice","schemaVersion":1,"subjectType":"human"}{}`,
		`{"platformActorId":"usr_alice","schemaVersion":1,"subjectType":"human"`,
		`{"platformActorId":"usr_alice","schemaVersion":1,"schemaVersion":1,"subjectType":"human"}`,
		`{"platformActorId":"usr_alice","schemaVersion":1,"\u0073chemaVersion":1,"subjectType":"human"}`,
		`{"platformActorId":"usr_alice","schemaVersion":1,"subjectType":"human","tenantId":"ten_acme"}`,
		`{"platform_actor_id":"usr_alice","schemaVersion":1,"subjectType":"human"}`,
		`{"platformActorId":"usr_alice","SchemaVersion":1,"subjectType":"human"}`,
		`{"platformActorId":"usr_alice","schemaVersion":"1","subjectType":"human"}`,
		`{"platformActorId":"usr_alice","schemaVersion":1.0,"subjectType":"human"}`,
		`{"platformActorId":"usr_alice","schemaVersion":1e0,"subjectType":"human"}`,
		`{"platformActorId":"usr_alice","schemaVersion":0,"subjectType":"human"}`,
		`{"platformActorId":"usr_alice","schemaVersion":2,"subjectType":"human"}`,
		`{"platformActorId":null,"schemaVersion":1,"subjectType":"human"}`,
		`{"platformActorId":true,"schemaVersion":1,"subjectType":"human"}`,
		`{"platformActorId":{"id":"usr_alice"},"schemaVersion":1,"subjectType":"human"}`,
		`{"platformActorId":"usr_alice","schemaVersion":1,"subjectType":null}`,
		`{"platformActorId":"usr_alice","schemaVersion":1,"subjectType":"owner"}`,
		`{"platformActorId":"usr_alice","schemaVersion":1,"subjectType":"human","agentDefinitionId":""}`,
		`{"platformActorId":"usr_alice","schemaVersion":1,"subjectType":"human","token":"secret-canary"}`,
		`{"schemaVersion":1,"subjectType":"human","platformActorId":"usr_alice"}`,
		`{"platformActorId":"\u0075sr_alice","schemaVersion":1,"subjectType":"human"}`,
		`{"platformActorId":"usr_alice","schemaVersion":1,"subjectType":"hum\u0061n"}`,
		`{"agentDefinitionId":"agd_finance","agentVersion":"1.0.0","platformActorId":"agt_finance","schemaVersion":1,"subjectType":"human"}`,
		`{"agentDefinitionId":"agd_finance","platformActorId":"agt_finance","schemaVersion":1,"subjectType":"agent"}`,
		`{"agentVersion":"1.0.0","platformActorId":"agt_finance","schemaVersion":1,"subjectType":"agent"}`,
		`{"agentDefinitionId":"agd_finance","agentVersion":"v1.0.0","platformActorId":"agt_finance","schemaVersion":1,"subjectType":"agent"}`,
		`{"agentDefinitionId":"agd_finance","agentVersion":"1.0.0","platformActorId":"usr_alice","schemaVersion":1,"subjectType":"agent"}`,
		`{"agentDefinitionId":"agd_finance","agentVersion":"1.0.0","platformActorId":"agt_ｆｉｎａｎｃｅ","schemaVersion":1,"subjectType":"agent"}`,
		`{"agentDefinitionId":"agd_finance","agentVersion":"1.0.0","platformActorId":"agt_finance\u000aadmin","schemaVersion":1,"subjectType":"agent"}`,
	} {
		raw := raw
		t.Run(testName(raw), func(t *testing.T) {
			t.Parallel()
			projection, err := DecodeUserProjection(raw)
			if !errors.Is(err, ErrInvalidProviderMetadata) || !projection.IsZero() {
				t.Fatalf("DecodeUserProjection(%q) = (%#v, %v), want zero and ErrInvalidProviderMetadata", raw, projection, err)
			}
			if strings.Contains(err.Error(), "secret-canary") {
				t.Fatalf("error leaked rejected payload: %v", err)
			}
		})
	}
}

func TestUserProjectionRejectsEveryNonCanonicalKeyPermutation(t *testing.T) {
	t.Parallel()

	for _, fields := range [][]string{
		{
			`"platformActorId":"usr_alice"`,
			`"schemaVersion":1`,
			`"subjectType":"human"`,
		},
		{
			`"agentDefinitionId":"agd_finance"`,
			`"agentVersion":"1.0.0"`,
			`"platformActorId":"agt_finance"`,
			`"schemaVersion":1`,
			`"subjectType":"agent"`,
		},
	} {
		canonical := "{" + strings.Join(fields, ",") + "}"
		accepted := 0
		for _, permutation := range permutations(fields) {
			raw := "{" + strings.Join(permutation, ",") + "}"
			projection, err := DecodeUserProjection(raw)
			if raw == canonical {
				if err != nil || projection.IsZero() {
					t.Fatalf("canonical permutation rejected: %q: %v", raw, err)
				}
				accepted++
				continue
			}
			if !errors.Is(err, ErrInvalidProviderMetadata) || !projection.IsZero() {
				t.Fatalf("non-canonical permutation accepted: %q", raw)
			}
		}
		if accepted != 1 {
			t.Fatalf("accepted permutations = %d, want exactly 1", accepted)
		}
	}
}

func TestUserProjectionRejectsOversizeBeforeParsing(t *testing.T) {
	t.Parallel()

	raw := `{"platformActorId":"usr_` + strings.Repeat("a", maxProviderMetadataBytes) + `","schemaVersion":1,"subjectType":"human"}`
	projection, err := DecodeUserProjection(raw)
	if !errors.Is(err, ErrProviderMetadataTooLarge) || !projection.IsZero() {
		t.Fatalf("DecodeUserProjection(oversize) = (%#v, %v), want zero and ErrProviderMetadataTooLarge", projection, err)
	}
}

func mustUserProjection(
	t *testing.T,
	subjectType im.SubjectType,
	actorIDValue string,
	agentDefinitionValue string,
	agentVersionValue string,
) UserProjection {
	t.Helper()
	actorID := mustActorID(t, actorIDValue)
	var agentDefinitionID im.AgentDefinitionID
	if agentDefinitionValue != "" {
		agentDefinitionID = mustAgentDefinitionID(t, agentDefinitionValue)
	}
	var agentVersion im.AgentVersion
	if agentVersionValue != "" {
		agentVersion = mustAgentVersion(t, agentVersionValue)
	}
	projection, err := NewUserProjection(subjectType, actorID, agentDefinitionID, agentVersion)
	if err != nil {
		t.Fatalf("NewUserProjection() error = %v", err)
	}
	return projection
}

func mustActorID(t *testing.T, value string) im.ActorID {
	t.Helper()
	identifier, err := im.ParseActorID(value)
	if err != nil {
		t.Fatalf("im.ParseActorID(%q) error = %v", value, err)
	}
	return identifier
}

func mustAgentDefinitionID(t *testing.T, value string) im.AgentDefinitionID {
	t.Helper()
	identifier, err := im.ParseAgentDefinitionID(value)
	if err != nil {
		t.Fatalf("im.ParseAgentDefinitionID(%q) error = %v", value, err)
	}
	return identifier
}

func mustAgentVersion(t *testing.T, value string) im.AgentVersion {
	t.Helper()
	version, err := im.ParseAgentVersion(value)
	if err != nil {
		t.Fatalf("im.ParseAgentVersion(%q) error = %v", value, err)
	}
	return version
}

func permutations(values []string) [][]string {
	working := append([]string(nil), values...)
	result := make([][]string, 0)
	var visit func(int)
	visit = func(index int) {
		if index == len(working) {
			result = append(result, append([]string(nil), working...))
			return
		}
		for current := index; current < len(working); current++ {
			working[index], working[current] = working[current], working[index]
			visit(index + 1)
			working[index], working[current] = working[current], working[index]
		}
	}
	visit(0)
	return result
}

func testName(raw string) string {
	name := raw
	if len(name) > 48 {
		name = name[:48]
	}
	name = strings.NewReplacer("/", "_", "\\", "_", "\n", "_", "\t", "_").Replace(name)
	return name
}
