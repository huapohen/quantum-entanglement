package immetadata

import (
	"errors"
	"testing"
)

func TestProviderMetadataRejectsUnicodeNormalizationControlsAndInvalidUTF8(t *testing.T) {
	t.Parallel()

	for _, actorID := range []string{
		"usr_e\u0301",
		"usr_é",
		"usr_ａｌｉｃｅ",
		"usr_аlice",
		"usr_alice\u202e",
		"usr_alice\u200b",
		"usr_alice\u0085",
	} {
		raw := `{"platformActorId":"` + actorID + `","schemaVersion":1,"subjectType":"human"}`
		projection, err := DecodeUserProjection(raw)
		if !errors.Is(err, ErrInvalidProviderMetadata) || !projection.IsZero() {
			t.Fatalf("DecodeUserProjection(%q) = (%#v, %v), want zero and ErrInvalidProviderMetadata", raw, projection, err)
		}
	}

	for _, raw := range []string{
		`{"platformActorId":"usr_alice\u0000","schemaVersion":1,"subjectType":"human"}`,
		`{"platformActorId":"usr_alice\r","schemaVersion":1,"subjectType":"human"}`,
		`{"platformActorId":"usr_alice\n","schemaVersion":1,"subjectType":"human"}`,
		`{"platformActorId":"usr_alice\t","schemaVersion":1,"subjectType":"human"}`,
		`{"platformActorId":"usr_alice\u007f","schemaVersion":1,"subjectType":"human"}`,
		`{"platformActorId":"usr_alice\ud800","schemaVersion":1,"subjectType":"human"}`,
		string([]byte{0xff, 0xfe, 0xfd}),
	} {
		projection, err := DecodeUserProjection(raw)
		if !errors.Is(err, ErrInvalidProviderMetadata) || !projection.IsZero() {
			t.Fatalf("DecodeUserProjection(%q) = (%#v, %v), want zero and ErrInvalidProviderMetadata", raw, projection, err)
		}
	}
}
