package plugins

import (
	"context"
	"time"
)

type PluginID string
type PortID string
type CapabilityID string

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
	PluginID       PluginID
	Version        string
	ArtifactDigest string
	ProvenanceRef  string
	SBOMRef        string
	ApprovalRef    string
	Revoked        bool
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

type PluginConfig struct {
	Values     map[string]string
	SecretRefs map[string]string
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
