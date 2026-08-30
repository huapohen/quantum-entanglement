// Package localdemo composes the native IM contracts into a loopback-only demo. Its default
// runtime is deterministic and zero-network; an explicit modelruntime can be injected for a
// controlled local model trial without changing the IM/provider contracts.
package localdemo

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"strings"
	"sync"
	"time"
	"unicode"
	"unicode/utf8"

	authfake "github.com/huapohen/quantum-entanglement/apps/im-api/internal/adapters/auth/fake"
	imfake "github.com/huapohen/quantum-entanglement/apps/im-api/internal/adapters/im/fake"
	"github.com/huapohen/quantum-entanglement/apps/im-api/internal/agentstore"
	"github.com/huapohen/quantum-entanglement/apps/im-api/internal/agentthread"
	"github.com/huapohen/quantum-entanglement/apps/im-api/internal/auth"
	"github.com/huapohen/quantum-entanglement/apps/im-api/internal/im"
	"github.com/huapohen/quantum-entanglement/apps/im-api/internal/immetadata"
	"github.com/huapohen/quantum-entanglement/apps/im-api/internal/modelruntime"
	"golang.org/x/text/unicode/norm"
)

var (
	ErrInvalidInput    = errors.New("invalid local IM demo input")
	ErrUnauthenticated = errors.New("local IM demo authentication failed")
	ErrConflict        = errors.New("local IM demo message identity conflict")
	ErrRuntime         = errors.New("local IM Agent runtime failed")
)

const (
	LocalBearerToken     = "demo.local.signature"
	LocalExternalSubject = "user_local_demo"
	maxInstructionBytes  = 4096
)

type MentionInput struct {
	ConversationID string `json:"conversationId,omitempty"`
	MessageID      string `json:"messageId"`
	Instruction    string `json:"instruction"`
}

type AgentReplyView struct {
	ConversationID string `json:"conversationId"`
	SenderActorID  string `json:"senderActorId"`
	Text           string `json:"text"`
}

type MentionResult struct {
	ParentConversationID string         `json:"parentConversationId"`
	ChildConversationID  string         `json:"childConversationId"`
	InvocationID         string         `json:"invocationId"`
	TaskID               string         `json:"taskId"`
	ArtifactID           string         `json:"artifactId"`
	NeedsYouID           string         `json:"needsYouId"`
	WorkCardExtInfo      string         `json:"workCardExtInfo"`
	AgentReply           AgentReplyView `json:"agentReply"`
	Replayed             bool           `json:"replayed"`
	ProviderStatus       string         `json:"providerStatus"`
}

type Snapshot struct {
	Mode                 string                  `json:"mode"`
	NetworkCalls         int                     `json:"networkCalls"`
	AuthProvider         string                  `json:"authProvider"`
	IMProvider           string                  `json:"imProvider"`
	ParentConversationID string                  `json:"parentConversationId"`
	HumanActorID         string                  `json:"humanActorId"`
	AgentActorID         string                  `json:"agentActorId"`
	AgentVersion         string                  `json:"agentVersion"`
	AgentRuntime         modelruntime.Descriptor `json:"agentRuntime"`
}

type Service struct {
	mu                    sync.Mutex
	authVerifier          auth.Verifier
	provider              *imfake.Provider
	coordinator           *agentthread.LocalCoordinator
	parent                im.ConversationSnapshot
	requester             im.ActorRef
	requestAccess         im.ConversationAccessSnapshot
	installation          agentstore.InstallationSnapshot
	passport              agentstore.TrustPassport
	agentCatalog          []agentCatalogRecord
	agentInstallRequests  map[string]agentInstallRecord
	agentOffboardRequests map[string]agentOffboardRecord
	runtime               modelruntime.Runtime
	runtimeCalls          int
	requests              map[string][sha256.Size]byte
	mentionResults        map[string]MentionResult
	knownActors           map[im.ActorID]im.ActorRef
	conversations         map[im.ConversationID]*localConversation
	conversationOrder     []im.ConversationID
	conversationCreates   map[string]createRecord
	memberUpdates         map[string]memberUpdateRecord
	tasks                 map[string]TaskView
	taskOrder             []string
	artifacts             map[string]ArtifactView
	needsYou              map[string]NeedsYouView
	cursorNamespaceHex    string
}

