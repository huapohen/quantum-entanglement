package immetadata

import (
	"errors"
	"testing"
)

func FuzzDecodeUserProjectionNeverPanics(f *testing.F) {
	for _, seed := range [][]byte{
		[]byte(`{"platformActorId":"usr_alice","schemaVersion":1,"subjectType":"human"}`),
		[]byte(`{"agentDefinitionId":"agd_finance","agentVersion":"1.0.0","platformActorId":"agt_finance","schemaVersion":1,"subjectType":"agent"}`),
		[]byte(`{"platformActorId":"usr_alice","schemaVersion":1,"schemaVersion":1,"subjectType":"human"}`),
		{0xff, 0xfe, 0xfd},
	} {
		f.Add(seed)
	}

	f.Fuzz(func(t *testing.T, raw []byte) {
		projection, err := DecodeUserProjection(string(raw))
		if err != nil {
			if !projection.IsZero() ||
				(!errors.Is(err, ErrInvalidProviderMetadata) &&
					!errors.Is(err, ErrProviderMetadataTooLarge)) {
				t.Fatalf("rejected user projection = (%#v, %v)", projection, err)
			}
			return
		}
		if projection.IsZero() {
			t.Fatal("accepted user projection is zero")
		}
		encoded, encodeErr := EncodeUserProjection(projection)
		if encodeErr != nil || encoded != string(raw) {
			t.Fatalf("accepted user metadata is not canonical: encode = (%q, %v), raw = %q", encoded, encodeErr, raw)
		}
	})
}

func FuzzDecodeConversationProjectionNeverPanics(f *testing.F) {
	for _, seed := range [][]byte{
		[]byte(`{"conversationType":"group","platformConversationId":"cnv_product","schemaVersion":1}`),
		[]byte(`{"agentInvocationId":"inv_finance","conversationType":"agent_thread","parentConversationId":"cnv_parent","platformConversationId":"cnv_thread","rootMessageId":"msg_root","schemaVersion":1}`),
		[]byte(`{"conversationType":"group","platformConversationId":"cnv_product","schemaVersion":1,"acl":true}`),
		{0xf0, 0x28, 0x8c, 0x28},
	} {
		f.Add(seed)
	}

	f.Fuzz(func(t *testing.T, raw []byte) {
		projection, err := DecodeConversationProjection(string(raw))
		if err != nil {
			if !projection.IsZero() ||
				(!errors.Is(err, ErrInvalidProviderMetadata) &&
					!errors.Is(err, ErrProviderMetadataTooLarge)) {
				t.Fatalf("rejected conversation projection = (%#v, %v)", projection, err)
			}
			return
		}
		if projection.IsZero() {
			t.Fatal("accepted conversation projection is zero")
		}
		encoded, encodeErr := EncodeConversationProjection(projection)
		if encodeErr != nil || encoded != string(raw) {
			t.Fatalf("accepted conversation metadata is not canonical: encode = (%q, %v), raw = %q", encoded, encodeErr, raw)
		}
	})
}
