package immetadata

import (
	"errors"
	"strings"
	"testing"

	"github.com/huapohen/quantum-entanglement/apps/im-api/internal/im"
)

func TestConversationProjectionCanonicalGoldenBytesAndRoundTrip(t *testing.T) {
	t.Parallel()

	for _, test := range []struct {
		name       string
		projection ConversationProjection
		golden     string
	}{
		{
			name:       "ordinary group",
			projection: mustConversationProjection(t, im.ConversationGroup, "cnv_product", "", "", ""),
			golden:     `{"conversationType":"group","platformConversationId":"cnv_product","schemaVersion":1}`,
		},
		{
			name:       "agent thread",
			projection: mustConversationProjection(t, im.ConversationAgentThread, "cnv_thread", "cnv_parent", "msg_root", "inv_finance"),
			golden:     `{"agentInvocationId":"inv_finance","conversationType":"agent_thread","parentConversationId":"cnv_parent","platformConversationId":"cnv_thread","rootMessageId":"msg_root","schemaVersion":1}`,
		},
	} {
		test := test
		t.Run(test.name, func(t *testing.T) {
			t.Parallel()
			encoded, err := EncodeConversationProjection(test.projection)
			if err != nil || encoded != test.golden {
				t.Fatalf("EncodeConversationProjection() = (%q, %v), want (%q, nil)", encoded, err, test.golden)
			}
			decoded, err := DecodeConversationProjection(encoded)
			if err != nil || decoded != test.projection || decoded.IsZero() {
				t.Fatalf("DecodeConversationProjection() = (%#v, %v), want %#v", decoded, err, test.projection)
			}
			reencoded, err := EncodeConversationProjection(decoded)
			if err != nil || reencoded != encoded {
				t.Fatalf("canonical re-encode = (%q, %v), want %q", reencoded, err, encoded)
			}
		})
	}
}

func TestConversationProjectionRejectsUnsupportedOrIncompleteTopology(t *testing.T) {
	t.Parallel()

	conversationID := mustConversationID(t, "cnv_thread")
	parentID := mustConversationID(t, "cnv_parent")
	rootMessageID := mustMessageID(t, "msg_root")
	invocationID := mustInvocationID(t, "inv_finance")
	for _, test := range []struct {
		name             string
		conversationType im.ConversationType
		conversationID   im.ConversationID
		parentID         im.ConversationID
		rootMessageID    im.MessageID
		invocationID     im.InvocationID
	}{
		{name: "direct has no group ext info", conversationType: im.ConversationDirect, conversationID: conversationID},
		{name: "missing conversation", conversationType: im.ConversationGroup},
		{name: "group forbids parent", conversationType: im.ConversationGroup, conversationID: conversationID, parentID: parentID},
		{name: "group forbids root message", conversationType: im.ConversationGroup, conversationID: conversationID, rootMessageID: rootMessageID},
		{name: "group forbids invocation", conversationType: im.ConversationGroup, conversationID: conversationID, invocationID: invocationID},
		{name: "thread requires parent", conversationType: im.ConversationAgentThread, conversationID: conversationID, rootMessageID: rootMessageID, invocationID: invocationID},
		{name: "thread requires root message", conversationType: im.ConversationAgentThread, conversationID: conversationID, parentID: parentID, invocationID: invocationID},
		{name: "thread requires invocation", conversationType: im.ConversationAgentThread, conversationID: conversationID, parentID: parentID, rootMessageID: rootMessageID},
		{name: "thread cannot parent itself", conversationType: im.ConversationAgentThread, conversationID: conversationID, parentID: conversationID, rootMessageID: rootMessageID, invocationID: invocationID},
		{name: "unknown type", conversationType: im.ConversationType("channel"), conversationID: conversationID},
	} {
		test := test
		t.Run(test.name, func(t *testing.T) {
			t.Parallel()
			projection, err := NewConversationProjection(
				test.conversationType,
				test.conversationID,
				test.parentID,
				test.rootMessageID,
				test.invocationID,
			)
			if !errors.Is(err, ErrInvalidProviderMetadata) || !projection.IsZero() {
				t.Fatalf("NewConversationProjection() = (%#v, %v), want zero and ErrInvalidProviderMetadata", projection, err)
			}
		})
	}

	if encoded, err := EncodeConversationProjection(ConversationProjection{}); !errors.Is(err, ErrInvalidProviderMetadata) || encoded != "" {
		t.Fatalf("EncodeConversationProjection(zero) = (%q, %v), want empty and ErrInvalidProviderMetadata", encoded, err)
	}
}

