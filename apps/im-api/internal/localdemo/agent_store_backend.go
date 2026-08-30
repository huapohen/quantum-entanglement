package localdemo

import (
	"bytes"
	"context"
	"errors"
	"strings"

	"github.com/huapohen/quantum-entanglement/apps/im-api/internal/agentstore"
	"github.com/huapohen/quantum-entanglement/apps/im-api/internal/im"
	store "github.com/huapohen/quantum-entanglement/apps/im-api/internal/imstore"
)

// AgentStoreRecord is the immutable catalog projection that a local demo backend must seed.
// The backend receives the reviewed Passport and optional tenant installation together, so a
// durable implementation cannot accidentally persist an installation without its catalog chain.
type AgentStoreRecord struct {
	Passport     agentstore.TrustPassport
	Installation agentstore.InstallationSnapshot
}

// AgentStoreBackend is the persistence seam for local-demo Agent Store actions. The local demo
// remains usable with a nil backend (deterministic in-memory mode), while a production-like
// composition can inject a tenant-bound PostgreSQL implementation. Provider effects are still
// executed by the demo's fake provider; the backend only commits the control-plane snapshots.
// This boundary is intentionally explicit until provider effect outbox/reconciliation is wired
// into the same runtime composition.
type AgentStoreBackend interface {
	SyncCatalog(context.Context, im.TenantID, []AgentStoreRecord) error
	CommitInstall(context.Context, im.TenantID, string, agentstore.SHA256Digest, agentstore.InstallationSnapshot, []agentstore.InstallationSnapshot) error
	CommitOffboard(context.Context, im.TenantID, string, agentstore.SHA256Digest, agentstore.InstallationSnapshot, agentstore.InstallationSnapshot) error
}

// PostgresAgentStoreBackend adapts the existing tenant-bound Unit of Work to the local-demo
// lifecycle seam. Every mutation uses the shared serializable command receipt path and performs
// repository CAS writes inside one transaction. It does not accept a raw pool, preserving the
// runtime-pool attestation boundary enforced by postgres/imstore.NewUnitOfWork.
type PostgresAgentStoreBackend struct {
	persistence store.TenantUnitOfWork
}

func NewPostgresAgentStoreBackend(persistence store.TenantUnitOfWork) (*PostgresAgentStoreBackend, error) {
	if persistence == nil {
		return nil, store.ErrInvalidRequest
	}
	return &PostgresAgentStoreBackend{persistence: persistence}, nil
}

func (backend *PostgresAgentStoreBackend) SyncCatalog(
	ctx context.Context,
	tenantID im.TenantID,
	records []AgentStoreRecord,
) error {
	if backend == nil || backend.persistence == nil || ctx == nil || tenantID.IsZero() || len(records) == 0 {
		return store.ErrInvalidRequest
	}
	digest := catalogSyncDigest(records)
	command, err := store.NewAgentStoreCommand("agent.catalog.sync", "sync-"+digest.Hex(), storeDigest(digest))
	if err != nil {
		return err
	}
	_, err = backend.persistence.Execute(ctx, tenantID, command, func(ctx context.Context, repositories store.TenantRepositories) (store.SHA256Digest, error) {
		for _, record := range records {
			if record.Passport.IsZero() || record.Passport.Definition().TenantID() != tenantID {
				return store.DigestBytes(nil), store.ErrInvalidRequest
			}
			if err := ensureDefinition(ctx, repositories.AgentStore(), record.Passport.Definition()); err != nil {
				return store.DigestBytes(nil), err
			}
			if err := ensureRelease(ctx, repositories.AgentStore(), record.Passport.Release()); err != nil {
				return store.DigestBytes(nil), err
			}
			if err := ensurePassport(ctx, repositories.AgentStore(), record.Passport); err != nil {
				return store.DigestBytes(nil), err
			}
			if !record.Installation.IsZero() {
				if record.Installation.TenantID() != tenantID {
					return store.DigestBytes(nil), store.ErrInvalidRequest
				}
				if err := ensureInstallation(ctx, repositories.AgentStore(), record.Installation); err != nil {
					return store.DigestBytes(nil), err
				}
			}
		}
		return storeDigest(digest), nil
	})
	return err
}

