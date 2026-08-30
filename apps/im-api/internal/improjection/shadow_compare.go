package improjection

import (
	"context"
	"errors"
	"fmt"

	"github.com/huapohen/quantum-entanglement/apps/im-api/internal/im"
	"github.com/huapohen/quantum-entanglement/apps/im-api/internal/imstore"
)

var (
	// ErrShadowMismatch means that replay and materialized readers returned different
	// durable message observations. It is intentionally distinct from a repository
	// failure: a mismatch must stop cutover rather than be treated as a fallback.
	ErrShadowMismatch = errors.New("message replay/materialized shadow mismatch")
	ErrShadowInvalid  = errors.New("invalid message shadow comparison request")
)

const maxShadowPages = 4096

// ShadowComparison records the bounded amount of data compared by CompareMessageReaders.
// ProjectionRevision is deliberately not compared: the replay reader exposes stream version,
// while the materialized reader exposes materialized generation. Message row revisions and
// conversation/access revisions are compared exactly.
type ShadowComparison struct {
	Pages    uint64
	Messages uint64
}

// CompareMessageReaders drains both read models from the same logical query and compares their
// ordered message observations. Each repository receives and advances its own opaque cursor;
// cursors are never exchanged between implementations. The function performs no writes and
// never treats a mismatch as a best-effort merge.
//
// The caller must provide an empty cursor. A shadow run is a complete bounded replay from the
// beginning; comparing from an arbitrary cursor would require a caller-provided cross-reader
// cursor binding that neither implementation can safely prove.
func CompareMessageReaders(
	ctx context.Context,
	replay imstore.MessageReadRepository,
	materialized imstore.MessageReadRepository,
	query imstore.MessageReadPageQuery,
) (ShadowComparison, error) {
	if ctx == nil || ctx.Err() != nil || replay == nil || materialized == nil ||
		query.Conversation.IsZero() || query.Limit == 0 || query.Limit > 256 ||
		query.ConversationRevision == 0 || query.AccessRevision == 0 || query.AfterCursor != "" {
		return ShadowComparison{}, ErrShadowInvalid
	}
	result := ShadowComparison{}
	var replayCursor, materializedCursor string
	for result.Pages < maxShadowPages {
		if err := ctx.Err(); err != nil {
			return ShadowComparison{}, err
		}
		replayQuery := query
		replayQuery.AfterCursor = replayCursor
		materializedQuery := query
		materializedQuery.AfterCursor = materializedCursor
		replayPage, err := replay.ReadPage(ctx, replayQuery)
		if err != nil {
			return ShadowComparison{}, err
		}
		materializedPage, err := materialized.ReadPage(ctx, materializedQuery)
		if err != nil {
			return ShadowComparison{}, err
		}
		if err := compareMessagePages(replayPage, materializedPage); err != nil {
			return ShadowComparison{}, err
		}
		result.Pages++
		result.Messages += uint64(len(replayPage.Messages))
		if !replayPage.HasMore && !materializedPage.HasMore {
			return result, nil
		}
		if replayPage.HasMore != materializedPage.HasMore ||
			replayPage.NextCursor == "" || materializedPage.NextCursor == "" ||
			replayPage.NextCursor == replayCursor || materializedPage.NextCursor == materializedCursor {
			return ShadowComparison{}, fmt.Errorf("%w: pagination progress", ErrShadowMismatch)
		}
		replayCursor = replayPage.NextCursor
		materializedCursor = materializedPage.NextCursor
	}
	return ShadowComparison{}, fmt.Errorf("%w: page bound exceeded", ErrShadowMismatch)
}

func compareMessagePages(replay, materialized imstore.MessageReadPage) error {
	if replay.Conversation != materialized.Conversation ||
		replay.ConversationRevision != materialized.ConversationRevision ||
		replay.HasMore != materialized.HasMore || len(replay.Messages) != len(materialized.Messages) {
		return fmt.Errorf("%w: page metadata", ErrShadowMismatch)
	}
	for index := range replay.Messages {
		if !equalMessageSnapshot(replay.Messages[index], materialized.Messages[index]) {
			return fmt.Errorf("%w: message index %d", ErrShadowMismatch, index)
		}
	}
	return nil
}

func equalMessageSnapshot(left, right im.MessageSnapshot) bool {
	return left.Ref() == right.Ref() && left.Sender() == right.Sender() &&
		left.ClientMessageID() == right.ClientMessageID() &&
		left.MessageType() == right.MessageType() && left.Status() == right.Status() &&
		left.Text() == right.Text() && left.ExtInfo() == right.ExtInfo() &&
		left.CreatedAt().Equal(right.CreatedAt()) && left.Revision() == right.Revision()
}
