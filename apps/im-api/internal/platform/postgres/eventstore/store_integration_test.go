package eventstore

import (
	"context"
	"errors"
	"fmt"
	"net"
	"net/url"
	"os"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/huapohen/quantum-entanglement/apps/im-api/internal/events"
	"github.com/huapohen/quantum-entanglement/apps/im-api/internal/platform/postgres/migrations"
	"github.com/huapohen/quantum-entanglement/apps/im-api/internal/platform/postgres/runtimepool"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
)

const eventStoreIntegrationURL = "WANWORK_TEST_POSTGRES_ADMIN_URL"

var eventStoreDatabaseSequence atomic.Uint64

func TestPostgresEventStoreAgainstPostgres(t *testing.T) {
	adminURL := os.Getenv(eventStoreIntegrationURL)
	if adminURL == "" {
		t.Skip(eventStoreIntegrationURL + " is not set")
	}
	databaseAdmin, connectionString, manifest := provisionEventStoreRuntime(t, adminURL)
	pool, err := runtimepool.Open(t.Context(), runtimepool.Config{
		ConnectionString:       connectionString,
		Manifest:               manifest,
		MaxConnections:         2,
		MinIdleConnections:     0,
		ConnectTimeout:         3 * time.Second,
		PingTimeout:            time.Second,
		AllowInsecureLocalhost: true,
	})
	if err != nil {
		t.Fatalf("open runtime pool: %v", err)
	}
	t.Cleanup(pool.Close)
	store, err := New(pool)
	if err != nil {
		t.Fatalf("new event store: %v", err)
	}
	seedEventStoreTenant(t, databaseAdmin, "ten_acme", "wsp_acme")
	seedEventStoreWorkspace(t, databaseAdmin, "ten_acme", "wsp_alt")
	seedEventStoreTenant(t, databaseAdmin, "ten_other", "wsp_other")

	workspace := "wsp_acme"
	first := integrationEvent(t, "evt_one", "key_one", "ten_acme", workspace, "task:one")
	second := integrationEvent(t, "evt_two", "key_two", "ten_acme", workspace, "task:one")
	firstBatch := events.AppendBatch{
		TenantID: "ten_acme", WorkspaceID: &workspace, StreamID: "task:one", ExpectedVersion: 0,
		Events: []events.EventToAppend{first, second},
	}
	appended, err := store.AppendBatch(t.Context(), firstBatch)
	if err != nil || appended.Replayed || len(appended.Events) != 2 {
		t.Fatalf("first append = %#v/%v", appended, err)
	}
	if appended.Events[0].Sequence != 1 || appended.Events[1].Sequence != 2 ||
		appended.Events[0].GlobalPosition != 1 || appended.Events[1].GlobalPosition != 2 {
		t.Fatalf("stored coordinates = %#v", appended.Events)
	}

	replay, err := store.AppendBatch(t.Context(), firstBatch)
	if err != nil || !replay.Replayed || len(replay.Events) != 2 {
		t.Fatalf("exact replay = %#v/%v", replay, err)
	}
	drift := firstBatch
	drift.Events = append([]events.EventToAppend(nil), firstBatch.Events...)
	drift.Events[0].ActorID = "act_other"
	if _, err := store.AppendBatch(t.Context(), drift); !errors.Is(err, events.ErrIdempotencyConflict) {
		t.Fatalf("retry drift error = %v, want %v", err, events.ErrIdempotencyConflict)
	}

	stale := events.AppendBatch{
		TenantID: "ten_acme", WorkspaceID: &workspace, StreamID: "task:one", ExpectedVersion: 0,
		Events: []events.EventToAppend{integrationEvent(t, "evt_stale", "key_stale", "ten_acme", workspace, "task:one")},
	}
	if _, err := store.AppendBatch(t.Context(), stale); !errors.Is(err, events.ErrRevisionConflict) {
		t.Fatalf("stale append error = %v, want %v", err, events.ErrRevisionConflict)
	}
	keyConflict := events.AppendBatch{
		TenantID: "ten_acme", WorkspaceID: &workspace, StreamID: "task:one", ExpectedVersion: 2,
		Events: []events.EventToAppend{integrationEvent(t, "evt_three", "key_one", "ten_acme", workspace, "task:one")},
	}
	if _, err := store.AppendBatch(t.Context(), keyConflict); !errors.Is(err, events.ErrIdempotencyConflict) {
		t.Fatalf("idempotency index conflict = %v, want %v", err, events.ErrIdempotencyConflict)
	}

	streamQuery := events.StreamQuery{TenantID: "ten_acme", WorkspaceID: &workspace, StreamID: "task:one", Limit: 1}
	var streamEvents []events.StoredEvent
	for {
		page, err := store.ReadStreamPage(t.Context(), streamQuery)
		if err != nil {
			t.Fatalf("stream page: %v", err)
		}
		streamEvents = append(streamEvents, page.Events...)
		if !page.HasMore {
			break
		}
		if page.Next == "" || page.Next == streamQuery.After {
			t.Fatalf("stream cursor did not advance: %#v", page)
		}
		streamQuery.After = page.Next
	}
	if len(streamEvents) != 2 || streamEvents[0].EventID != "evt_one" || streamEvents[1].EventID != "evt_two" {
		t.Fatalf("stream events = %#v", streamEvents)
	}
	global, err := store.ReadGlobalPage(t.Context(), events.GlobalQuery{TenantID: "ten_acme", WorkspaceID: &workspace, Limit: 10})
	if err != nil || len(global.Events) != 2 || global.Events[0].GlobalPosition != 1 || global.Events[1].GlobalPosition != 2 {
		t.Fatalf("global page = %#v/%v", global, err)
	}
	if _, err := store.ReadStreamPage(t.Context(), events.StreamQuery{
		TenantID: "ten_acme", WorkspaceID: &workspace, StreamID: "task:one", After: streamQuery.After, Limit: 1,
	}); err != nil {
		t.Fatalf("tail stream page: %v", err)
	}

	// Event IDs are scoped to tenant+workspace (not stream), while idempotency keys are scoped to
	// tenant+workspace+stream. These are the same retry identities as the contract fake.
	altWorkspace := "wsp_alt"
	if _, err := store.AppendBatch(t.Context(), events.AppendBatch{
		TenantID: "ten_acme", WorkspaceID: &altWorkspace, StreamID: "task:one", Events: []events.EventToAppend{
			integrationEvent(t, "evt_one", "key_alt_workspace", "ten_acme", altWorkspace, "task:one"),
		},
	}); err != nil {
		t.Fatalf("same-tenant alternate workspace event ID append: %v", err)
	}
	otherWorkspace := "wsp_other"
	crossWorkspace := integrationEvent(t, "evt_one", "key_other_workspace", "ten_other", otherWorkspace, "task:one")
	if _, err := store.AppendBatch(t.Context(), events.AppendBatch{
		TenantID: "ten_other", WorkspaceID: &otherWorkspace, StreamID: "task:one", Events: []events.EventToAppend{crossWorkspace},
	}); err != nil {
		t.Fatalf("cross-tenant event ID append: %v", err)
	}
	if page, err := store.ReadGlobalPage(t.Context(), events.GlobalQuery{TenantID: "ten_acme", WorkspaceID: &workspace, Limit: 10}); err != nil || len(page.Events) != 2 {
		t.Fatalf("tenant isolation page = %#v/%v", page, err)
	}
	if page, err := store.ReadGlobalPage(t.Context(), events.GlobalQuery{TenantID: "ten_acme", WorkspaceID: &altWorkspace, Limit: 10}); err != nil || len(page.Events) != 1 || page.Events[0].EventID != "evt_one" {
		t.Fatalf("alternate workspace page = %#v/%v", page, err)
	}
	if page, err := store.ReadGlobalPage(t.Context(), events.GlobalQuery{TenantID: "ten_acme", Limit: 10}); err != nil || len(page.Events) != 0 {
		t.Fatalf("nil workspace page = %#v/%v", page, err)
	}

	checkpointStore, err := NewProjectionCheckpointStore(pool)
	if err != nil {
		t.Fatalf("new projection checkpoint store: %v", err)
	}
	projectionScope := events.ProjectionScope{
		TenantID: "ten_acme", WorkspaceID: &workspace, ProjectionID: "messages-v1",
	}
	initial, err := checkpointStore.LoadProjectionCheckpoint(t.Context(), projectionScope)
	if err != nil || initial.Scope.TenantID != projectionScope.TenantID || initial.Position != 0 {
		t.Fatalf("initial projection checkpoint = %#v/%v", initial, err)
	}
	checkpoint := events.ProjectionCheckpoint{
		Scope: projectionScope, Position: global.Events[1].GlobalPosition,
		Cursor: global.Next, LastEventID: global.Events[1].EventID,
	}
	if err := checkpointStore.CommitProjectionCheckpoint(t.Context(), initial, checkpoint); err != nil {
		t.Fatalf("commit projection checkpoint: %v", err)
	}
	reloaded, err := checkpointStore.LoadProjectionCheckpoint(t.Context(), projectionScope)
	if err != nil || reloaded.Position != checkpoint.Position || reloaded.Cursor != checkpoint.Cursor ||
		reloaded.LastEventID != checkpoint.LastEventID {
		t.Fatalf("reloaded projection checkpoint = %#v/%v", reloaded, err)
	}
	if err := checkpointStore.CommitProjectionCheckpoint(t.Context(), initial, checkpoint); !errors.Is(err, events.ErrProjectionCheckpointConflict) {
		t.Fatalf("stale projection checkpoint commit = %v, want %v", err, events.ErrProjectionCheckpointConflict)
	}

	inboxStore, err := NewNativeIMInboxStore(pool)
	if err != nil {
		t.Fatalf("new native IM inbox store: %v", err)
	}
	inboxPayload, err := events.NewInlinePayload([]byte(`{"message":"hello"}`))
	if err != nil {
		t.Fatalf("native IM inbox payload: %v", err)
	}
	inboxWorkspace := workspace
	inboxEnvelope := events.InboxEnvelope{
		Scope: events.InboxScope{
			TenantID: "ten_acme", WorkspaceID: &inboxWorkspace,
			Provider: "rongcloud", ChannelID: "channel_main",
		},
		EventID:        "provider-event-1",
		EventDigest:    events.SHA256Digest("sha256:" + strings.Repeat("c", 64)),
		VerificationID: "verification-1", Payload: inboxPayload,
	}
	inboxFirst, err := inboxStore.Admit(t.Context(), inboxEnvelope)
	if err != nil || inboxFirst.Status != events.InboxInserted || inboxFirst.Receipt.DeliveryCount != 1 {
		t.Fatalf("native IM inbox first admission = %#v/%v", inboxFirst, err)
	}
	inboxReplay, err := inboxStore.Admit(t.Context(), inboxEnvelope)
	if err != nil || inboxReplay.Status != events.InboxReplayed || inboxReplay.Receipt.DeliveryCount != 2 {
		t.Fatalf("native IM inbox replay = %#v/%v", inboxReplay, err)
	}
	unknownEnvelope := inboxEnvelope
	unknownEnvelope.EventID = "provider-event-unknown"
	var injectedUnknown atomic.Bool
	inboxStore.commitHook = func(ctx context.Context, transaction pgx.Tx) error {
		if injectedUnknown.CompareAndSwap(false, true) {
			if err := transaction.Commit(ctx); err != nil {
				return err
			}
			return errors.New("synthetic inbox commit acknowledgement loss")
		}
		return commitInboxTransaction(ctx, transaction)
	}
	unknownAdmission, err := inboxStore.Admit(t.Context(), unknownEnvelope)
	if err != nil || unknownAdmission.Status != events.InboxReplayed ||
		!unknownAdmission.ResolvedAfterUnknown || unknownAdmission.Receipt.DeliveryCount != 1 {
		t.Fatalf("native IM inbox unknown commit = %#v/%v", unknownAdmission, err)
	}
	inboxStore.commitHook = commitInboxTransaction
	inboxDrift := inboxEnvelope
	inboxDrift.EventDigest = events.SHA256Digest("sha256:" + strings.Repeat("d", 64))
	if _, err := inboxStore.Admit(t.Context(), inboxDrift); !errors.Is(err, events.ErrInboxDigestConflict) {
		t.Fatalf("native IM inbox digest drift = %v, want %v", err, events.ErrInboxDigestConflict)
	}
	inboxLoaded, err := inboxStore.Load(t.Context(), inboxEnvelope.Scope, inboxEnvelope.EventID)
	if err != nil || inboxLoaded.DeliveryCount != 2 || inboxLoaded.Envelope.EventDigest != inboxEnvelope.EventDigest {
		t.Fatalf("native IM inbox load = %#v/%v", inboxLoaded, err)
	}
	otherInboxScope := inboxEnvelope.Scope
	otherInboxScope.TenantID = "ten_other"
	if _, err := inboxStore.Load(t.Context(), otherInboxScope, inboxEnvelope.EventID); !errors.Is(err, events.ErrInboxNotFound) {
		t.Fatalf("cross-tenant inbox load = %v, want %v", err, events.ErrInboxNotFound)
	}

	connection, err := pool.Acquire(t.Context())
	if err != nil {
		t.Fatalf("acquire runtime connection: %v", err)
	}
	_, rawWriteErr := connection.Exec(t.Context(), `
INSERT INTO wanwork_im.event_log (
 tenant_id, workspace_id, stream_id, sequence, global_position, event_id, schema_version,
 event_type, actor_id, occurred_at, correlation_id, causation_id, idempotency_key, traceparent,
 payload_kind, payload_inline, payload_storage, payload_reference_id, payload_byte_length,
 payload_digest, append_digest
) VALUES ('ten_acme', 'wsp_acme', 'task:raw', 1, 99, 'evt_raw', 1, 'task.created.v1',
 'act_user', clock_timestamp(), 'corr_raw', '', 'key_raw', '', 'inline', '{"value":1}', '', '', -1,
 'sha256:' || repeat('0', 64), 'sha256:' || repeat('0', 64))`)
	connection.Release()
	if !postgresCodeEventStore(rawWriteErr, "42501") {
		t.Fatalf("raw runtime event write error = %v, want SQLSTATE 42501", rawWriteErr)
	}
	connection, err = pool.Acquire(t.Context())
	if err != nil {
		t.Fatalf("acquire runtime connection for raw inbox write: %v", err)
	}
	_, rawInboxWriteErr := connection.Exec(t.Context(), `
INSERT INTO wanwork_im.native_im_inbox (
 tenant_id, workspace_id, provider, channel_id, event_id, event_digest, verification_id,
 payload_kind, payload_inline, payload_storage, payload_reference_id, payload_byte_length, payload_digest
) VALUES ('ten_acme', 'wsp_acme', 'rongcloud', 'channel_main', 'provider-event-raw',
 'sha256:' || repeat('e', 64), 'verification-raw', 'inline', '{"message":"raw"}', '', '', -1,
 'sha256:' || repeat('f', 64))`)
	connection.Release()
	if !postgresCodeEventStore(rawInboxWriteErr, "42501") {
		t.Fatalf("raw runtime inbox write error = %v, want SQLSTATE 42501", rawInboxWriteErr)
	}

	// Native IM ingress admits the transport receipt and canonical event in one transaction.
	atomicStore, err := NewNativeIMAtomicStore(pool)
	if err != nil {
		t.Fatalf("new native IM atomic store: %v", err)
	}
	atomicWorkspace := workspace
	atomicPayload, err := events.NewInlinePayload([]byte(`{"message":"atomic"}`))
	if err != nil {
		t.Fatalf("atomic payload: %v", err)
	}
	atomicEvent := events.EventToAppend{
		SchemaVersion: 1, EventID: "provider-event-atomic", StreamID: "inbound:channel_main",
		EventType: "message.received.v1", TenantID: "ten_acme", WorkspaceID: &atomicWorkspace,
		ActorID: "act_user", OccurredAt: time.Date(2026, time.August, 29, 1, 2, 3, 0, time.UTC),
		CorrelationID: "corr-atomic", IdempotencyKey: stringPointer("atomic-key"), Payload: atomicPayload,
	}
	atomicDigest, err := events.DigestEventToAppend(atomicEvent)
	if err != nil {
		t.Fatalf("atomic event digest: %v", err)
	}
	atomicProjection := events.InboxEventProjection{
		Envelope: events.InboxEnvelope{
			Scope:   events.InboxScope{TenantID: "ten_acme", WorkspaceID: &atomicWorkspace, Provider: "rongcloud", ChannelID: "channel_main"},
			EventID: atomicEvent.EventID, EventDigest: atomicDigest, VerificationID: "verification-atomic", Payload: atomicPayload,
		},
		SchemaVersion: atomicEvent.SchemaVersion, StreamID: atomicEvent.StreamID, EventType: atomicEvent.EventType,
		ActorID: atomicEvent.ActorID, OccurredAt: atomicEvent.OccurredAt, CorrelationID: atomicEvent.CorrelationID,
		IdempotencyKey: atomicEvent.IdempotencyKey, ExpectedVersion: 0,
	}
	atomicFirst, err := atomicStore.AdmitAndAppend(t.Context(), atomicProjection)
	if err != nil || atomicFirst.Inbox.Status != events.InboxInserted || atomicFirst.Append.Replayed || len(atomicFirst.Append.Events) != 1 {
		t.Fatalf("atomic first admission = %#v/%v", atomicFirst, err)
	}
	atomicReplay, err := atomicStore.AdmitAndAppend(t.Context(), atomicProjection)
	if err != nil || atomicReplay.Inbox.Status != events.InboxReplayed || !atomicReplay.Append.Replayed || atomicReplay.Inbox.Receipt.DeliveryCount != 2 {
		t.Fatalf("atomic replay admission = %#v/%v", atomicReplay, err)
	}
	atomicFailedEvent := atomicEvent
	atomicFailedEvent.EventID = "provider-event-atomic-rollback"
	failedDigest, err := events.DigestEventToAppend(atomicFailedEvent)
	if err != nil {
		t.Fatalf("atomic rollback event digest: %v", err)
	}
	atomicFailed := atomicProjection
	atomicFailed.Envelope.EventID = "provider-event-atomic-rollback"
	atomicFailed.Envelope.EventDigest = failedDigest
	atomicFailed.ExpectedVersion = 99
	if _, err := atomicStore.AdmitAndAppend(t.Context(), atomicFailed); !errors.Is(err, events.ErrRevisionConflict) {
		t.Fatalf("atomic revision rollback error = %v, want %v", err, events.ErrRevisionConflict)
	}
	if _, err := inboxStore.Load(t.Context(), atomicFailed.Envelope.Scope, atomicFailed.Envelope.EventID); !errors.Is(err, events.ErrInboxNotFound) {
		t.Fatalf("atomic rollback left inbox receipt = %v, want %v", err, events.ErrInboxNotFound)
	}

	pool.Close()
	reopened, err := runtimepool.Open(t.Context(), runtimepool.Config{
		ConnectionString:       connectionString,
		Manifest:               manifest,
		MaxConnections:         1,
		MinIdleConnections:     0,
		ConnectTimeout:         3 * time.Second,
		PingTimeout:            time.Second,
		AllowInsecureLocalhost: true,
	})
	if err != nil {
		t.Fatalf("reopen runtime pool: %v", err)
	}
	t.Cleanup(reopened.Close)
	reopenedStore, err := New(reopened)
	if err != nil {
		t.Fatalf("new reopened event store: %v", err)
	}
	page, err := reopenedStore.ReadStreamPage(t.Context(), events.StreamQuery{TenantID: "ten_acme", WorkspaceID: &workspace, StreamID: "task:one", Limit: 10})
	if err != nil || len(page.Events) != 2 {
		t.Fatalf("reopened stream page = %#v/%v", page, err)
	}
	reopenedCheckpointStore, err := NewProjectionCheckpointStore(reopened)
	if err != nil {
		t.Fatalf("new reopened projection checkpoint store: %v", err)
	}
	checkpointAfterRestart, err := reopenedCheckpointStore.LoadProjectionCheckpoint(t.Context(), projectionScope)
	if err != nil || checkpointAfterRestart.Position != checkpoint.Position ||
		checkpointAfterRestart.LastEventID != checkpoint.LastEventID {
		t.Fatalf("reopened projection checkpoint = %#v/%v", checkpointAfterRestart, err)
	}

	// Serializable append admission has one winner for a fresh stream. The loser may surface as a
	// revision conflict or a transient serialization failure, both of which are safe to retry.
	parallelStream := "task:concurrent"
	results := make(chan error, 2)
	var wait sync.WaitGroup
	for index := 0; index < 2; index++ {
		wait.Add(1)
		go func(index int) {
			defer wait.Done()
			event := integrationEvent(t, fmt.Sprintf("evt_concurrent_%d", index), fmt.Sprintf("key_concurrent_%d", index), "ten_acme", workspace, parallelStream)
			_, appendErr := reopenedStore.AppendBatch(t.Context(), events.AppendBatch{
				TenantID: "ten_acme", WorkspaceID: &workspace, StreamID: parallelStream,
				ExpectedVersion: 0, Events: []events.EventToAppend{event},
			})
			results <- appendErr
		}(index)
	}
	wait.Wait()
	close(results)
	winners := 0
	for appendErr := range results {
		if appendErr == nil {
			winners++
			continue
		}
		if !errors.Is(appendErr, events.ErrRevisionConflict) && !errors.Is(appendErr, events.ErrStoreUnavailable) {
			t.Fatalf("concurrent loser error = %v", appendErr)
		}
	}
	if winners != 1 {
		t.Fatalf("concurrent append winners = %d, want 1", winners)
	}
}