func New() (*Service, error) {
	return NewWithRuntime(modelruntime.NewDeterministic())
}

func NewFromEnv(lookup modelruntime.LookupEnv) (*Service, error) {
	runtime, err := modelruntime.FromEnv(lookup)
	if err != nil {
		return nil, err
	}
	return NewWithRuntime(runtime)
}

func NewWithRuntime(runtime modelruntime.Runtime) (*Service, error) {
	if runtime == nil {
		runtime = modelruntime.NewDeterministic()
	}
	now := time.Now().UTC()
	tenant, _, parent, requester, requestAccess, installation, passport, err := buildPlatform(now)
	if err != nil {
		return nil, err
	}
	plannerPassport, err := buildAgentPassport(
		now, tenant, "agd_local_planner", "agr_local_planner_100", "v0版规划 Agent",
		"把复杂目标拆成可审阅执行计划的本地零网络 Agent。", "conversation.read",
	)
	if err != nil {
		return nil, err
	}
	realm, err := im.ParseProviderRealmID("rlm_local_demo")
	if err != nil {
		return nil, err
	}
	verifier, err := authfake.New(authfake.Options{
		Realm: realm, Issuer: "clerk.local-demo", Audience: "wanwork-local-demo",
		Now: func() time.Time { return time.Now().UTC() },
		Tokens: map[string]authfake.TokenFixture{
			LocalBearerToken: {
				ExternalSubject: LocalExternalSubject, SessionID: "sess_local_demo",
				IssuedAt: now.Add(-time.Hour), ExpiresAt: now.Add(24 * time.Hour),
			},
		},
	})
	if err != nil {
		return nil, err
	}
	provider, err := imfake.New(imfake.Options{Realm: realm, AllowOutbound: true})
	if err != nil {
		return nil, err
	}
	if err := provisionHuman(context.Background(), provider, requester.ActorID()); err != nil {
		return nil, err
	}
	agentProvision, err := agentstore.BuildProviderUserProvision(
		installation, passport, provider.Profile(), "demo/user/agent",
	)
	if err != nil {
		return nil, err
	}
	if _, _, err := provider.ProvisionUser(context.Background(), agentProvision); err != nil {
		return nil, err
	}
	if err := createParentGroup(context.Background(), provider, parent.Ref(), requester.ActorID(), installation.AgentActor()); err != nil {
		return nil, err
	}
	coordinator, err := agentthread.NewLocalCoordinator(provider)
	if err != nil {
		return nil, err
	}
	agentRef, err := im.NewActorRef(tenant, installation.AgentActor())
	if err != nil {
		return nil, err
	}
	humanMembership, err := im.NewConversationMembershipSnapshot(
		parent.Ref(), requester, im.ConversationMembershipOwner,
		im.ConversationMembershipActive, 1,
	)
	if err != nil {
		return nil, err
	}
	agentMembership, err := im.NewConversationMembershipSnapshot(
		parent.Ref(), agentRef, im.ConversationMembershipMember,
		im.ConversationMembershipActive, 1,
	)
	if err != nil {
		return nil, err
	}
	agentAccess, err := im.NewConversationAccessSnapshot(
		parent.Ref(), agentRef, []im.ConversationPermission{im.ConversationPermissionRead}, 1,
	)
	if err != nil {
		return nil, err
	}
	providerRef, err := im.NewProviderConversationRef(
		im.IdentityProviderRongCloud, provider.Profile().Realm, parent.Ref().ConversationID().String(),
	)
	if err != nil {
		return nil, err
	}
	cursorNamespace := sha256.Sum256([]byte("wanwork.local-demo-cursor/1\x00" + parent.Ref().ConversationID().String() + "\x00" + now.Format(time.RFC3339Nano)))
	parentRecord := &localConversation{
		snapshot: parent, name: "产品研发群",
		members: map[im.ActorID]im.ConversationMembershipSnapshot{
			requester.ActorID(): humanMembership, installation.AgentActor(): agentMembership,
		},
		access: map[im.ActorID]im.ConversationAccessSnapshot{
			requester.ActorID(): requestAccess, installation.AgentActor(): agentAccess,
		},
		providerRef: providerRef, providerBound: true, providerStatus: string(im.ProviderEffectCommitted),
		createdAt: now, messages: make([]localMessage, 0), byClient: make(map[im.MessageID]int),
	}
	return &Service{
		authVerifier: verifier, provider: provider, coordinator: coordinator,
		parent: parent, requester: requester, requestAccess: requestAccess,
		installation: installation, passport: passport,
		agentCatalog: []agentCatalogRecord{
			{passport: passport, installation: installation},
			{passport: plannerPassport},
		},
		agentInstallRequests: make(map[string]agentInstallRecord), agentOffboardRequests: make(map[string]agentOffboardRecord), runtime: runtime,
		requests:       make(map[string][sha256.Size]byte),
		mentionResults: make(map[string]MentionResult),
		knownActors: map[im.ActorID]im.ActorRef{
			requester.ActorID(): requester, installation.AgentActor(): agentRef,
		},
		conversations:       map[im.ConversationID]*localConversation{parent.Ref().ConversationID(): parentRecord},
		conversationOrder:   []im.ConversationID{parent.Ref().ConversationID()},
		conversationCreates: make(map[string]createRecord), memberUpdates: make(map[string]memberUpdateRecord),
		tasks: make(map[string]TaskView), artifacts: make(map[string]ArtifactView), needsYou: make(map[string]NeedsYouView),
		cursorNamespaceHex: hex.EncodeToString(cursorNamespace[:]),
	}, nil
}

