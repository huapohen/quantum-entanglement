package localdemo

import (
	"context"
	"errors"
	"sync"
	"testing"
)

func TestServiceRunsAuthenticatedMentionAndReplay(t *testing.T) {
	t.Parallel()
	service, err := New()
	if err != nil {
		t.Fatal(err)
	}
	snapshot := service.Snapshot()
	if snapshot.Mode != "zero-network-fake" || snapshot.NetworkCalls != 0 ||
		snapshot.ParentConversationID == "" || snapshot.AgentActorID == "" {
		t.Fatalf("unexpected snapshot: %#v", snapshot)
	}
	input := MentionInput{MessageID: "msg_demo_1", Instruction: "调研三个竞品并输出证据表"}
	result, err := service.Mention(context.Background(), LocalBearerToken, input)
	if err != nil {
		t.Fatal(err)
	}
	if result.ParentConversationID != snapshot.ParentConversationID ||
		result.ChildConversationID == "" || result.ChildConversationID == result.ParentConversationID ||
		result.AgentReply.ConversationID != result.ChildConversationID ||
		result.AgentReply.SenderActorID != snapshot.AgentActorID || result.Replayed ||
		result.ProviderStatus != "committed" || result.WorkCardExtInfo == "" {
		t.Fatalf("unexpected mention result: %#v", result)
	}
	replay, err := service.Mention(context.Background(), LocalBearerToken, input)
	if err != nil || !replay.Replayed || replay.ChildConversationID != result.ChildConversationID ||
		replay.InvocationID != result.InvocationID {
		t.Fatalf("mention replay = %#v, %v", replay, err)
	}
}

func TestServiceListsAuthenticatedAgentStoreProjection(t *testing.T) {
	t.Parallel()
	service, err := New()
	if err != nil {
		t.Fatal(err)
	}
	page, err := service.ListAgents(context.Background(), LocalBearerToken)
	if err != nil {
		t.Fatal(err)
	}
	if len(page.Agents) != 1 {
		t.Fatalf("agent store count = %d, want 1", len(page.Agents))
	}
	agent := page.Agents[0]
	if agent.Name != "v0版研究 Agent" || agent.Version != "1.0.0" ||
		agent.DefinitionStatus != "active" || agent.ReleaseStatus != "published" ||
		agent.PassportStatus != "active" || agent.InstallationStatus != "active" ||
		agent.AgentActorID != "agt_local_research" || agent.Isolation != "process" {
		t.Fatalf("agent store projection = %#v", agent)
	}
	if len(agent.RequestedCapabilities) != 1 || agent.RequestedCapabilities[0] != "conversation.read" ||
		len(agent.GrantedCapabilities) != 1 || agent.GrantedCapabilities[0] != "conversation.read" {
		t.Fatalf("agent capabilities = %#v", agent)
	}
	if len(agent.DataRoutes) != 1 || agent.DataRoutes[0].Name != "conversation.context" ||
		agent.DataRoutes[0].Classification != "confidential" || len(agent.DataRoutes[0].Destinations) != 2 {
		t.Fatalf("agent data routes = %#v", agent.DataRoutes)
	}
	if len(agent.Attestations) != 3 {
		t.Fatalf("agent attestations = %#v", agent.Attestations)
	}
}

func TestServiceAgentStoreRejectsWrongToken(t *testing.T) {
	t.Parallel()
	service, err := New()
	if err != nil {
		t.Fatal(err)
	}
	if _, err := service.ListAgents(context.Background(), "wrong.local.token"); !errors.Is(err, ErrUnauthenticated) {
		t.Fatalf("wrong token = %v", err)
	}
}

func TestServiceAddsInstalledAgentToExistingGroupIdempotently(t *testing.T) {
	t.Parallel()
	service, err := New()
	if err != nil {
		t.Fatal(err)
	}
	created, err := service.CreateConversation(context.Background(), LocalBearerToken, CreateConversationInput{
		Type: "group", Name: "成员动作验收群", IdempotencyKey: "test/members/create",
	})
	if err != nil {
		t.Fatal(err)
	}
	first, err := service.AddMembers(context.Background(), LocalBearerToken, created.Conversation.ID, AddMembersInput{
		MemberActorIDs: []string{"agt_local_research"}, IdempotencyKey: "test/members/add/1",
	})
	if err != nil {
		t.Fatal(err)
	}
	if first.Replayed || len(first.AddedActorIDs) != 1 || first.AddedActorIDs[0] != "agt_local_research" ||
		len(first.Conversation.MemberActorIDs) != 2 {
		t.Fatalf("first member update = %#v", first)
	}
	replay, err := service.AddMembers(context.Background(), LocalBearerToken, created.Conversation.ID, AddMembersInput{
		MemberActorIDs: []string{"agt_local_research"}, IdempotencyKey: "test/members/add/1",
	})
	if err != nil || !replay.Replayed || len(replay.AddedActorIDs) != 1 {
		t.Fatalf("member update replay = %#v, %v", replay, err)
	}
	if _, err := service.AddMembers(context.Background(), LocalBearerToken, created.Conversation.ID, AddMembersInput{
		MemberActorIDs: []string{"agt_local_research"}, IdempotencyKey: "test/members/add/1-different",
	}); err != nil {
		t.Fatalf("already-present member with new key should be a no-op: %v", err)
	}
}