func provisionEventStoreRuntime(t *testing.T, adminURL string) (*pgx.Conn, string, migrations.AuthorityAccessManifest) {
	t.Helper()
	adminConfig, err := pgx.ParseConfig(adminURL)
	if err != nil {
		t.Fatalf("parse %s: %v", eventStoreIntegrationURL, err)
	}
	admin, err := pgx.ConnectConfig(t.Context(), adminConfig)
	if err != nil {
		t.Fatalf("connect integration admin: %v", err)
	}
	t.Cleanup(func() { _ = admin.Close(context.Background()) })
	suffix := fmt.Sprintf("%d_%d", os.Getpid(), eventStoreDatabaseSequence.Add(1))
	databaseName := "wanwork_event_" + suffix
	quotedDatabase := pgx.Identifier{databaseName}.Sanitize()
	if _, err := admin.Exec(t.Context(), "CREATE DATABASE "+quotedDatabase+" TEMPLATE template0"); err != nil {
		t.Fatalf("create event store database: %v", err)
	}
	databaseConfig := adminConfig.Copy()
	databaseConfig.Database = databaseName
	databaseConnection, err := pgx.ConnectConfig(t.Context(), databaseConfig)
	if err != nil {
		t.Fatalf("connect event store database: %v", err)
	}
	roles := make([]string, 0, 5)
	t.Cleanup(func() {
		_ = databaseConnection.Close(context.Background())
		_, _ = admin.Exec(context.Background(), "DROP DATABASE "+quotedDatabase+" WITH (FORCE)")
		if len(roles) != 0 {
			quotedRoles := make([]string, 0, len(roles))
			for _, role := range roles {
				quotedRoles = append(quotedRoles, pgx.Identifier{role}.Sanitize())
			}
			_, _ = admin.Exec(context.Background(), "DROP ROLE "+strings.Join(quotedRoles, ", "))
		}
	})
	if _, err := migrations.Apply(t.Context(), databaseConnection); err != nil {
		t.Fatalf("apply event store migrations: %v", err)
	}
	var databaseOwner string
	if err := databaseConnection.QueryRow(t.Context(), "SELECT current_user").Scan(&databaseOwner); err != nil {
		t.Fatalf("read event store database owner: %v", err)
	}
	manifest := migrations.AuthorityAccessManifest{
		DatabaseName: databaseName, DatabaseOwnerRole: databaseOwner,
		OwnerRole: "wanwork_event_owner_" + suffix, MigratorRole: "wanwork_event_migrator_" + suffix,
		RuntimeRole:         "wanwork_event_runtime_" + suffix,
		MigrationLoginRoles: []string{"wanwork_event_deploy_" + suffix},
		RuntimeLoginRoles:   []string{"wanwork_event_app_" + suffix},
	}
	roles = append(roles, manifest.OwnerRole, manifest.MigratorRole, manifest.RuntimeRole,
		manifest.MigrationLoginRoles[0], manifest.RuntimeLoginRoles[0])
	for _, role := range roles {
		login := "NOLOGIN"
		if role == manifest.MigrationLoginRoles[0] || role == manifest.RuntimeLoginRoles[0] {
			login = "LOGIN"
		}
		if _, err := databaseConnection.Exec(t.Context(), "CREATE ROLE "+pgx.Identifier{role}.Sanitize()+" "+login+
			" NOSUPERUSER NOINHERIT NOCREATEROLE NOCREATEDB NOREPLICATION NOBYPASSRLS CONNECTION LIMIT -1"); err != nil {
			t.Fatalf("create event store role %s: %v", role, err)
		}
	}
	grantEventStoreRole(t, databaseConnection, manifest.OwnerRole, manifest.MigratorRole)
	grantEventStoreRole(t, databaseConnection, manifest.MigratorRole, manifest.MigrationLoginRoles[0])
	grantEventStoreRole(t, databaseConnection, manifest.RuntimeRole, manifest.RuntimeLoginRoles[0])
	configureEventStoreAuthority(t, databaseConnection, manifest)
	return databaseConnection, eventStoreRuntimeConnectionString(t, databaseConfig, manifest), manifest
}