func (service *Service) Snapshot() Snapshot {
	if service == nil {
		return Snapshot{}
	}
	descriptor := modelruntime.Descriptor{}
	if service.runtime != nil {
		descriptor = service.runtime.Descriptor()
	}
	mode := "zero-network-fake"
	if descriptor.Mode == "model" {
		mode = "model-runtime"
	}
	service.mu.Lock()
	runtimeCalls := service.runtimeCalls
	service.mu.Unlock()
	return Snapshot{
		Mode: mode, NetworkCalls: runtimeCalls, AuthProvider: "auth.fake.clerk-shaped.v1",
		IMProvider:           "im.fake.rongcloud-shaped.v1",
		ParentConversationID: service.parent.Ref().ConversationID().String(),
		HumanActorID:         service.requester.ActorID().String(),
		AgentActorID:         service.installation.AgentActor().String(), AgentVersion: service.installation.Version().String(),
		AgentRuntime: descriptor,
	}
}

func (service *Service) Mention(
	ctx context.Context,
	bearerToken string,
	input MentionInput,
) (MentionResult, error) {
	if service == nil || ctx == nil || !validInstruction(input.Instruction) {
		return MentionResult{}, ErrInvalidInput
	}
	parentID := service.parent.Ref().ConversationID()
	if input.ConversationID != "" {
		parsedParentID, err := im.ParseConversationID(input.ConversationID)
		if err != nil {
			return MentionResult{}, ErrInvalidInput
		}
		parentID = parsedParentID
	}
	messageID, err := im.ParseMessageID(input.MessageID)
	if err != nil {
		return MentionResult{}, ErrInvalidInput
	}
	identity, err := service.authVerifier.Verify(ctx, auth.VerifyRequest{BearerToken: bearerToken})
	if err != nil || identity.ExternalRef.SubjectID() != LocalExternalSubject {
		return MentionResult{}, ErrUnauthenticated
	}
	instructionDigest := sha256.Sum256([]byte(input.Instruction))
	requestKey := parentID.String() + "\x00" + messageID.String()
	service.mu.Lock()
	parentRecord, parentExists := service.conversations[parentID]
	if !parentExists || parentRecord.snapshot.ConversationType() != im.ConversationGroup ||
		parentRecord.snapshot.Status() != im.ConversationActive || !service.canInvoke(parentRecord) {
		service.mu.Unlock()
		return MentionResult{}, ErrForbidden
	}
	// A previously installed Agent remains a member projection, but an expired or revoked
	// Trust Passport must stop new invocations immediately. This is an action-time check, not a
	// replacement for durable membership/capability resolution in production.
	if service.installation.IsZero() || service.installation.Status() != agentstore.InstallationActive ||
		!service.usableAgentPassport(service.passport) {
		service.mu.Unlock()
		return MentionResult{}, ErrForbidden
	}
	if _, ok := parentRecord.members[service.installation.AgentActor()]; !ok {
		service.mu.Unlock()
		return MentionResult{}, ErrForbidden
	}
	if existing, ok := service.requests[requestKey]; ok && existing != instructionDigest {
		service.mu.Unlock()
		return MentionResult{}, ErrConflict
	}
	if existingResult, ok := service.mentionResults[requestKey]; ok {
		existingResult.Replayed = true
		service.mu.Unlock()
		return existingResult, nil
	}
	service.requests[requestKey] = instructionDigest
	requestAccess := parentRecord.access[service.requester.ActorID()]
	parentSnapshot := parentRecord.snapshot
	service.mu.Unlock()
	thread, err := service.coordinator.Open(ctx, agentthread.MentionCommand{
		Parent: parentSnapshot, RequestingActor: service.requester,
		RequestingAccess: requestAccess, RootMessage: messageID,
		AgentInstallation: service.installation,
	})
	if err != nil {
		return MentionResult{}, err
	}
	replyEffectDigest := sha256.Sum256([]byte(messageID.String() + "\x00" + input.Instruction))
	replyMessageID, err := im.ParseMessageID("msg_agent_" + hex.EncodeToString(replyEffectDigest[:12]))
	if err != nil {
		return MentionResult{}, ErrInvalidInput
	}
	workspace, ok := parentSnapshot.WorkspaceID()
	if !ok {
		return MentionResult{}, ErrIntegrity
	}
	if service.runtime.Descriptor().Mode == "model" {
		service.mu.Lock()
		service.runtimeCalls++
		service.mu.Unlock()
	}
	runtimeResult, err := service.runtime.Generate(ctx, modelruntime.Request{
		TenantID: parentSnapshot.Ref().TenantID().String(), WorkspaceID: workspace.String(),
		ParentConversation: parentSnapshot.Ref().ConversationID().String(),
		ChildConversation:  thread.Plan().Child().Ref().ConversationID().String(),
		InvocationID:       thread.Plan().InvocationID().String(), AgentActorID: service.installation.AgentActor().String(),
		AgentVersion: service.installation.Version().String(), Instruction: input.Instruction,
	})
	if err != nil {
		return MentionResult{}, errors.Join(ErrRuntime, err)
	}
	if err := runtimeResult.Validate(); err != nil {
		return MentionResult{}, errors.Join(ErrRuntime, err)
	}
	replyText := runtimeResult.Text
	receipt, err := service.coordinator.SendAgentReply(
		ctx, thread, service.installation, replyMessageID, replyText,
		"demo/reply/"+hex.EncodeToString(replyEffectDigest[:16]),
	)
	if err != nil {
		return MentionResult{}, err
	}
	if err := service.materializeThread(thread, replyMessageID, replyText, receipt); err != nil {
		return MentionResult{}, err
	}
	workCard, err := agentthread.EncodeParentWorkCard(thread.Plan().ParentCard())
	if err != nil {
		return MentionResult{}, err
	}
	result := MentionResult{
		ParentConversationID: parentSnapshot.Ref().ConversationID().String(),
		ChildConversationID:  thread.Plan().Child().Ref().ConversationID().String(),
		InvocationID:         thread.Plan().InvocationID().String(), WorkCardExtInfo: workCard,
		AgentReply: AgentReplyView{
			ConversationID: thread.Plan().Child().Ref().ConversationID().String(),
			SenderActorID:  service.installation.AgentActor().String(), Text: replyText,
		},
		Replayed:       thread.Replayed() || receipt.Status == im.ProviderEffectReplayed,
		ProviderStatus: string(receipt.Status),
	}
	task, artifact, needsYou, err := service.materializeTaskOutcome(
		result.ParentConversationID, result.ChildConversationID, result.InvocationID, input.Instruction, replyText,
	)
	if err != nil {
		return MentionResult{}, err
	}
	result.TaskID, result.ArtifactID, result.NeedsYouID = task.ID, artifact.ID, needsYou.ID
	service.mu.Lock()
	if existingResult, exists := service.mentionResults[requestKey]; exists {
		existingResult.Replayed = true
		service.mu.Unlock()
		return existingResult, nil
	}
	service.mentionResults[requestKey] = result
	service.mu.Unlock()
	return result, nil
}

