package plugins

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