func configureEventStoreAuthority(t *testing.T, connection *pgx.Conn, manifest migrations.AuthorityAccessManifest) {
	t.Helper()
	quotedDatabase := pgx.Identifier{manifest.DatabaseName}.Sanitize()
	quotedOwner := pgx.Identifier{manifest.OwnerRole}.Sanitize()
	quotedRuntime := pgx.Identifier{manifest.RuntimeRole}.Sanitize()
	statements := []string{
		"REVOKE CONNECT, CREATE, TEMPORARY ON DATABASE " + quotedDatabase + " FROM PUBLIC",
		"GRANT CREATE ON DATABASE " + quotedDatabase + " TO " + quotedOwner,
		"GRANT CONNECT ON DATABASE " + quotedDatabase + " TO " + pgx.Identifier{manifest.MigrationLoginRoles[0]}.Sanitize(),
		"GRANT CONNECT ON DATABASE " + quotedDatabase + " TO " + pgx.Identifier{manifest.RuntimeLoginRoles[0]}.Sanitize(),
	}
	for _, statement := range statements {
		if _, err := connection.Exec(t.Context(), statement); err != nil {
			t.Fatalf("configure database authority: %v", err)
		}
	}
	relations := eventStoreRuntimeRelations(t, connection)
	for _, relation := range relations {
		if _, err := connection.Exec(t.Context(), "ALTER TABLE "+relation+" OWNER TO "+quotedOwner); err != nil {
			t.Fatalf("transfer event store relation %s: %v", relation, err)
		}
	}
	functions := eventStoreRuntimeFunctions(t, connection)
	for _, function := range functions {
		if _, err := connection.Exec(t.Context(), "ALTER FUNCTION wanwork_im."+function+" OWNER TO "+quotedOwner); err != nil {
			t.Fatalf("transfer event store function %s: %v", function, err)
		}
	}
	for _, schema := range []string{"wanwork_meta", "wanwork_im"} {
		if _, err := connection.Exec(t.Context(), "ALTER SCHEMA "+pgx.Identifier{schema}.Sanitize()+" OWNER TO "+quotedOwner); err != nil {
			t.Fatalf("transfer event store schema %s: %v", schema, err)
		}
	}
	if _, err := connection.Exec(t.Context(), "SET ROLE "+quotedOwner); err != nil {
		t.Fatalf("set event store owner role: %v", err)
	}
	defer func() { _, _ = connection.Exec(context.Background(), "RESET ROLE") }()
	if _, err := connection.Exec(t.Context(), "ALTER DEFAULT PRIVILEGES FOR ROLE "+quotedOwner+" REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC"); err != nil {
		t.Fatalf("freeze event store default function privileges: %v", err)
	}
	if _, err := connection.Exec(t.Context(), "GRANT USAGE ON SCHEMA wanwork_im TO "+quotedRuntime); err != nil {
		t.Fatalf("grant event store schema usage: %v", err)
	}
	readTables := []string{
		"conversation_access_heads", "conversation_access_snapshots", "conversation_heads",
		"conversation_membership_heads", "conversation_membership_snapshots", "conversation_snapshots",
		"provider_conversation_binding_heads", "provider_conversation_binding_snapshots", "tenant_command_receipts",
		"event_stream_heads", "event_tenant_heads", "event_log",
		"event_projection_checkpoints",
		"native_im_inbox",
	}
	qualifiedTables := make([]string, 0, len(readTables))
	for _, table := range readTables {
		qualifiedTables = append(qualifiedTables, "wanwork_im."+pgx.Identifier{table}.Sanitize())
	}
	if _, err := connection.Exec(t.Context(), "GRANT SELECT ON "+strings.Join(qualifiedTables, ", ")+" TO "+quotedRuntime); err != nil {
		t.Fatalf("grant event store table reads: %v", err)
	}
	qualifiedFunctions := make([]string, 0, len(functions))
	for _, function := range functions {
		qualifiedFunctions = append(qualifiedFunctions, "wanwork_im."+function)
	}
	if _, err := connection.Exec(t.Context(), "GRANT EXECUTE ON FUNCTION "+strings.Join(qualifiedFunctions, ", ")+" TO "+quotedRuntime); err != nil {
		t.Fatalf("grant event store function execution: %v", err)
	}
}

