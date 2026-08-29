package agentthread

import (
	"context"
	"errors"
	"strings"
	"testing"
	"time"

	"github.com/huapohen/quantum-entanglement/apps/im-api/internal/adapters/im/fake"
	"github.com/huapohen/quantum-entanglement/apps/im-api/internal/agentstore"
	"github.com/huapohen/quantum-entanglement/apps/im-api/internal/im"
	"github.com/huapohen/quantum-entanglement/apps/im-api/internal/immetadata"
)

func TestMentionPlanIsStableAndCreatesIndependentChildACL(t *testing.T) {
	t.Parallel()
	command, installation := threadTestCommand(t)
	first, err := PlanMention(command)
	if err != nil {
		t.Fatal(err)
	}
	second, err := PlanMention(command)
	if err != nil {
		t.Fatal(err)
	}
	if first.DedupeKey() != second.DedupeKey() || first.RequestDigestHex() != second.RequestDigestHex() ||
		first.Child().Ref() != second.Child().Ref() || first.IsZero() {
		t.Fatalf("mention planning is not deterministic: %#v %#v", first, second)
	}
	child := first.Child()
	if child.ConversationType() != im.ConversationAgentThread ||
		child.ParentConversationID() != command.Parent.Ref().ConversationID() ||
		child.RootMessageID() != command.RootMessage || child.AgentInvocationID() != first.InvocationID() {
		t.Fatalf("child lineage drift: %#v", child)
	}
	if first.HumanMembership().ConversationRef() != child.Ref() ||
		first.AgentMembership().ConversationRef() != child.Ref() ||
		first.AgentMembership().ActorRef().ActorID() != installation.AgentActor() {
		t.Fatalf("child memberships are not explicit: %#v %#v", first.HumanMembership(), first.AgentMembership())
	}
	if !first.AgentAccess().HasPermission(im.ConversationPermissionSendMessage) ||
		first.AgentAccess().HasPermission(im.ConversationPermissionManageConversation) ||
		first.AgentAccess().ConversationRef() == command.Parent.Ref() {
		t.Fatalf("agent child ACL is unsafe: %#v", first.AgentAccess())
	}
	projection, err := immetadata.DecodeConversationProjection(first.ProviderGroup().ExtInfo)
	if err != nil || projection.ConversationType() != im.ConversationAgentThread ||
		projection.ParentConversationID() != command.Parent.Ref().ConversationID() ||
		projection.RootMessageID() != command.RootMessage {
		t.Fatalf("provider child projection = %#v, %v", projection, err)
	}
	changed := command
	changed.RootMessage = threadMustMessage(t, "msg_other")
	changedPlan, err := PlanMention(changed)
	if err != nil {
		t.Fatal(err)
	}
	if changedPlan.DedupeKey() == first.DedupeKey() || changedPlan.Child().Ref() == first.Child().Ref() {
		t.Fatal("different root message reused child identity")
	}

	unauthorized := command
	unauthorized.RequestingAccess = threadMustAccess(
		t, command.Parent.Ref(), command.RequestingActor,
		[]im.ConversationPermission{im.ConversationPermissionRead, im.ConversationPermissionSendMessage},
	)
	if _, err := PlanMention(unauthorized); !errors.Is(err, ErrMentionUnauthorized) {
		t.Fatalf("missing invoke_agent permission = %v", err)
	}
}

func TestParentWorkCardIsCanonicalAndContainsNoChildContent(t *testing.T) {
	t.Parallel()
	command, _ := threadTestCommand(t)
	plan, err := PlanMention(command)
	if err != nil {
		t.Fatal(err)
	}
	encoded, err := EncodeParentWorkCard(plan.ParentCard())
	if err != nil {
		t.Fatal(err)
	}
	decoded, err := DecodeParentWorkCard(encoded, command.Parent.Ref().TenantID())
	if err != nil || decoded.Parent() != command.Parent.Ref() || decoded.Child() != plan.Child().Ref() ||
		decoded.InvocationID() != plan.InvocationID() || decoded.Status() != WorkCardStarted {
		t.Fatalf("work card round trip = %#v, %v", decoded, err)
	}
	for _, forbidden := range []string{"prompt", "response", "artifactDigest", "capabilities", "credentials", "acl"} {
		if strings.Contains(encoded, forbidden) {
			t.Fatalf("parent card contains forbidden child data %q: %s", forbidden, encoded)
		}
	}
	if _, err := DecodeParentWorkCard(encoded+" ", command.Parent.Ref().TenantID()); !errors.Is(err, ErrInvalidMention) {
		t.Fatalf("non-canonical card = %v", err)
	}
}