func buildPlatform(now time.Time) (
	im.TenantID,
	im.WorkspaceID,
	im.ConversationSnapshot,
	im.ActorRef,
	im.ConversationAccessSnapshot,
	agentstore.InstallationSnapshot,
	agentstore.TrustPassport,
	error,
) {
	tenant, err := im.ParseTenantID("ten_local_demo")
	if err != nil {
		return im.TenantID{}, im.WorkspaceID{}, im.ConversationSnapshot{}, im.ActorRef{}, im.ConversationAccessSnapshot{}, agentstore.InstallationSnapshot{}, agentstore.TrustPassport{}, err
	}
	workspace, err := im.ParseWorkspaceID("wsp_local_demo")
	if err != nil {
		return im.TenantID{}, im.WorkspaceID{}, im.ConversationSnapshot{}, im.ActorRef{}, im.ConversationAccessSnapshot{}, agentstore.InstallationSnapshot{}, agentstore.TrustPassport{}, err
	}
	parentID, _ := im.ParseConversationID("cnv_local_demo_parent")
	parentRef, _ := im.NewConversationRef(tenant, parentID)
	parent, err := im.NewConversationSnapshot(
		parentRef, &workspace, im.ConversationGroup, im.ConversationActive,
		im.ConversationID{}, im.MessageID{}, im.InvocationID{}, 1,
	)
	if err != nil {
		return im.TenantID{}, im.WorkspaceID{}, im.ConversationSnapshot{}, im.ActorRef{}, im.ConversationAccessSnapshot{}, agentstore.InstallationSnapshot{}, agentstore.TrustPassport{}, err
	}
	humanID, _ := im.ParseActorID("usr_local_demo")
	requester, _ := im.NewActorRef(tenant, humanID)
	access, err := im.NewConversationAccessSnapshot(
		parentRef, requester,
		[]im.ConversationPermission{
			im.ConversationPermissionRead, im.ConversationPermissionSendMessage,
			im.ConversationPermissionInvokeAgent,
		}, 1,
	)
	if err != nil {
		return im.TenantID{}, im.WorkspaceID{}, im.ConversationSnapshot{}, im.ActorRef{}, im.ConversationAccessSnapshot{}, agentstore.InstallationSnapshot{}, agentstore.TrustPassport{}, err
	}
	installation, passport, err := buildInstalledAgent(now, tenant, workspace)
	return tenant, workspace, parent, requester, access, installation, passport, err
}