func eventStoreRuntimeRelations(t *testing.T, connection *pgx.Conn) []string {
	rows, err := connection.Query(t.Context(), `
SELECT pg_catalog.quote_ident(namespace.nspname) || '.' || pg_catalog.quote_ident(relation.relname)
FROM pg_catalog.pg_class AS relation
JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid = relation.relnamespace
WHERE namespace.nspname = ANY(ARRAY['wanwork_im', 'wanwork_meta']) AND relation.relkind = 'r'
ORDER BY namespace.nspname, relation.relname`)
	if err != nil {
		t.Fatalf("list event store relations: %v", err)
	}
	values, err := pgx.CollectRows(rows, pgx.RowTo[string])
	if err != nil || len(values) != 28 {
		t.Fatalf("event store relation count = %d/%v", len(values), err)
	}
	return values
}

func eventStoreRuntimeFunctions(t *testing.T, connection *pgx.Conn) []string {
	rows, err := connection.Query(t.Context(), `
SELECT pg_catalog.quote_ident(procedure.proname) || '(' ||
       pg_catalog.pg_get_function_identity_arguments(procedure.oid) || ')'
FROM pg_catalog.pg_proc AS procedure
JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid = procedure.pronamespace
WHERE namespace.nspname = 'wanwork_im'
ORDER BY procedure.proname, pg_catalog.pg_get_function_identity_arguments(procedure.oid)`)
	if err != nil {
		t.Fatalf("list event store functions: %v", err)
	}
	values, err := pgx.CollectRows(rows, pgx.RowTo[string])
	if err != nil || len(values) != 8 {
		t.Fatalf("event store function count = %d/%v", len(values), err)
	}
	return values
}

