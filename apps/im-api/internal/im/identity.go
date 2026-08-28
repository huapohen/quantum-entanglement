package im

import (
	"errors"
	"regexp"
	"strings"
)

const (
	maxPlatformIDBytes      = 128
	maxAgentVersionBytes    = 128
	maxExternalSubjectBytes = 256
	platformIDSeparator     = "_"
	humanActorIDPrefix      = "usr_"
	agentActorIDPrefix      = "agt_"
	systemActorIDPrefix     = "sys_"
	serviceActorIDPrefix    = "svc_"
	tenantIDPrefix          = "ten_"
	workspaceIDPrefix       = "wsp_"
	providerRealmIDPrefix   = "rlm_"
	agentDefinitionIDPrefix = "agd_"
	conversationIDPrefix    = "cnv_"
	messageIDPrefix         = "msg_"
	invocationIDPrefix      = "inv_"
	clerkExternalIDPrefix   = "user_"
)

var (
	ErrInvalidIdentity = errors.New("invalid IM identity")

	platformIDSuffixPattern = regexp.MustCompile(`^[A-Za-z0-9](?:[A-Za-z0-9_-]{0,122}[A-Za-z0-9])?$`)
	externalSubjectPattern  = regexp.MustCompile(`^[A-Za-z0-9](?:[A-Za-z0-9_.:@/-]{0,254}[A-Za-z0-9])?$`)
	semanticVersionPattern  = regexp.MustCompile(
		`^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(?:-((?:0|[1-9][0-9]*|[0-9]*[A-Za-z-][0-9A-Za-z-]*)(?:\.(?:0|[1-9][0-9]*|[0-9]*[A-Za-z-][0-9A-Za-z-]*))*))?(?:\+([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$`,
	)
)

type SubjectType string

const (
	SubjectHuman   SubjectType = "human"
	SubjectAgent   SubjectType = "agent"
	SubjectSystem  SubjectType = "system"
	SubjectService SubjectType = "service"
)

func (subjectType SubjectType) Valid() bool {
	switch subjectType {
	case SubjectHuman, SubjectAgent, SubjectSystem, SubjectService:
		return true
	default:
		return false
	}
}

func (subjectType SubjectType) actorIDPrefix() string {
	switch subjectType {
	case SubjectHuman:
		return humanActorIDPrefix
	case SubjectAgent:
		return agentActorIDPrefix
	case SubjectSystem:
		return systemActorIDPrefix
	case SubjectService:
		return serviceActorIDPrefix
	default:
		return ""
	}
}

type TenantID struct{ value string }

func ParseTenantID(value string) (TenantID, error) {
	if !validPrefixedPlatformID(value, tenantIDPrefix) {
		return TenantID{}, ErrInvalidIdentity
	}
	return TenantID{value: value}, nil
}

func (value TenantID) String() string { return value.value }
func (value TenantID) IsZero() bool   { return value.value == "" }

type WorkspaceID struct{ value string }

func ParseWorkspaceID(value string) (WorkspaceID, error) {
	if !validPrefixedPlatformID(value, workspaceIDPrefix) {
		return WorkspaceID{}, ErrInvalidIdentity
	}
	return WorkspaceID{value: value}, nil
}

func (value WorkspaceID) String() string { return value.value }
func (value WorkspaceID) IsZero() bool   { return value.value == "" }

// ProviderRealmID scopes a provider subject to one configured application/environment. It is not
// a secret, tenant membership, provider proof, or authorization grant.
type ProviderRealmID struct{ value string }

func ParseProviderRealmID(value string) (ProviderRealmID, error) {
	if !validPrefixedPlatformID(value, providerRealmIDPrefix) {
		return ProviderRealmID{}, ErrInvalidIdentity
	}
	return ProviderRealmID{value: value}, nil
}

func (value ProviderRealmID) String() string { return value.value }
func (value ProviderRealmID) IsZero() bool   { return value.value == "" }

type ActorID struct{ value string }

func ParseActorID(value string) (ActorID, error) {
	for _, prefix := range []string{
		humanActorIDPrefix,
		agentActorIDPrefix,
		systemActorIDPrefix,
		serviceActorIDPrefix,
	} {
		if validPrefixedPlatformID(value, prefix) {
			return ActorID{value: value}, nil
		}
	}
	return ActorID{}, ErrInvalidIdentity
}

func (value ActorID) String() string { return value.value }
func (value ActorID) IsZero() bool   { return value.value == "" }
func (value ActorID) SubjectType() (SubjectType, bool) {
	for _, subjectType := range []SubjectType{
		SubjectHuman,
		SubjectAgent,
		SubjectSystem,
		SubjectService,
	} {
		if strings.HasPrefix(value.value, subjectType.actorIDPrefix()) {
			return subjectType, true
		}
	}
	return "", false
}

type AgentDefinitionID struct{ value string }

func ParseAgentDefinitionID(value string) (AgentDefinitionID, error) {
	if !validPrefixedPlatformID(value, agentDefinitionIDPrefix) {
		return AgentDefinitionID{}, ErrInvalidIdentity
	}
	return AgentDefinitionID{value: value}, nil
}

func (value AgentDefinitionID) String() string { return value.value }
func (value AgentDefinitionID) IsZero() bool   { return value.value == "" }