func (backend *PostgresAgentStoreBackend) CommitInstall(
	ctx context.Context,
	tenantID im.TenantID,
	idempotencyKey string,
	digest agentstore.SHA256Digest,
	target agentstore.InstallationSnapshot,
	retired []agentstore.InstallationSnapshot,
) error {
	if backend == nil || backend.persistence == nil || ctx == nil || tenantID.IsZero() || target.IsZero() || target.TenantID() != tenantID || digest.IsZero() {
		return store.ErrInvalidRequest
	}
	command, err := store.NewAgentStoreCommand("agent.install", backendCommandKey("install", idempotencyKey, digest), storeDigest(digest))
	if err != nil {
		return err
	}
	_, err = backend.persistence.Execute(ctx, tenantID, command, func(ctx context.Context, repositories store.TenantRepositories) (store.SHA256Digest, error) {
		catalog := repositories.AgentStore()
		if err := installCAS(ctx, catalog, target); err != nil {
			return store.DigestBytes(nil), err
		}
		for _, next := range retired {
			if next.IsZero() || next.TenantID() != tenantID {
				return store.DigestBytes(nil), store.ErrInvalidRequest
			}
			if err := transitionCAS(ctx, catalog, next); err != nil {
				return store.DigestBytes(nil), err
			}
		}
		return storeDigest(digest), nil
	})
	return err
}

func (backend *PostgresAgentStoreBackend) CommitOffboard(
	ctx context.Context,
	tenantID im.TenantID,
	idempotencyKey string,
	digest agentstore.SHA256Digest,
	current agentstore.InstallationSnapshot,
	next agentstore.InstallationSnapshot,
) error {
	if backend == nil || backend.persistence == nil || ctx == nil || tenantID.IsZero() || current.IsZero() || next.IsZero() || current.TenantID() != tenantID || next.TenantID() != tenantID || digest.IsZero() {
		return store.ErrInvalidRequest
	}
	if current.ID() != next.ID() || next.Revision() != current.Revision()+1 {
		return store.ErrRevisionConflict
	}
	command, err := store.NewAgentStoreCommand("agent.offboard", backendCommandKey("offboard", idempotencyKey, digest), storeDigest(digest))
	if err != nil {
		return err
	}
	_, err = backend.persistence.Execute(ctx, tenantID, command, func(ctx context.Context, repositories store.TenantRepositories) (store.SHA256Digest, error) {
		catalog := repositories.AgentStore()
		stored, err := catalog.CurrentInstallation(ctx, current.ID())
		if err != nil {
			return store.DigestBytes(nil), err
		}
		if stored.Revision() != current.Revision() {
			return store.DigestBytes(nil), agentstore.ErrInstallationConflict
		}
		if _, err := catalog.CompareAndSwapInstallation(ctx, current.Revision(), next); err != nil {
			return store.DigestBytes(nil), err
		}
		return storeDigest(digest), nil
	})
	return err
}

func storeDigest(value agentstore.SHA256Digest) store.SHA256Digest {
	var converted store.SHA256Digest
	copy(converted[:], value[:])
	return converted
}

func backendCommandKey(prefix, _ string, digest agentstore.SHA256Digest) string {
	// Local demo keys historically allowed slashes. The durable command grammar deliberately does
	// not, so preserve retry identity with a canonical digest-backed key and avoid copying raw data.
	return prefix + "-" + digest.Hex()
}

func catalogSyncDigest(records []AgentStoreRecord) agentstore.SHA256Digest {
	parts := make([]string, 0, len(records)*4)
	for _, record := range records {
		if record.Passport.IsZero() {
			parts = append(parts, "zero")
			continue
		}
		parts = append(parts,
			record.Passport.Definition().ID().String(),
			record.Passport.Release().ID().String(),
			record.Passport.Release().ArtifactDigest().Hex(),
			string(record.Passport.Status()),
		)
	}
	return agentstore.DigestBytes([]byte(strings.Join(parts, "\x00")))
}

func ensureDefinition(ctx context.Context, repository agentstore.Repository, next agentstore.DefinitionSnapshot) error {
	current, err := repository.CurrentDefinition(ctx, next.ID())
	if err == nil {
		if !sameDefinition(current, next) {
			return agentstore.ErrDefinitionConflict
		}
		return nil
	}
	if !errors.Is(err, agentstore.ErrNotFound) {
		return err
	}
	_, err = repository.CompareAndSwapDefinition(ctx, 0, next)
	return err
}