func buildInstalledAgent(
	now time.Time,
	tenant im.TenantID,
	workspace im.WorkspaceID,
) (agentstore.InstallationSnapshot, agentstore.TrustPassport, error) {
	definitionID, _ := im.ParseAgentDefinitionID("agd_local_research")
	owner, _ := im.ParseHumanPrincipalID("hpr_local_demo")
	publisher, _ := agentstore.ParsePublisherID("pub_local_demo")
	definition, err := agentstore.NewDefinitionSnapshot(
		definitionID, tenant, owner, publisher, "v0版研究 Agent", "本地零网络 IM 验收 Agent。",
		agentstore.DefinitionActive, 1,
	)
	if err != nil {
		return agentstore.InstallationSnapshot{}, agentstore.TrustPassport{}, err
	}
	releaseID, _ := agentstore.ParseReleaseID("agr_local_research_100")
	version, _ := im.ParseAgentVersion("1.0.0")
	capability, _ := agentstore.ParseCapability("conversation.read")
	route, err := agentstore.NewDataRoute(
		"conversation.context", agentstore.DataInput, agentstore.DataConfidential,
		[]string{"local", "provider:rongcloud"}, 1,
	)
	if err != nil {
		return agentstore.InstallationSnapshot{}, agentstore.TrustPassport{}, err
	}
	publishedAt := now.Add(-2 * time.Hour)
	release, err := agentstore.NewReleaseSnapshot(
		releaseID, definitionID, version, agentstore.DigestBytes([]byte("local artifact")),
		agentstore.DigestBytes([]byte("local manifest")), agentstore.DigestBytes([]byte("local persona")),
		[]agentstore.Capability{capability}, nil, []agentstore.DataRoute{route},
		agentstore.IsolationProcess, agentstore.ReleasePublished, publishedAt, 1,
	)
	if err != nil {
		return agentstore.InstallationSnapshot{}, agentstore.TrustPassport{}, err
	}
	security, _ := agentstore.ParsePublisherID("pub_local_security")
	attestations := make([]agentstore.TrustAttestation, 0, 3)
	for _, claim := range []agentstore.AttestationClaim{
		agentstore.AttestationPublisherVerified,
		agentstore.AttestationSecurityReviewed,
		agentstore.AttestationDataRoutesReviewed,
	} {
		value, err := agentstore.NewTrustAttestation(
			security, claim, 1, agentstore.DigestBytes([]byte(claim)),
			now.Add(-time.Hour), now.Add(24*time.Hour),
		)
		if err != nil {
			return agentstore.InstallationSnapshot{}, agentstore.TrustPassport{}, err
		}
		attestations = append(attestations, value)
	}
	passport, err := agentstore.NewTrustPassport(
		definition, release, attestations, agentstore.PassportActive, 1,
	)
	if err != nil {
		return agentstore.InstallationSnapshot{}, agentstore.TrustPassport{}, err
	}
	installationID, _ := agentstore.ParseInstallationID("ins_local_research")
	agentID, _ := im.ParseActorID("agt_local_research")
	installation, err := agentstore.NewInstallationSnapshot(
		installationID, tenant, workspace, agentID, owner, passport,
		[]agentstore.Capability{capability}, []string{"conversation.context"},
		agentstore.InstallationActive, now, time.Time{}, 1,
	)
	return installation, passport, err
}

