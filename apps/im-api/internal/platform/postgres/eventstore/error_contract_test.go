package eventstore

import (
	"context"
	"errors"
	"testing"

	"github.com/huapohen/quantum-entanglement/apps/im-api/internal/events"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
)

func TestEventStoreDoesNotExposeAdapterInternalReadErrors(t *testing.T) {
	t.Parallel()

	for _, internal := range []error{errEventNotFound, errEventIntegrity} {
		if got := mapEventReadError(internal); !errors.Is(got, events.ErrStoreUnavailable) {
			t.Fatalf("internal error %v mapped to %v, want %v", internal, got, events.ErrStoreUnavailable)
		}
	}
	public := errors.New("public caller error")
	if got := mapEventReadError(public); !errors.Is(got, public) {
		t.Fatalf("public error was rewritten: %v", got)
	}
}

func TestEventStoreMapsCapacityAndPreservesContextErrors(t *testing.T) {
	t.Parallel()

	if got := mapError(context.Background(), &pgconn.PgError{Code: "22003"}); !errors.Is(got, events.ErrStoreCapacity) {
		t.Fatalf("capacity error = %v, want %v", got, events.ErrStoreCapacity)
	}
	cancelled, cancel := context.WithCancel(context.Background())
	cancel()
	if got := mapError(cancelled, pgx.ErrTxClosed); !errors.Is(got, context.Canceled) {
		t.Fatalf("cancelled mapping = %v, want %v", got, context.Canceled)
	}
}