func TestConversationProjectionStrictDecoderRejectsStructuralAndSemanticDrift(t *testing.T) {
	t.Parallel()

	for _, raw := range []string{
		"",
		"null",
		"[]",
		`{"conversationType":"group","platformConversationId":"cnv_product","schemaVersion":1} `,
		`{"conversationType":"group","platformConversationId":"cnv_product","schemaVersion":1}x`,
		`{"conversationType":"group","platformConversationId":"cnv_product","schemaVersion":1}{}`,
		`{"conversationType":"group","platformConversationId":"cnv_product","schemaVersion":1`,
		`{"conversationType":"group","conversationType":"group","platformConversationId":"cnv_product","schemaVersion":1}`,
		`{"conversationType":"group","platformConversationId":"cnv_product","schemaVersion":1,"\u0073chemaVersion":1}`,
		`{"conversationType":"group","platformConversationId":"cnv_product","schemaVersion":1,"tenantId":"ten_acme"}`,
		`{"conversationType":"group","platformConversationId":"cnv_product","schemaVersion":1,"acl":["usr_alice"]}`,
		`{"conversationType":"group","platformConversationId":"cnv_product","schemaVersion":1,"token":"secret-canary"}`,
		`{"conversation_type":"group","platformConversationId":"cnv_product","schemaVersion":1}`,
		`{"conversationType":"group","platform_conversation_id":"cnv_product","schemaVersion":1}`,
		`{"conversationType":"group","platformConversationId":"cnv_product","schemaVersion":"1"}`,
		`{"conversationType":"group","platformConversationId":"cnv_product","schemaVersion":1.0}`,
		`{"conversationType":"group","platformConversationId":"cnv_product","schemaVersion":1e0}`,
		`{"conversationType":"group","platformConversationId":"cnv_product","schemaVersion":2}`,
		`{"conversationType":null,"platformConversationId":"cnv_product","schemaVersion":1}`,
		`{"conversationType":"group","platformConversationId":null,"schemaVersion":1}`,
		`{"conversationType":"direct","platformConversationId":"cnv_direct","schemaVersion":1}`,
		`{"conversationType":"channel","platformConversationId":"cnv_product","schemaVersion":1}`,
		`{"conversationType":"group","parentConversationId":"cnv_parent","platformConversationId":"cnv_product","schemaVersion":1}`,
		`{"agentInvocationId":"inv_finance","conversationType":"agent_thread","parentConversationId":"cnv_parent","platformConversationId":"cnv_thread","schemaVersion":1}`,
		`{"conversationType":"agent_thread","parentConversationId":"cnv_parent","platformConversationId":"cnv_thread","rootMessageId":"msg_root","schemaVersion":1}`,
		`{"agentInvocationId":"inv_finance","conversationType":"agent_thread","platformConversationId":"cnv_thread","rootMessageId":"msg_root","schemaVersion":1}`,
		`{"agentInvocationId":"inv_finance","conversationType":"agent_thread","parentConversationId":"cnv_thread","platformConversationId":"cnv_thread","rootMessageId":"msg_root","schemaVersion":1}`,
		`{"agentInvocationId":"msg_wrong","conversationType":"agent_thread","parentConversationId":"cnv_parent","platformConversationId":"cnv_thread","rootMessageId":"msg_root","schemaVersion":1}`,
		`{"agentInvocationId":"inv_finance","conversationType":"agent_thread","parentConversationId":"cnv_parent","platformConversationId":"cnv_thread","rootMessageId":"inv_wrong","schemaVersion":1}`,
		`{"agentInvocationId":"inv_finance","conversationType":"agent_thread","parentConversationId":"cnv_parent","platformConversationId":"cnv_ｔｈｒｅａｄ","rootMessageId":"msg_root","schemaVersion":1}`,
		`{"agentInvocationId":"inv_finance","conversationType":"agent_thread","parentConversationId":"cnv_parent","platformConversationId":"cnv_thread\u000aadmin","rootMessageId":"msg_root","schemaVersion":1}`,
		`{"schemaVersion":1,"conversationType":"group","platformConversationId":"cnv_product"}`,
	} {
		raw := raw
		t.Run(testName(raw), func(t *testing.T) {
			t.Parallel()
			projection, err := DecodeConversationProjection(raw)
			if !errors.Is(err, ErrInvalidProviderMetadata) || !projection.IsZero() {
				t.Fatalf("DecodeConversationProjection(%q) = (%#v, %v), want zero and ErrInvalidProviderMetadata", raw, projection, err)
			}
			if strings.Contains(err.Error(), "secret-canary") {
				t.Fatalf("error leaked rejected payload: %v", err)
			}
		})
	}
}