func ensureRelease(ctx context.Context, repository agentstore.Repository, next agentstore.ReleaseSnapshot) error {
	current, err := repository.CurrentRelease(ctx, next.ID())
	if err == nil {
		if !sameRelease(current, next) {
			return agentstore.ErrReleaseConflict
		}
		return nil
	}
	if !errors.Is(err, agentstore.ErrNotFound) {
		return err
	}
	_, err = repository.CompareAndSwapRelease(ctx, 0, next)
	return err
}

func ensurePassport(ctx context.Context, repository agentstore.Repository, next agentstore.TrustPassport) error {
	current, err := repository.CurrentPassport(ctx, next.Release().ID())
	if err == nil {
		if !samePassport(current, next) {
			return agentstore.ErrPassportConflict
		}
		return nil
	}
	if !errors.Is(err, agentstore.ErrNotFound) {
		return err
	}
	_, err = repository.CompareAndSwapPassport(ctx, 0, next)
	return err
}

func ensureInstallation(ctx context.Context, repository agentstore.Repository, next agentstore.InstallationSnapshot) error {
	current, err := repository.CurrentInstallation(ctx, next.ID())
	if err == nil {
		if !sameInstallation(current, next) {
			return agentstore.ErrInstallationConflict
		}
		return nil
	}
	if !errors.Is(err, agentstore.ErrNotFound) {
		return err
	}
	_, err = repository.CompareAndSwapInstallation(ctx, 0, next)
	return err
}

func sameDefinition(left, right agentstore.DefinitionSnapshot) bool {
	leftEncoded, leftErr := agentstore.EncodeDefinition(left)
	rightEncoded, rightErr := agentstore.EncodeDefinition(right)
	return leftErr == nil && rightErr == nil && bytes.Equal(leftEncoded, rightEncoded)
}

func sameRelease(left, right agentstore.ReleaseSnapshot) bool {
	leftEncoded, leftErr := agentstore.EncodeRelease(left)
	rightEncoded, rightErr := agentstore.EncodeRelease(right)
	return leftErr == nil && rightErr == nil && bytes.Equal(leftEncoded, rightEncoded)
}

func samePassport(left, right agentstore.TrustPassport) bool {
	leftEncoded, leftErr := agentstore.EncodeTrustPassport(left)
	rightEncoded, rightErr := agentstore.EncodeTrustPassport(right)
	return leftErr == nil && rightErr == nil && bytes.Equal(leftEncoded, rightEncoded)
}

func sameInstallation(left, right agentstore.InstallationSnapshot) bool {
	leftEncoded, leftErr := agentstore.EncodeInstallation(left)
	rightEncoded, rightErr := agentstore.EncodeInstallation(right)
	return leftErr == nil && rightErr == nil && bytes.Equal(leftEncoded, rightEncoded)
}

func installCAS(ctx context.Context, repository agentstore.Repository, target agentstore.InstallationSnapshot) error {
	current, err := repository.CurrentInstallation(ctx, target.ID())
	if errors.Is(err, agentstore.ErrNotFound) {
		_, err = repository.CompareAndSwapInstallation(ctx, 0, target)
		return err
	}
	if err != nil {
		return err
	}
	if current.TenantID() != target.TenantID() || current.DefinitionID() != target.DefinitionID() {
		return agentstore.ErrIntegrity
	}
	if current.Revision() == target.Revision() && current.Status() == target.Status() {
		return nil
	}
	return agentstore.ErrInstallationConflict
}

func transitionCAS(ctx context.Context, repository agentstore.Repository, next agentstore.InstallationSnapshot) error {
	current, err := repository.CurrentInstallation(ctx, next.ID())
	if err != nil {
		return err
	}
	if current.Revision() == next.Revision() && current.Status() == next.Status() {
		return nil
	}
	if current.Revision()+1 != next.Revision() {
		return agentstore.ErrInstallationConflict
	}
	_, err = repository.CompareAndSwapInstallation(ctx, current.Revision(), next)
	return err
}