func TestAgentReplyCanOnlyTargetItsChildProviderGroup(t *testing.T) {
	t.Parallel()
	command, installation := threadTestCommand(t)
	plan, err := PlanMention(command)
	if err != nil {
		t.Fatal(err)
	}
	childProvider := threadMustProviderConversation(
		t, command.ProviderProfile.Realm, plan.Child().Ref().ConversationID().String(),
	)
	reply, err := BuildAgentReply(
		plan, installation, childProvider, threadMustMessage(t, "msg_reply"), "done",
		command.ProviderProfile, "reply/1",
	)
	if err != nil || reply.Conversation != childProvider || reply.Sender != installation.AgentActor() {
		t.Fatalf("child reply = %#v, %v", reply, err)
	}
	parentProvider := threadMustProviderConversation(
		t, command.ProviderProfile.Realm, command.Parent.Ref().ConversationID().String(),
	)
	if _, err := BuildAgentReply(
		plan, installation, parentProvider, threadMustMessage(t, "msg_reply2"), "leak",
		command.ProviderProfile, "reply/2",
	); !errors.Is(err, ErrReplyOutsideThread) {
		t.Fatalf("parent-directed agent reply = %v", err)
	}
}

func TestLocalCoordinatorRunsFakeChildGroupAndReplyVerticalSlice(t *testing.T) {
	t.Parallel()
	command, installation := threadTestCommand(t)
	provider, err := fake.New(fake.Options{Realm: command.ProviderProfile.Realm, AllowOutbound: true})
	if err != nil {
		t.Fatal(err)
	}
	ctx := context.Background()
	threadProvisionProviderUser(t, ctx, provider, command.RequestingActor.ActorID(), im.SubjectHuman, "user/human")
	threadProvisionProviderUser(t, ctx, provider, installation.AgentActor(), im.SubjectAgent, "user/agent")
	coordinator, err := NewLocalCoordinator(provider)
	if err != nil {
		t.Fatal(err)
	}
	result, err := coordinator.Open(ctx, command)
	if err != nil {
		t.Fatal(err)
	}
	replayed, err := coordinator.Open(ctx, command)
	if err != nil || !replayed.Replayed() || replayed.Plan().Child().Ref() != result.Plan().Child().Ref() {
		t.Fatalf("thread replay = %#v, %v", replayed, err)
	}
	receipt, err := coordinator.SendAgentReply(
		ctx, result, installation, threadMustMessage(t, "msg_reply"), "research complete", "reply/1",
	)
	if err != nil || receipt.Status != im.ProviderEffectCommitted {
		t.Fatalf("SendAgentReply() = %#v, %v", receipt, err)
	}
	sent := provider.SentMessages()
	if len(sent) != 1 || sent[0].Conversation.SubjectID() != result.Plan().Child().Ref().ConversationID().String() ||
		sent[0].Conversation.SubjectID() == command.Parent.Ref().ConversationID().String() {
		t.Fatalf("agent reply escaped child group: %#v", sent)
	}
}