func grantEventStoreRole(t *testing.T, connection *pgx.Conn, granted, member string) {
	if _, err := connection.Exec(t.Context(), "GRANT "+pgx.Identifier{granted}.Sanitize()+" TO "+pgx.Identifier{member}.Sanitize()+" WITH INHERIT FALSE"); err != nil {
		t.Fatalf("grant event store role %s to %s: %v", granted, member, err)
	}
}

func eventStoreRuntimeConnectionString(t *testing.T, adminConfig *pgx.ConnConfig, manifest migrations.AuthorityAccessManifest) string {
	query := url.Values{"sslmode": []string{"disable"}}
	value := url.URL{Scheme: "postgresql", User: url.User(manifest.RuntimeLoginRoles[0]), Path: "/" + manifest.DatabaseName}
	if strings.HasPrefix(adminConfig.Host, "/") {
		query.Set("host", adminConfig.Host)
		query.Set("port", strconv.Itoa(int(adminConfig.Port)))
	} else {
		address := net.ParseIP(adminConfig.Host)
		if address == nil || !address.IsLoopback() {
			t.Fatalf("event store integration host %q is not local", adminConfig.Host)
		}
		value.Host = net.JoinHostPort(adminConfig.Host, strconv.Itoa(int(adminConfig.Port)))
	}
	value.RawQuery = query.Encode()
	return value.String()
}

