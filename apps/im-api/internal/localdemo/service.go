// Package localdemo composes the native IM contracts into a deterministic loopback-only demo.
// It performs no network calls and contains no production credential or provider SDK.
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
	"golang.org/x/text/unicode/norm"
)

var (
	ErrInvalidInput    = errors.New("invalid local IM demo input")
	ErrUnauthenticated = errors.New("local IM demo authentication failed")
	ErrConflict        = errors.New("local IM demo message identity conflict")
)

const (
	LocalBearerToken    = "demo.local.signature"
	maxInstructionBytes = 4096
)

type MentionInput struct {
	MessageID   string `json:"messageId"`
	Instruction string `json:"instruction"`
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
	WorkCardExtInfo      string         `json:"workCardExtInfo"`
	AgentReply           AgentReplyView `json:"agentReply"`
	Replayed             bool           `json:"replayed"`
	ProviderStatus       string         `json:"providerStatus"`
}

type Snapshot struct {
	Mode                 string `json:"mode"`
	NetworkCalls         int    `json:"networkCalls"`
	AuthProvider         string `json:"authProvider"`
	IMProvider           string `json:"imProvider"`
	ParentConversationID string `json:"parentConversationId"`
	HumanActorID         string `json:"humanActorId"`
	AgentActorID         string `json:"agentActorId"`
	AgentVersion         string `json:"agentVersion"`
}

type Service struct {
	mu            sync.Mutex
	authVerifier  auth.Verifier
	provider      *imfake.Provider
	coordinator   *agentthread.LocalCoordinator
	parent        im.ConversationSnapshot
	requester     im.ActorRef
	requestAccess im.ConversationAccessSnapshot
	installation  agentstore.InstallationSnapshot
	requests      map[im.MessageID][sha256.Size]byte
}

func New() (*Service, error) {
	now := time.Now().UTC()
	tenant, workspace, parent, requester, requestAccess, installation, passport, err := buildPlatform(now)
	if err != nil {
		return nil, err
	}
	realm, err := im.ParseProviderRealmID("rlm_local_demo")
	if err != nil {
		return nil, err
	}
	principal := installation.InstalledBy()
	verifier, err := authfake.New(authfake.Options{
		Realm: realm, Issuer: "clerk.local-demo", Audience: "wanwork-local-demo",
		Now: func() time.Time { return time.Now().UTC() },
		Tokens: map[string]authfake.TokenFixture{
			LocalBearerToken: {
				ExternalSubject: "user_local_demo", PrincipalID: principal, SessionID: "sess_local_demo",
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
	_ = tenant
	_ = workspace
	return &Service{
		authVerifier: verifier, provider: provider, coordinator: coordinator,
		parent: parent, requester: requester, requestAccess: requestAccess,
		installation: installation, requests: make(map[im.MessageID][sha256.Size]byte),
	}, nil
}

func (service *Service) Snapshot() Snapshot {
	if service == nil {
		return Snapshot{}
	}
	return Snapshot{
		Mode: "zero-network-fake", NetworkCalls: 0, AuthProvider: "auth.fake.clerk-shaped.v1",
		IMProvider:           "im.fake.rongcloud-shaped.v1",
		ParentConversationID: service.parent.Ref().ConversationID().String(),
		HumanActorID:         service.requester.ActorID().String(),
		AgentActorID:         service.installation.AgentActor().String(), AgentVersion: service.installation.Version().String(),
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
	messageID, err := im.ParseMessageID(input.MessageID)
	if err != nil {
		return MentionResult{}, ErrInvalidInput
	}
	identity, err := service.authVerifier.Verify(ctx, auth.VerifyRequest{BearerToken: bearerToken})
	if err != nil || identity.PrincipalID != service.installation.InstalledBy() {
		return MentionResult{}, ErrUnauthenticated
	}
	instructionDigest := sha256.Sum256([]byte(input.Instruction))
	service.mu.Lock()
	if existing, ok := service.requests[messageID]; ok && existing != instructionDigest {
		service.mu.Unlock()
		return MentionResult{}, ErrConflict
	}
	service.requests[messageID] = instructionDigest
	service.mu.Unlock()
	thread, err := service.coordinator.Open(ctx, agentthread.MentionCommand{
		Parent: service.parent, RequestingActor: service.requester,
		RequestingAccess: service.requestAccess, RootMessage: messageID,
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
	replyText := "v0版研究 Agent 已在独立子群处理：" + input.Instruction
	receipt, err := service.coordinator.SendAgentReply(
		ctx, thread, service.installation, replyMessageID, replyText,
		"demo/reply/"+hex.EncodeToString(replyEffectDigest[:16]),
	)
	if err != nil {
		return MentionResult{}, err
	}
	workCard, err := agentthread.EncodeParentWorkCard(thread.Plan().ParentCard())
	if err != nil {
		return MentionResult{}, err
	}
	return MentionResult{
		ParentConversationID: service.parent.Ref().ConversationID().String(),
		ChildConversationID:  thread.Plan().Child().Ref().ConversationID().String(),
		InvocationID:         thread.Plan().InvocationID().String(), WorkCardExtInfo: workCard,
		AgentReply: AgentReplyView{
			ConversationID: thread.Plan().Child().Ref().ConversationID().String(),
			SenderActorID:  service.installation.AgentActor().String(), Text: replyText,
		},
		Replayed:       thread.Replayed() || receipt.Status == im.ProviderEffectReplayed,
		ProviderStatus: string(receipt.Status),
	}, nil
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