func TestServiceMentionUsesSelectedGroupAndMaterializesThreadProjection(t *testing.T) {
	t.Parallel()
	service, err := New()
	if err != nil {
		t.Fatal(err)
	}
	created, err := service.CreateConversation(context.Background(), LocalBearerToken, CreateConversationInput{
		Type: "group", Name: "指定父群验收群", MemberActorIDs: []string{"agt_local_research"},
		IdempotencyKey: "test/mention/selected-group",
	})
	if err != nil {
		t.Fatal(err)
	}
	result, err := service.Mention(context.Background(), LocalBearerToken, MentionInput{
		ConversationID: created.Conversation.ID, MessageID: "msg_selected_group_1", Instruction: "在指定群里执行",
	})
	if err != nil {
		t.Fatal(err)
	}
	if result.ParentConversationID != created.Conversation.ID || result.ChildConversationID == "" {
		t.Fatalf("selected group mention = %#v", result)
	}
	conversations, err := service.ListConversations(context.Background(), LocalBearerToken, "", 20)
	if err != nil {
		t.Fatal(err)
	}
	var foundChild bool
	for _, conversation := range conversations.Conversations {
		if conversation.ID == result.ChildConversationID && conversation.ParentConversationID == created.Conversation.ID {
			foundChild = true
		}
	}
	if !foundChild {
		t.Fatalf("materialized child missing: %#v", conversations.Conversations)
	}
	childMessages, err := service.ListMessages(context.Background(), LocalBearerToken, result.ChildConversationID, "", 20)
	if err != nil || len(childMessages.Messages) != 1 || childMessages.Messages[0].SenderActorID != service.Snapshot().AgentActorID {
		t.Fatalf("child projection = %#v, %v", childMessages, err)
	}
	parentMessages, err := service.ListMessages(context.Background(), LocalBearerToken, created.Conversation.ID, "", 20)
	if err != nil || len(parentMessages.Messages) != 1 || parentMessages.Messages[0].ExtInfo == "" {
		t.Fatalf("parent work card projection = %#v, %v", parentMessages, err)
	}
	if _, err := service.Mention(context.Background(), LocalBearerToken, MentionInput{
		ConversationID: created.Conversation.ID, MessageID: "msg_selected_group_1", Instruction: "changed",
	}); !errors.Is(err, ErrConflict) {
		t.Fatalf("selected group mention drift = %v", err)
	}
}

func TestServiceRejectsAuthenticationValidationAndMessageDrift(t *testing.T) {
	t.Parallel()
	service, err := New()
	if err != nil {
		t.Fatal(err)
	}
	input := MentionInput{MessageID: "msg_demo_1", Instruction: "first instruction"}
	if _, err := service.Mention(context.Background(), "wrong.local.token", input); !errors.Is(err, ErrUnauthenticated) {
		t.Fatalf("wrong token = %v", err)
	}
	if _, err := service.Mention(context.Background(), LocalBearerToken, MentionInput{
		MessageID: "msg_demo_2", Instruction: "unsafe\ninstruction",
	}); !errors.Is(err, ErrInvalidInput) {
		t.Fatalf("control character instruction = %v", err)
	}
	if _, err := service.Mention(context.Background(), LocalBearerToken, input); err != nil {
		t.Fatal(err)
	}
	input.Instruction = "changed instruction"
	if _, err := service.Mention(context.Background(), LocalBearerToken, input); !errors.Is(err, ErrConflict) {
		t.Fatalf("message identity drift = %v", err)
	}
}

func TestServiceSameInstructionAcrossDifferentMessagesDoesNotCollide(t *testing.T) {
	t.Parallel()
	service, err := New()
	if err != nil {
		t.Fatal(err)
	}
	first, err := service.Mention(context.Background(), LocalBearerToken, MentionInput{
		MessageID: "msg_demo_1", Instruction: "same instruction",
	})
	if err != nil {
		t.Fatal(err)
	}
	second, err := service.Mention(context.Background(), LocalBearerToken, MentionInput{
		MessageID: "msg_demo_2", Instruction: "same instruction",
	})
	if err != nil {
		t.Fatal(err)
	}
	if first.ChildConversationID == second.ChildConversationID || second.ProviderStatus != "committed" {
		t.Fatalf("different messages collided: %#v %#v", first, second)
	}
}

func TestServiceConcurrentReplayConverges(t *testing.T) {
	t.Parallel()
	service, err := New()
	if err != nil {
		t.Fatal(err)
	}
	input := MentionInput{MessageID: "msg_concurrent", Instruction: "run once"}
	const workers = 8
	results := make(chan MentionResult, workers)
	errorsOut := make(chan error, workers)
	var wait sync.WaitGroup
	for index := 0; index < workers; index++ {
		wait.Add(1)
		go func() {
			defer wait.Done()
			result, err := service.Mention(context.Background(), LocalBearerToken, input)
			results <- result
			errorsOut <- err
		}()
	}
	wait.Wait()
	close(results)
	close(errorsOut)
	for err := range errorsOut {
		if err != nil {
			t.Fatalf("concurrent mention: %v", err)
		}
	}
	var child string
	for result := range results {
		if child == "" {
			child = result.ChildConversationID
		}
		if result.ChildConversationID != child {
			t.Fatalf("concurrent mentions diverged: %q != %q", result.ChildConversationID, child)
		}
	}
}