func threadTestCommand(t *testing.T) (MentionCommand, agentstore.InstallationSnapshot) {
	t.Helper()
	tenant := threadMustTenant(t, "ten_acme")
	workspace := threadMustWorkspace(t, "wsp_product")
	parentRef := threadMustConversationRef(t, tenant, threadMustConversation(t, "cnv_parent"))
	parent, err := im.NewConversationSnapshot(
		parentRef, &workspace, im.ConversationGroup, im.ConversationActive,
		im.ConversationID{}, im.MessageID{}, im.InvocationID{}, 1,
	)
	if err != nil {
		t.Fatal(err)
	}
	requester := threadMustActorRef(t, tenant, threadMustActor(t, "usr_alice"))
	access := threadMustAccess(
		t, parentRef, requester,
		[]im.ConversationPermission{
			im.ConversationPermissionRead, im.ConversationPermissionSendMessage,
			im.ConversationPermissionInvokeAgent,
		},
	)
	installation := threadTestInstallation(t, tenant, workspace)
	realm := threadMustRealm(t, "rlm_fake")
	profile, err := im.NewProviderProfile(
		im.IdentityProviderRongCloud, realm,
		[]im.ProviderCapability{
			im.ProviderCapabilityHealth, im.ProviderCapabilityGroupCreate,
			im.ProviderCapabilityUserProvision, im.ProviderCapabilityTextSend,
		}, im.ProviderMaxTextBytes, im.ProviderMaxTextBytes, 1024,
	)
	if err != nil {
		t.Fatal(err)
	}
	return MentionCommand{
		Parent: parent, RequestingActor: requester, RequestingAccess: access,
		RootMessage: threadMustMessage(t, "msg_root"), AgentInstallation: installation,
		ProviderProfile: profile,
	}, installation
}

func threadTestInstallation(t *testing.T, tenant im.TenantID, workspace im.WorkspaceID) agentstore.InstallationSnapshot {
	t.Helper()
	definitionID, _ := im.ParseAgentDefinitionID("agd_research")
	owner, _ := im.ParseHumanPrincipalID("hpr_alice")
	publisher, _ := agentstore.ParsePublisherID("pub_acme")
	definition, err := agentstore.NewDefinitionSnapshot(
		definitionID, tenant, owner, publisher, "Research Agent", "Produces cited research.",
		agentstore.DefinitionActive, 1,
	)
	if err != nil {
		t.Fatal(err)
	}
	releaseID, _ := agentstore.ParseReleaseID("agr_research_100")
	version, _ := im.ParseAgentVersion("1.0.0")
	capability, _ := agentstore.ParseCapability("conversation.read")
	route, err := agentstore.NewDataRoute(
		"conversation.context", agentstore.DataInput, agentstore.DataConfidential,
		[]string{"local", "provider:rongcloud"}, 30,
	)
	if err != nil {
		t.Fatal(err)
	}
	publishedAt := time.Unix(1700000000, 0).UTC()
	release, err := agentstore.NewReleaseSnapshot(
		releaseID, definitionID, version, agentstore.DigestBytes([]byte("artifact")),
		agentstore.DigestBytes([]byte("manifest")), agentstore.DigestBytes([]byte("persona")),
		[]agentstore.Capability{capability}, nil, []agentstore.DataRoute{route},
		agentstore.IsolationMicroVM, agentstore.ReleasePublished, publishedAt, 1,
	)
	if err != nil {
		t.Fatal(err)
	}
	security, _ := agentstore.ParsePublisherID("pub_security")
	attestations := make([]agentstore.TrustAttestation, 0, 3)
	for _, claim := range []agentstore.AttestationClaim{
		agentstore.AttestationPublisherVerified,
		agentstore.AttestationSecurityReviewed,
		agentstore.AttestationDataRoutesReviewed,
	} {
		value, err := agentstore.NewTrustAttestation(
			security, claim, 1, agentstore.DigestBytes([]byte(claim)),
			publishedAt.Add(-time.Hour), publishedAt.Add(24*time.Hour),
		)
		if err != nil {
			t.Fatal(err)
		}
		attestations = append(attestations, value)
	}
	passport, err := agentstore.NewTrustPassport(definition, release, attestations, agentstore.PassportActive, 1)
	if err != nil {
		t.Fatal(err)
	}
	installationID, _ := agentstore.ParseInstallationID("ins_research")
	agent := threadMustActor(t, "agt_research")
	installation, err := agentstore.NewInstallationSnapshot(
		installationID, tenant, workspace, agent, owner, passport,
		[]agentstore.Capability{capability}, []string{"conversation.context"},
		agentstore.InstallationActive, publishedAt.Add(time.Hour), time.Time{}, 1,
	)
	if err != nil {
		t.Fatal(err)
	}
	return installation
}