func TestConversationProjectionRejectsEveryNonCanonicalKeyPermutation(t *testing.T) {
	t.Parallel()

	for _, fields := range [][]string{
		{
			`"conversationType":"group"`,
			`"platformConversationId":"cnv_product"`,
			`"schemaVersion":1`,
		},
		{
			`"agentInvocationId":"inv_finance"`,
			`"conversationType":"agent_thread"`,
			`"parentConversationId":"cnv_parent"`,
			`"platformConversationId":"cnv_thread"`,
			`"rootMessageId":"msg_root"`,
			`"schemaVersion":1`,
		},
	} {
		canonical := "{" + strings.Join(fields, ",") + "}"
		accepted := 0
		for _, permutation := range permutations(fields) {
			raw := "{" + strings.Join(permutation, ",") + "}"
			projection, err := DecodeConversationProjection(raw)
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

func TestConversationProjectionRejectsOversizeBeforeParsing(t *testing.T) {
	t.Parallel()

	raw := `{"conversationType":"group","platformConversationId":"cnv_` + strings.Repeat("a", maxProviderMetadataBytes) + `","schemaVersion":1}`
	projection, err := DecodeConversationProjection(raw)
	if !errors.Is(err, ErrProviderMetadataTooLarge) || !projection.IsZero() {
		t.Fatalf("DecodeConversationProjection(oversize) = (%#v, %v), want zero and ErrProviderMetadataTooLarge", projection, err)
	}
}

func mustConversationProjection(
	t *testing.T,
	conversationType im.ConversationType,
	conversationIDValue string,
	parentIDValue string,
	rootMessageIDValue string,
	invocationIDValue string,
) ConversationProjection {
	t.Helper()
	conversationID := mustConversationID(t, conversationIDValue)
	var parentID im.ConversationID
	if parentIDValue != "" {
		parentID = mustConversationID(t, parentIDValue)
	}
	var rootMessageID im.MessageID
	if rootMessageIDValue != "" {
		rootMessageID = mustMessageID(t, rootMessageIDValue)
	}
	var invocationID im.InvocationID
	if invocationIDValue != "" {
		invocationID = mustInvocationID(t, invocationIDValue)
	}
	projection, err := NewConversationProjection(
		conversationType,
		conversationID,
		parentID,
		rootMessageID,
		invocationID,
	)
	if err != nil {
		t.Fatalf("NewConversationProjection() error = %v", err)
	}
	return projection
}

func mustConversationID(t *testing.T, value string) im.ConversationID {
	t.Helper()
	identifier, err := im.ParseConversationID(value)
	if err != nil {
		t.Fatalf("im.ParseConversationID(%q) error = %v", value, err)
	}
	return identifier
}

func mustMessageID(t *testing.T, value string) im.MessageID {
	t.Helper()
	identifier, err := im.ParseMessageID(value)
	if err != nil {
		t.Fatalf("im.ParseMessageID(%q) error = %v", value, err)
	}
	return identifier
}

func mustInvocationID(t *testing.T, value string) im.InvocationID {
	t.Helper()
	identifier, err := im.ParseInvocationID(value)
	if err != nil {
		t.Fatalf("im.ParseInvocationID(%q) error = %v", value, err)
	}
	return identifier
}
