package localdemo

import "context"

// AgentStorePage is the authenticated, read-only catalog projection used by the local Web
// client. It deliberately exposes reviewed declarations and the current installation decision
// separately; neither catalog metadata nor a granted capability is a runtime bearer credential.
type AgentStorePage struct {
	Agents []AgentStoreView `json:"agents"`
}

type AgentStoreView struct {
	DefinitionID          string               `json:"definitionId"`
	ReleaseID             string               `json:"releaseId"`
	InstallationID        string               `json:"installationId"`
	Name                  string               `json:"name"`
	Summary               string               `json:"summary"`
	Version               string               `json:"version"`
	DefinitionStatus      string               `json:"definitionStatus"`
	ReleaseStatus         string               `json:"releaseStatus"`
	PassportStatus        string               `json:"passportStatus"`
	InstallationStatus    string               `json:"installationStatus"`
	AgentActorID          string               `json:"agentActorId"`
	Isolation             string               `json:"isolation"`
	RequestedCapabilities []string             `json:"requestedCapabilities"`
	GrantedCapabilities   []string             `json:"grantedCapabilities"`
	DataRoutes            []AgentDataRouteView `json:"dataRoutes"`
	Attestations          []string             `json:"attestations"`
}

type AgentDataRouteView struct {
	Name           string   `json:"name"`
	Direction      string   `json:"direction"`
	Classification string   `json:"classification"`
	Destinations   []string `json:"destinations"`
	RetentionDays  uint16   `json:"retentionDays"`
}

// ListAgents returns the catalog and installation projection for the authenticated local tenant.
// The local composition contains one pre-installed, fully attested v0版 Agent; production will
// replace this in-memory read with a tenant-bound repository and action-time resolver.
func (service *Service) ListAgents(ctx context.Context, bearerToken string) (AgentStorePage, error) {
	if service == nil || ctx == nil {
		return AgentStorePage{}, ErrInvalidInput
	}
	if err := service.verifyRequester(ctx, bearerToken); err != nil {
		return AgentStorePage{}, err
	}

	service.mu.Lock()
	defer service.mu.Unlock()
	if service.passport.IsZero() || service.installation.IsZero() {
		return AgentStorePage{}, ErrIntegrity
	}
	definition := service.passport.Definition()
	release := service.passport.Release()
	routes := release.DataRoutes()
	dataRoutes := make([]AgentDataRouteView, 0, len(routes))
	for _, route := range routes {
		dataRoutes = append(dataRoutes, AgentDataRouteView{
			Name: route.Name(), Direction: string(route.Direction()),
			Classification: string(route.Classification()), Destinations: route.Destinations(),
			RetentionDays: route.RetentionDays(),
		})
	}
	attestations := service.passport.Attestations()
	claims := make([]string, 0, len(attestations))
	for _, attestation := range attestations {
		claims = append(claims, string(attestation.Claim()))
	}
	capabilities := release.RequestedCapabilities()
	requested := make([]string, 0, len(capabilities))
	for _, capability := range capabilities {
		requested = append(requested, string(capability))
	}
	grantedCapabilities := service.installation.GrantedCapabilities()
	granted := make([]string, 0, len(grantedCapabilities))
	for _, capability := range grantedCapabilities {
		granted = append(granted, string(capability))
	}
	return AgentStorePage{Agents: []AgentStoreView{{
		DefinitionID: definition.ID().String(), ReleaseID: release.ID().String(),
		InstallationID: service.installation.ID().String(), Name: definition.DisplayName(),
		Summary: definition.Summary(), Version: release.Version().String(),
		DefinitionStatus: string(definition.Status()), ReleaseStatus: string(release.Status()),
		PassportStatus:     string(service.passport.Status()),
		InstallationStatus: string(service.installation.Status()),
		AgentActorID:       service.installation.AgentActor().String(), Isolation: string(release.Isolation()),
		RequestedCapabilities: requested, GrantedCapabilities: granted,
		DataRoutes: dataRoutes, Attestations: claims,
	}}}, nil
}
