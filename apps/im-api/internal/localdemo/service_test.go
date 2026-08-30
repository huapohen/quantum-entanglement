package localdemo

import (
	"context"
	"errors"
	"strings"
	"sync"
	"testing"

	"github.com/huapohen/quantum-entanglement/apps/im-api/internal/modelruntime"
)

type recordingRuntime struct {
	calls int
}

func (runtime *recordingRuntime) Generate(_ context.Context, request modelruntime.Request) (modelruntime.Result, error) {
	runtime.calls++
	return modelruntime.Result{
		Text:     "# 模型结果\n任务：" + request.Instruction,
		Provider: "test", Model: "test-model", ResponseID: "resp_local",
	}, nil
}

func (*recordingRuntime) Descriptor() modelruntime.Descriptor {
	return modelruntime.Descriptor{Mode: "model", Provider: "test", Model: "test-model", Status: "configured"}
}

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

func TestServiceUsesInjectedModelRuntimeOutputInAgentChild(t *testing.T) {
	t.Parallel()
	runtime := &recordingRuntime{}
	service, err := NewWithRuntime(runtime)
	if err != nil {
		t.Fatal(err)
	}
	if snapshot := service.Snapshot(); snapshot.Mode != "model-runtime" || snapshot.AgentRuntime.Model != "test-model" {
		t.Fatalf("model runtime snapshot = %#v", snapshot)
	}
	result, err := service.Mention(context.Background(), LocalBearerToken, MentionInput{
		MessageID: "msg_model_runtime", Instruction: "生成动态验收结果",
	})
	if err != nil {
		t.Fatal(err)
	}
	if runtime.calls != 1 || !strings.Contains(result.AgentReply.Text, "# 模型结果") {
		t.Fatalf("runtime calls/result = %d, %#v", runtime.calls, result)
	}
	messages, err := service.ListMessages(context.Background(), LocalBearerToken, result.ChildConversationID, "", 20)
	if err != nil || len(messages.Messages) != 1 || messages.Messages[0].Text != result.AgentReply.Text {
		t.Fatalf("child model message = %#v, %v", messages, err)
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
	if len(page.Agents) != 2 {
		t.Fatalf("agent store count = %d, want 2", len(page.Agents))
	}
	var agent AgentStoreView
	var available AgentStoreView
	for _, candidate := range page.Agents {
		if candidate.InstallationStatus == "active" {
			agent = candidate
		} else if candidate.InstallationStatus == "available" {
			available = candidate
		}
	}
	if agent.DefinitionID == "" || available.DefinitionID == "" {
		t.Fatalf("agent store must contain active and available entries: %#v", page.Agents)
	}
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
	if available.DefinitionID != "agd_local_planner" || !available.CanInstall || available.InstallationID != "" {
		t.Fatalf("available Agent Store entry = %#v", available)
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

func TestServiceInstallsCatalogAgentIdempotentlyAndAddsItToParent(t *testing.T) {
	t.Parallel()
	service, err := New()
	if err != nil {
		t.Fatal(err)
	}
	first, err := service.InstallAgent(context.Background(), LocalBearerToken, "agd_local_planner", AgentStoreInstallInput{
		IdempotencyKey: "test/store/install/planner",
	})
	if err != nil {
		t.Fatal(err)
	}
	if first.Replayed || first.Agent.InstallationStatus != "active" || first.Agent.AgentActorID != "agt_local_planner" ||
		first.Agent.GrantedCapabilities == nil || first.Agent.CanInstall {
		t.Fatalf("first install = %#v", first)
	}
	replay, err := service.InstallAgent(context.Background(), LocalBearerToken, "agd_local_planner", AgentStoreInstallInput{
		IdempotencyKey: "test/store/install/planner",
	})
	if err != nil || !replay.Replayed || replay.Agent.InstallationID != first.Agent.InstallationID {
		t.Fatalf("install replay = %#v, %v", replay, err)
	}
	snapshot := service.Snapshot()
	if snapshot.AgentActorID != "agt_local_planner" || snapshot.AgentVersion != "1.0.0" {
		t.Fatalf("selected installed agent = %#v", snapshot)
	}
	conversations, err := service.ListConversations(context.Background(), LocalBearerToken, "", 20)
	if err != nil || len(conversations.Conversations) == 0 ||
		!containsString(conversations.Conversations[0].MemberActorIDs, "agt_local_planner") {
		t.Fatalf("parent after install = %#v, %v", conversations, err)
	}
	page, err := service.ListAgents(context.Background(), LocalBearerToken)
	if err != nil {
		t.Fatal(err)
	}
	var retired bool
	for _, agent := range page.Agents {
		if agent.DefinitionID == "agd_local_research" && agent.InstallationStatus == "offboarded" {
			retired = true
		}
	}
	if !retired {
		t.Fatalf("previous installation not retired: %#v", page.Agents)
	}
	mention, mentionErr := service.Mention(context.Background(), LocalBearerToken, MentionInput{
		ConversationID: service.Snapshot().ParentConversationID, MessageID: "msg_install_after", Instruction: "安装后的 Agent 指令",
	})
	if mentionErr != nil {
		t.Fatalf("mention after install = %#v, %v", mention, mentionErr)
	}
}

func containsString(values []string, target string) bool {
	for _, value := range values {
		if value == target {
			return true
		}
	}
	return false
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
