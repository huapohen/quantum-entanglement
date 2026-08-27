package plugins

import (
	"context"
	"time"
)

type PluginID string
type PortID string
type CapabilityID string

type SecretClaimReference struct {
	ClaimDigest   string
	ClaimRevision uint64
}

// SecretBindingView is identity and audit metadata, not a bearer capability. Possessing this
// value never authorizes secret resolution; a future action-time scoped resolver is a separate API.
type SecretBindingView struct {
	BrokerID               string
	BrokerDefinitionDigest string
	ClaimDigest            string
	ClaimRevision          uint64
	BindingFingerprint     string
	BrokerPolicyRevision   uint64
	ScopeDigest            string
}

type SecretClaimRequest struct {
	IdempotencyKey       string
	TenantID             string
	RowID                RowID
	PluginID             PluginID
	PluginVersion        string
	ArtifactDigest       string
	ManifestDigest       string
	AdmissionRevision    uint64
	ConfigSchemaDigest   string
	LogicalName          string
	BrokerID             string
	Purpose              string
	Audience             string
	PresentedReferenceID string
}

type SecretReferenceAdmissionBroker interface {
	ValidateReference(SecretClaimRequest) error
}

type SecretReferenceAdmissionBrokerFunc func(SecretClaimRequest) error

func (function SecretReferenceAdmissionBrokerFunc) ValidateReference(request SecretClaimRequest) error {
	return function(request)
}

type SecretBrokerDefinition struct {
	SchemaVersion        uint32
	ID                   string
	Version              string
	ImplementationDigest string
	PolicyRevision       uint64
	SupportedPurposes    []string
}

const HostAPIV1 = "wanwork.plugin-host/v1"

// Manifest is a plugin claim. Trust, provenance, and organization approval are deliberately
// stored in the separate host-owned PackageRecord.
type Manifest struct {
	ID                 PluginID
	Version            string
	HostAPI            string
	Provides           []PortID
	Requires           []PortRequirement
	Capabilities       []CapabilityID
	Egress             []string
	SecretRefNames     []string
	ConfigSchemaDigest string
	Timeouts           LifecycleTimeouts
}

// LifecycleTimeouts become context deadlines for cooperative in-process callbacks. They are
// not forced termination limits; hostile code requires an isolated supervisor boundary.
type LifecycleTimeouts struct {
	Start time.Duration
	Ready time.Duration
	Drain time.Duration
	Stop  time.Duration
}

type PortRequirement struct {
	Port       PortID
	ProviderID PluginID
}

type PackageRecord struct {
	PluginID               PluginID
	Version                string
	ArtifactDigest         string
	ApprovedManifestDigest string
	AdmissionRevision      uint64
	ProvenanceRef          string
	SBOMRef                string
	ApprovalRef            string
	Revoked                bool
}

type PortBinding struct {
	ConsumerID PluginID
	Port       PortID
	ProviderID PluginID
}

type Plan struct {
	Order    []PluginID
	Bindings []PortBinding
}

type ConfigurationInput struct {
	Values       map[string]string
	SecretClaims map[string]SecretClaimReference
}

type PluginConfig struct {
	Values         map[string]string
	SecretBindings map[string]SecretBindingView
}

type ConfigValueKind string

const ConfigValueEnum ConfigValueKind = "enum"

type ConfigValueField struct {
	Name       string
	Kind       ConfigValueKind
	Required   bool
	HasDefault bool
	Default    string
	Enum       []string
}

type ConfigSecretField struct {
	Name           string
	Required       bool
	Purpose        string
	Audience       string
	AllowedBrokers []string
}

// ConfigSchemaDefinition is declarative and host-owned. W1 deliberately supports only bounded
// enums for ordinary canonical-public values; arbitrary free-form strings fail closed.
type ConfigSchemaDefinition struct {
	SchemaVersion uint32
	ID            string
	ValueFields   []ConfigValueField
	SecretFields  []ConfigSecretField
}

type Factory interface {
	Manifest() Manifest
	Configure(PluginConfig) (Instance, error)
}

type Instance interface {
	Start(context.Context, Effects) error
	Ready(context.Context) error
	Drain(context.Context) error
	Stop(context.Context) error
}

type Effects interface {
	Defer(label string, cleanup func(context.Context) error) error
}