func buildAgentPassport(
	now time.Time,
	tenant im.TenantID,
	definitionIDValue string,
	releaseIDValue string,
	displayName string,
	summary string,
	capabilityValue string,
) (agentstore.TrustPassport, error) {
	definitionID, err := im.ParseAgentDefinitionID(definitionIDValue)
	if err != nil {
		return agentstore.TrustPassport{}, err
	}
	owner, err := im.ParseHumanPrincipalID("hpr_local_demo")
	if err != nil {
		return agentstore.TrustPassport{}, err
	}
	publisher, err := agentstore.ParsePublisherID("pub_local_demo")
	if err != nil {
		return agentstore.TrustPassport{}, err
	}
	definition, err := agentstore.NewDefinitionSnapshot(
		definitionID, tenant, owner, publisher, displayName, summary, agentstore.DefinitionActive, 1,
	)
	if err != nil {
		return agentstore.TrustPassport{}, err
	}
	releaseID, err := agentstore.ParseReleaseID(releaseIDValue)
	if err != nil {
		return agentstore.TrustPassport{}, err
	}
	version, err := im.ParseAgentVersion("1.0.0")
	if err != nil {
		return agentstore.TrustPassport{}, err
	}
	capability, err := agentstore.ParseCapability(capabilityValue)
	if err != nil {
		return agentstore.TrustPassport{}, err
	}
	route, err := agentstore.NewDataRoute(
		"conversation.context", agentstore.DataInput, agentstore.DataConfidential,
		[]string{"local", "provider:rongcloud"}, 1,
	)
	if err != nil {
		return agentstore.TrustPassport{}, err
	}
	publishedAt := now.Add(-2 * time.Hour)
	release, err := agentstore.NewReleaseSnapshot(
		releaseID, definitionID, version, agentstore.DigestBytes([]byte(definitionIDValue+" artifact")),
		agentstore.DigestBytes([]byte(definitionIDValue+" manifest")), agentstore.DigestBytes([]byte(definitionIDValue+" persona")),
		[]agentstore.Capability{capability}, nil, []agentstore.DataRoute{route},
		agentstore.IsolationProcess, agentstore.ReleasePublished, publishedAt, 1,
	)
	if err != nil {
		return agentstore.TrustPassport{}, err
	}
	security, err := agentstore.ParsePublisherID("pub_local_security")
	if err != nil {
		return agentstore.TrustPassport{}, err
	}
	attestations := make([]agentstore.TrustAttestation, 0, 3)
	for _, claim := range []agentstore.AttestationClaim{
		agentstore.AttestationPublisherVerified,
		agentstore.AttestationSecurityReviewed,
		agentstore.AttestationDataRoutesReviewed,
	} {
		attestation, attestationErr := agentstore.NewTrustAttestation(
			security, claim, 1, agentstore.DigestBytes([]byte(definitionIDValue+string(claim))),
			now.Add(-time.Hour), now.Add(24*time.Hour),
		)
		if attestationErr != nil {
			return agentstore.TrustPassport{}, attestationErr
		}
		attestations = append(attestations, attestation)
	}
	return agentstore.NewTrustPassport(definition, release, attestations, agentstore.PassportActive, 1)
}