// AgentVersion is a strict SemVer compatibility/display label. It is not an immutable release
// identity, artifact digest, signature, installation approval, or runtime authority.
type AgentVersion struct{ value string }

func ParseAgentVersion(value string) (AgentVersion, error) {
	if value == "" || len(value) > maxAgentVersionBytes || !semanticVersionPattern.MatchString(value) {
		return AgentVersion{}, ErrInvalidIdentity
	}
	return AgentVersion{value: value}, nil
}

func (value AgentVersion) String() string { return value.value }
func (value AgentVersion) IsZero() bool   { return value.value == "" }

type IdentityProvider string

const (
	IdentityProviderClerk     IdentityProvider = "clerk"
	IdentityProviderRongCloud IdentityProvider = "rongcloud"
)

func (provider IdentityProvider) Valid() bool {
	return provider == IdentityProviderClerk || provider == IdentityProviderRongCloud
}

// ExternalIdentityRef is realm-scoped provider mapping metadata, not provider proof or an
// authorization grant. Possessing a RongCloud user ID does not establish tenant membership or
// Actor authority; the persisted binding and platform membership must still be resolved.
type ExternalIdentityRef struct {
	provider  IdentityProvider
	realmID   ProviderRealmID
	subjectID string
}

func NewExternalIdentityRef(
	provider IdentityProvider,
	realmID ProviderRealmID,
	subjectID string,
) (ExternalIdentityRef, error) {
	if !provider.Valid() || realmID.IsZero() || !validExternalSubjectID(provider, subjectID) {
		return ExternalIdentityRef{}, ErrInvalidIdentity
	}
	return ExternalIdentityRef{provider: provider, realmID: realmID, subjectID: subjectID}, nil
}

func (reference ExternalIdentityRef) Provider() IdentityProvider { return reference.provider }
func (reference ExternalIdentityRef) RealmID() ProviderRealmID   { return reference.realmID }
func (reference ExternalIdentityRef) SubjectID() string          { return reference.subjectID }
func (reference ExternalIdentityRef) IsZero() bool {
	return reference.provider == "" && reference.realmID.IsZero() && reference.subjectID == ""
}

// ActorRef is the stable tenant-scoped visible business reference. It deliberately excludes
// snapshot revision, workload, delegation, credential, membership, and capability authority.
type ActorRef struct {
	tenantID TenantID
	actorID  ActorID
}

func NewActorRef(tenantID TenantID, actorID ActorID) (ActorRef, error) {
	if tenantID.IsZero() || actorID.IsZero() {
		return ActorRef{}, ErrInvalidIdentity
	}
	return ActorRef{tenantID: tenantID, actorID: actorID}, nil
}

func (reference ActorRef) TenantID() TenantID { return reference.tenantID }
func (reference ActorRef) ActorID() ActorID   { return reference.actorID }
func (reference ActorRef) IsZero() bool {
	return reference.tenantID.IsZero() && reference.actorID.IsZero()
}

// ActorSnapshot describes one immutable revision of a stable ActorRef. Prefix/type agreement is
// syntax validation only: authorization paths must resolve the persisted Actor and membership.
type ActorSnapshot struct {
	reference   ActorRef
	subjectType SubjectType
	revision    uint64
}

func NewActorSnapshot(
	reference ActorRef,
	subjectType SubjectType,
	revision uint64,
) (ActorSnapshot, error) {
	inferredType, ok := reference.actorID.SubjectType()
	if reference.IsZero() || !subjectType.Valid() || revision == 0 || !ok ||
		inferredType != subjectType {
		return ActorSnapshot{}, ErrInvalidIdentity
	}
	return ActorSnapshot{
		reference: reference, subjectType: subjectType, revision: revision,
	}, nil
}

func (snapshot ActorSnapshot) Ref() ActorRef            { return snapshot.reference }
func (snapshot ActorSnapshot) SubjectType() SubjectType { return snapshot.subjectType }
func (snapshot ActorSnapshot) Revision() uint64         { return snapshot.revision }
func (snapshot ActorSnapshot) IsZero() bool {
	return snapshot.reference.IsZero() && snapshot.subjectType == "" && snapshot.revision == 0
}

func validPrefixedPlatformID(value, prefix string) bool {
	if value == "" || len(value) > maxPlatformIDBytes || !strings.HasPrefix(value, prefix) ||
		!strings.HasSuffix(prefix, platformIDSeparator) {
		return false
	}
	return platformIDSuffixPattern.MatchString(strings.TrimPrefix(value, prefix))
}

func validExternalSubjectID(provider IdentityProvider, subjectID string) bool {
	if subjectID == "" || len(subjectID) > maxExternalSubjectBytes ||
		!externalSubjectPattern.MatchString(subjectID) {
		return false
	}
	switch provider {
	case IdentityProviderClerk:
		return strings.HasPrefix(subjectID, clerkExternalIDPrefix) &&
			platformIDSuffixPattern.MatchString(strings.TrimPrefix(subjectID, clerkExternalIDPrefix))
	case IdentityProviderRongCloud:
		_, err := ParseActorID(subjectID)
		return err == nil
	default:
		return false
	}
}
