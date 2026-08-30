package immetadata

import (
	"fmt"
	"sync"
	"testing"

	"github.com/huapohen/quantum-entanglement/apps/im-api/internal/im"
)

func TestProviderMetadataCodecIsDeterministicUnderConcurrency(t *testing.T) {
	t.Parallel()

	user := mustUserProjection(t, im.SubjectAgent, "agt_finance", "agd_finance", "1.0.0")
	userGolden := `{"agentDefinitionId":"agd_finance","agentVersion":"1.0.0","platformActorId":"agt_finance","schemaVersion":1,"subjectType":"agent"}`
	conversation := mustConversationProjection(
		t,
		im.ConversationAgentThread,
		"cnv_thread",
		"cnv_parent",
		"msg_root",
		"inv_finance",
	)
	conversationGolden := `{"agentInvocationId":"inv_finance","conversationType":"agent_thread","parentConversationId":"cnv_parent","platformConversationId":"cnv_thread","rootMessageId":"msg_root","schemaVersion":1}`

	const workers = 128
	const iterations = 100
	errorsFound := make(chan error, workers)
	var waitGroup sync.WaitGroup
	for worker := 0; worker < workers; worker++ {
		waitGroup.Add(1)
		go func() {
			defer waitGroup.Done()
			for iteration := 0; iteration < iterations; iteration++ {
				encodedUser, err := EncodeUserProjection(user)
				if err != nil || encodedUser != userGolden {
					errorsFound <- fmt.Errorf("user encode = (%q, %v)", encodedUser, err)
					return
				}
				decodedUser, err := DecodeUserProjection(encodedUser)
				if err != nil || decodedUser != user {
					errorsFound <- fmt.Errorf("user decode = (%#v, %v)", decodedUser, err)
					return
				}

				encodedConversation, err := EncodeConversationProjection(conversation)
				if err != nil || encodedConversation != conversationGolden {
					errorsFound <- fmt.Errorf("conversation encode = (%q, %v)", encodedConversation, err)
					return
				}
				decodedConversation, err := DecodeConversationProjection(encodedConversation)
				if err != nil || decodedConversation != conversation {
					errorsFound <- fmt.Errorf("conversation decode = (%#v, %v)", decodedConversation, err)
					return
				}
			}
		}()
	}
	waitGroup.Wait()
	close(errorsFound)
	for err := range errorsFound {
		t.Error(err)
	}
}