func seedEventStoreTenant(t *testing.T, admin *pgx.Conn, tenant, workspace string) {
	if _, err := admin.Exec(t.Context(), `
INSERT INTO wanwork_im.tenants (tenant_id, status, revision) VALUES ($1, 'active', 1)`, tenant); err != nil {
		t.Fatalf("seed tenant %s: %v", tenant, err)
	}
	seedEventStoreWorkspace(t, admin, tenant, workspace)
}

func seedEventStoreWorkspace(t *testing.T, admin *pgx.Conn, tenant, workspace string) {
	if _, err := admin.Exec(t.Context(), `
INSERT INTO wanwork_im.workspaces (tenant_id, workspace_id, status, revision)
VALUES ($1, $2, 'active', 1)`, tenant, workspace); err != nil {
		t.Fatalf("seed workspace %s: %v", workspace, err)
	}
}

func integrationEvent(t *testing.T, eventID, key, tenant, workspace, stream string) events.EventToAppend {
	t.Helper()
	payload, err := events.NewInlinePayload([]byte(`{"value":1}`))
	if err != nil {
		t.Fatalf("integration payload: %v", err)
	}
	workspaceCopy, keyCopy := workspace, key
	return events.EventToAppend{
		SchemaVersion: 1, EventID: eventID, StreamID: stream, EventType: "task.created.v1",
		TenantID: tenant, WorkspaceID: &workspaceCopy, ActorID: "act_user",
		OccurredAt:    time.Date(2026, time.August, 29, 0, 0, 0, 0, time.UTC),
		CorrelationID: "corr_integration", IdempotencyKey: &keyCopy, Payload: payload,
	}
}

func postgresCodeEventStore(err error, code string) bool {
	var postgresError *pgconn.PgError
	return errors.As(err, &postgresError) && postgresError.Code == code
}