func provisionHuman(ctx context.Context, provider *imfake.Provider, actor im.ActorID) error {
	projection, err := immetadata.NewUserProjection(
		im.SubjectHuman, actor, im.AgentDefinitionID{}, im.AgentVersion{},
	)
	if err != nil {
		return err
	}
	extInfo, err := immetadata.EncodeUserProjection(projection)
	if err != nil {
		return err
	}
	_, _, err = provider.ProvisionUser(ctx, im.ProviderUserProvision{
		Actor: actor, DisplayName: "本地验收用户", ExtInfo: extInfo, IdempotencyKey: "demo/user/human",
	})
	return err
}

func createParentGroup(
	ctx context.Context,
	provider *imfake.Provider,
	parent im.ConversationRef,
	human im.ActorID,
	agent im.ActorID,
) error {
	projection, err := immetadata.NewConversationProjection(
		im.ConversationGroup, parent.ConversationID(), im.ConversationID{}, im.MessageID{}, im.InvocationID{},
	)
	if err != nil {
		return err
	}
	extInfo, err := immetadata.EncodeConversationProjection(projection)
	if err != nil {
		return err
	}
	_, _, err = provider.CreateGroup(ctx, im.ProviderGroupCreate{
		Conversation: parent, ExtInfo: extInfo, MemberActors: []im.ActorID{human, agent},
		IdempotencyKey: "demo/group/parent",
	})
	return err
}

func validInstruction(value string) bool {
	if value == "" || len(value) > maxInstructionBytes || !utf8.ValidString(value) ||
		!norm.NFC.IsNormalString(value) || strings.TrimSpace(value) != value {
		return false
	}
	for _, character := range value {
		if unicode.IsControl(character) {
			return false
		}
	}
	return true
}
