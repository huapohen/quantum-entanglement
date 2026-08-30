package immetadata

import (
	"errors"
	"strings"
	"testing"
)

func TestProviderMetadataRejectsAuthorizationSecretContentAndEvidenceFields(t *testing.T) {
	t.Parallel()

	userGolden := `{"platformActorId":"usr_alice","schemaVersion":1,"subjectType":"human"}`
	conversationGolden := `{"conversationType":"group","platformConversationId":"cnv_product","schemaVersion":1}`
	for _, field := range []string{
		"tenantId",
		"workspaceId",
		"acl",
		"role",
		"memberIds",
		"permission",
		"capability",
		"policy",
		"approval",
		"delegation",
		"mandate",
		"scope",
		"sessionId",
		"runId",
		"attemptId",
		"workloadId",
		"credentialLease",
		"token",
		"apiKey",
		"password",
		"cookie",
		"authorization",
		"refreshToken",
		"credential",
		"secretRef",
		"messageBody",
		"prompt",
		"email",
		"phone",
		"memory",
		"callback",
		"webhook",
		"endpoint",
		"fileUrl",
		"taskId",
		"actionId",
		"receiptId",
		"artifactId",
		"acceptanceId",
		"evidence",
		"checkpoint",
		"metadata",
		"extensions",
		"extra",
	} {
		field := field
		t.Run(field, func(t *testing.T) {
			t.Parallel()
			canary := "secret-canary-" + field
			injection := `,"` + field + `":"` + canary + `"}`
			userRaw := strings.TrimSuffix(userGolden, "}") + injection
			userProjection, userErr := DecodeUserProjection(userRaw)
			if !errors.Is(userErr, ErrInvalidProviderMetadata) || !userProjection.IsZero() {
				t.Fatalf("user forbidden field %q = (%#v, %v)", field, userProjection, userErr)
			}
			conversationRaw := strings.TrimSuffix(conversationGolden, "}") + injection
			conversationProjection, conversationErr := DecodeConversationProjection(conversationRaw)
			if !errors.Is(conversationErr, ErrInvalidProviderMetadata) ||
				!conversationProjection.IsZero() {
				t.Fatalf("conversation forbidden field %q = (%#v, %v)", field, conversationProjection, conversationErr)
			}
			if strings.Contains(userErr.Error(), canary) || strings.Contains(conversationErr.Error(), canary) {
				t.Fatalf("forbidden field canary leaked through error for %q", field)
			}
		})
	}
}
