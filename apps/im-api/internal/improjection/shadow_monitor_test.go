package improjection

import (
	"context"
	"errors"
	"sync"
	"testing"

	"github.com/huapohen/quantum-entanglement/apps/im-api/internal/im"
	"github.com/huapohen/quantum-entanglement/apps/im-api/internal/imstore"
)

func TestShadowMonitorCountsOutcomesAndLatchesOnlyMismatch(t *testing.T) {
	monitor := NewShadowMonitor()
	monitor.observe(ShadowComparison{Pages: 2, Messages: 7}, nil)
	monitor.observe(ShadowComparison{}, imstore.ErrStoreUnavailable)
	if err := monitor.Ready(context.Background()); err != nil {
		t.Fatalf("availability failure latched readiness: %v", err)
	}
	monitor.observe(ShadowComparison{}, errors.Join(errors.New("detail"), ErrShadowMismatch))
	monitor.observe(ShadowComparison{Pages: 9, Messages: 9}, nil)
	snapshot := monitor.Snapshot()
	if snapshot.Runs != 4 || snapshot.Successes != 2 || snapshot.Mismatches != 1 ||
		snapshot.Failures != 1 || snapshot.ComparedPages != 11 ||
		snapshot.ComparedMessages != 16 || !snapshot.MismatchLatched {
		t.Fatalf("shadow telemetry=%#v", snapshot)
	}
	if err := monitor.Ready(context.Background()); !errors.Is(err, ErrShadowUnhealthy) {
		t.Fatalf("latched readiness error=%v", err)
	}
}

func TestShadowMonitorCompareRecordsEqualAndMismatchRuns(t *testing.T) {
	reference := shadowMonitorConversation(t)
	query := imstore.MessageReadPageQuery{
		Conversation: reference, Limit: 10, ConversationRevision: 1, AccessRevision: 1,
	}
	equalPage := imstore.MessageReadPage{Conversation: reference, ConversationRevision: 1}
	monitor := NewShadowMonitor()
	comparison, err := monitor.Compare(
		t.Context(), &shadowPageReader{page: equalPage}, &shadowPageReader{page: equalPage}, query,
	)
	if err != nil || comparison.Pages != 1 || comparison.Messages != 0 {
		t.Fatalf("equal comparison=%#v err=%v", comparison, err)
	}
	driftedPage := equalPage
	driftedPage.ConversationRevision = 2
	if _, err := monitor.Compare(
		t.Context(), &shadowPageReader{page: equalPage}, &shadowPageReader{page: driftedPage}, query,
	); !errors.Is(err, ErrShadowMismatch) {
		t.Fatalf("mismatch error=%v", err)
	}
	snapshot := monitor.Snapshot()
	if snapshot.Runs != 2 || snapshot.Successes != 1 || snapshot.Mismatches != 1 ||
		snapshot.Failures != 0 || snapshot.ComparedPages != 1 || !snapshot.MismatchLatched {
		t.Fatalf("comparison telemetry=%#v", snapshot)
	}
}

func TestShadowMonitorTelemetryIsConcurrentAndIdentifierFree(t *testing.T) {
	monitor := NewShadowMonitor()
	var group sync.WaitGroup
	for index := 0; index < 90; index++ {
		group.Add(1)
		go func(index int) {
			defer group.Done()
			switch index % 3 {
			case 0:
				monitor.observe(ShadowComparison{Pages: 1, Messages: 2}, nil)
			case 1:
				monitor.observe(ShadowComparison{}, ErrShadowMismatch)
			default:
				monitor.observe(ShadowComparison{}, imstore.ErrStoreUnavailable)
			}
		}(index)
	}
	group.Wait()
	snapshot := monitor.Snapshot()
	if snapshot.Runs != 90 || snapshot.Successes != 30 || snapshot.Mismatches != 30 ||
		snapshot.Failures != 30 || snapshot.ComparedPages != 30 ||
		snapshot.ComparedMessages != 60 || !snapshot.MismatchLatched {
		t.Fatalf("concurrent telemetry=%#v", snapshot)
	}
}

func TestShadowMonitorRejectsNilAndCancelledReadiness(t *testing.T) {
	var monitor *ShadowMonitor
	if _, err := monitor.Compare(context.Background(), nil, nil, imstore.MessageReadPageQuery{}); !errors.Is(err, ErrShadowMonitorInvalid) {
		t.Fatalf("nil compare error=%v", err)
	}
	if err := monitor.Ready(context.Background()); !errors.Is(err, ErrShadowMonitorInvalid) {
		t.Fatalf("nil readiness error=%v", err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if err := NewShadowMonitor().Ready(ctx); !errors.Is(err, ErrShadowMonitorInvalid) {
		t.Fatalf("cancelled readiness error=%v", err)
	}
}

func shadowMonitorConversation(t *testing.T) im.ConversationRef {
	t.Helper()
	tenant, err := im.ParseTenantID("ten_shadow")
	if err != nil {
		t.Fatal(err)
	}
	conversation, err := im.ParseConversationID("cnv_shadow")
	if err != nil {
		t.Fatal(err)
	}
	reference, err := im.NewConversationRef(tenant, conversation)
	if err != nil {
		t.Fatal(err)
	}
	return reference
}