func threadProvisionProviderUser(
	t *testing.T,
	ctx context.Context,
	provider im.Provider,
	actor im.ActorID,
	subjectType im.SubjectType,
	idempotencyKey string,
) {
	t.Helper()
	var definitionID im.AgentDefinitionID
	var version im.AgentVersion
	if subjectType == im.SubjectAgent {
		definitionID, _ = im.ParseAgentDefinitionID("agd_research")
		version, _ = im.ParseAgentVersion("1.0.0")
	}
	projection, err := immetadata.NewUserProjection(subjectType, actor, definitionID, version)
	if err != nil {
		t.Fatal(err)
	}
	extInfo, err := immetadata.EncodeUserProjection(projection)
	if err != nil {
		t.Fatal(err)
	}
	if _, _, err := provider.ProvisionUser(ctx, im.ProviderUserProvision{
		Actor: actor, DisplayName: actor.String(), ExtInfo: extInfo, IdempotencyKey: idempotencyKey,
	}); err != nil {
		t.Fatal(err)
	}
}

func threadMustTenant(t *testing.T, value string) im.TenantID {
	t.Helper()
	parsed, err := im.ParseTenantID(value)
	if err != nil {
		t.Fatal(err)
	}
	return parsed
}

func threadMustWorkspace(t *testing.T, value string) im.WorkspaceID {
	t.Helper()
	parsed, err := im.ParseWorkspaceID(value)
	if err != nil {
		t.Fatal(err)
	}
	return parsed
}

func threadMustRealm(t *testing.T, value string) im.ProviderRealmID {
	t.Helper()
	parsed, err := im.ParseProviderRealmID(value)
	if err != nil {
		t.Fatal(err)
	}
	return parsed
}

func threadMustActor(t *testing.T, value string) im.ActorID {
	t.Helper()
	parsed, err := im.ParseActorID(value)
	if err != nil {
		t.Fatal(err)
	}
	return parsed
}

func threadMustConversation(t *testing.T, value string) im.ConversationID {
	t.Helper()
	parsed, err := im.ParseConversationID(value)
	if err != nil {
		t.Fatal(err)
	}
	return parsed
}

func threadMustMessage(t *testing.T, value string) im.MessageID {
	t.Helper()
	parsed, err := im.ParseMessageID(value)
	if err != nil {
		t.Fatal(err)
	}
	return parsed
}

func threadMustConversationRef(t *testing.T, tenant im.TenantID, conversation im.ConversationID) im.ConversationRef {
	t.Helper()
	parsed, err := im.NewConversationRef(tenant, conversation)
	if err != nil {
		t.Fatal(err)
	}
	return parsed
}

func threadMustActorRef(t *testing.T, tenant im.TenantID, actor im.ActorID) im.ActorRef {
	t.Helper()
	parsed, err := im.NewActorRef(tenant, actor)
	if err != nil {
		t.Fatal(err)
	}
	return parsed
}

func threadMustAccess(
	t *testing.T,
	conversation im.ConversationRef,
	actor im.ActorRef,
	permissions []im.ConversationPermission,
) im.ConversationAccessSnapshot {
	t.Helper()
	parsed, err := im.NewConversationAccessSnapshot(conversation, actor, permissions, 1)
	if err != nil {
		t.Fatal(err)
	}
	return parsed
}

func threadMustProviderConversation(
	t *testing.T,
	realm im.ProviderRealmID,
	conversationID string,
) im.ProviderConversationRef {
	t.Helper()
	parsed, err := im.NewProviderConversationRef(im.IdentityProviderRongCloud, realm, conversationID)
	if err != nil {
		t.Fatal(err)
	}
	return parsed
}
