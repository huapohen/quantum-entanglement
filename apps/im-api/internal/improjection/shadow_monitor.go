package improjection

import (
	"context"
	"errors"
	"sync/atomic"

	"github.com/huapohen/quantum-entanglement/apps/im-api/internal/imstore"
)

var (
	ErrShadowMonitorInvalid = errors.New("invalid message shadow monitor")
	ErrShadowUnhealthy      = errors.New("message shadow mismatch is latched")
)

// ShadowMonitor owns process-local, identifier-free telemetry for the opt-in replay/materialized
// equality canary. A durable mismatch is sticky for the lifetime of the monitor so a process
// cannot pass readiness after it has observed divergent business state. Dependency and request
// failures are counted but do not latch: the primary database readiness probe owns availability.
type ShadowMonitor struct {
	runs       atomic.Uint64
	successes  atomic.Uint64
	mismatches atomic.Uint64
	failures   atomic.Uint64
	pages      atomic.Uint64
	messages   atomic.Uint64
	latched    atomic.Bool
}

// ShadowTelemetrySnapshot is safe to expose to an internal metrics adapter. It contains no
// tenant, workspace, conversation, cursor, message, provider, credential or raw error value.
type ShadowTelemetrySnapshot struct {
	Runs             uint64
	Successes        uint64
	Mismatches       uint64
	Failures         uint64
	ComparedPages    uint64
	ComparedMessages uint64
	MismatchLatched  bool
}

func NewShadowMonitor() *ShadowMonitor { return &ShadowMonitor{} }

// Compare executes one complete bounded shadow run and records its outcome exactly once.
func (monitor *ShadowMonitor) Compare(
	ctx context.Context,
	replay imstore.MessageReadRepository,
	materialized imstore.MessageReadRepository,
	query imstore.MessageReadPageQuery,
) (ShadowComparison, error) {
	if monitor == nil {
		return ShadowComparison{}, ErrShadowMonitorInvalid
	}
	comparison, err := CompareMessageReaders(ctx, replay, materialized, query)
	monitor.observe(comparison, err)
	return comparison, err
}

// Ready fails closed after the first durable equality mismatch. The latch is intentionally not
// resettable: an operator must preserve evidence, replace the process and complete reconciliation.
func (monitor *ShadowMonitor) Ready(ctx context.Context) error {
	if monitor == nil || ctx == nil || ctx.Err() != nil {
		return ErrShadowMonitorInvalid
	}
	if monitor.latched.Load() {
		return ErrShadowUnhealthy
	}
	return nil
}

func (monitor *ShadowMonitor) Snapshot() ShadowTelemetrySnapshot {
	if monitor == nil {
		return ShadowTelemetrySnapshot{}
	}
	return ShadowTelemetrySnapshot{
		Runs: monitor.runs.Load(), Successes: monitor.successes.Load(),
		Mismatches: monitor.mismatches.Load(), Failures: monitor.failures.Load(),
		ComparedPages: monitor.pages.Load(), ComparedMessages: monitor.messages.Load(),
		MismatchLatched: monitor.latched.Load(),
	}
}

func (monitor *ShadowMonitor) observe(comparison ShadowComparison, err error) {
	monitor.runs.Add(1)
	if err == nil {
		monitor.successes.Add(1)
		monitor.pages.Add(comparison.Pages)
		monitor.messages.Add(comparison.Messages)
		return
	}
	if errors.Is(err, ErrShadowMismatch) {
		monitor.mismatches.Add(1)
		monitor.latched.Store(true)
		return
	}
	monitor.failures.Add(1)
}
