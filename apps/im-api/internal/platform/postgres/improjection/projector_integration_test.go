package improjection

import (
	"context"
	"errors"
	"fmt"
	"net"
	"net/url"
	"os"
	"strconv"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/huapohen/quantum-entanglement/apps/im-api/internal/events"
	"github.com/huapohen/quantum-entanglement/apps/im-api/internal/im"
	"github.com/huapohen/quantum-entanglement/apps/im-api/internal/imstore"
	"github.com/huapohen/quantum-entanglement/apps/im-api/internal/platform/postgres/eventstore"
	"github.com/huapohen/quantum-entanglement/apps/im-api/internal/platform/postgres/migrations"
	"github.com/huapohen/quantum-entanglement/apps/im-api/internal/platform/postgres/runtimepool"
	"github.com/jackc/pgx/v5"
)

var projectorIntegrationSequence atomic.Uint64

// This is deliberately an opt-in integration test. It provisions the same exact runtime
// authority graph as the event-store integration fixture, then exercises EventStore -> projector
// -> materialized Reader through the runtime-only pool.
func TestPostgresMessageProjectorEndToEnd(t *testing.T) {
	adminURL := os.Getenv("WANWORK_TEST_POSTGRES_ADMIN_URL")
	if adminURL == "" {
		t.Skip("WANWORK_TEST_POSTGRES_ADMIN_URL is not set")
	}
	admin, connectionString, manifest := provisionProjectorRuntime(t, adminURL)
	pool, err := runtimepool.Open(t.Context(), runtimepool.Config{
		ConnectionString: connectionString, Manifest: manifest, MaxConnections: 2,
		MinIdleConnections: 0, ConnectTimeout: 3 * time.Second, PingTimeout: time.Second,
		AllowInsecureLocalhost: true,
	})
	if err != nil {
		t.Fatalf("open runtime pool: %v", err)
	}
	t.Cleanup(pool.Close)
	if _, err := admin.Exec(t.Context(), `INSERT INTO wanwork_im.tenants (tenant_id,status,revision) VALUES ('ten_alpha','active',1)`); err != nil {
		t.Fatal(err)
	}
	if _, err := admin.Exec(t.Context(), `INSERT INTO wanwork_im.workspaces (tenant_id,workspace_id,status,revision) VALUES ('ten_alpha','wsp_alpha','active',1)`); err != nil {
		t.Fatal(err)
	}
	store, err := eventstore.New(pool)
	if err != nil {
		t.Fatal(err)
	}
	workspace := "wsp_alpha"
	tenantID := mustTenant(t, "ten_alpha")
	workspaceValue := mustWorkspace(t, workspace)
	when := time.Date(2026, 8, 30, 13, 0, 0, 0, time.UTC)
	eventsToAppend := []events.EventToAppend{
		projectorIntegrationEvent(t, "evt_msg_created", "message.created", 1, `{"conversationId":"cnv_room","messageId":"msg_1","clientMessageId":"msg_client_1","messageType":"text","text":"before"}`, when, workspace),
		projectorIntegrationEvent(t, "evt_conversation_meta", "conversation.updated", 2, `{"conversationId":"cnv_room","value":1}`, when.Add(time.Second), workspace),
		projectorIntegrationEvent(t, "evt_msg_edited", "message.edited", 3, `{"conversationId":"cnv_room","messageId":"msg_1","text":"after"}`, when.Add(2*time.Second), workspace),
	}
	if _, err := store.AppendBatch(t.Context(), events.AppendBatch{TenantID: "ten_alpha", WorkspaceID: &workspace, StreamID: "cnv_room", Events: eventsToAppend}); err != nil {
		t.Fatalf("append conversation events: %v", err)
	}
	projector, err := NewProjector(pool)
	if err != nil {
		t.Fatal(err)
	}
	first, err := projector.Run(t.Context(), tenantID, workspaceValue, 2)
	if err != nil || first.Processed != 3 || first.Checkpoint.Position != 3 {
		t.Fatalf("first projector result=%#v err=%v", first, err)
	}
	reader, err := NewReader(pool)
	if err != nil {
		t.Fatal(err)
	}
	conversation := mustConversation(t, "ten_alpha", "cnv_room")
	page, err := reader.ReadPage(t.Context(), imstore.MessageReadPageQuery{Conversation: conversation, WorkspaceID: workspaceValue, Limit: 10, ConversationRevision: 1, AccessRevision: 1})
	if err != nil || len(page.Messages) != 1 || page.Messages[0].Status() != im.MessageStatusEdited || page.Messages[0].Text() != "after" || page.ProjectionRevision != 3 {
		t.Fatalf("materialized page=%#v err=%v", page, err)
	}
	second, err := projector.Run(t.Context(), tenantID, workspaceValue, 2)
	if err != nil || second.Processed != 0 || second.Checkpoint.Position != 3 {
		t.Fatalf("idempotent rerun=%#v err=%v", second, err)
	}
	// Add a fresh event, then race two workers from the same checkpoint. One may win the CAS while
	// the other observes the committed checkpoint or reports a retryable conflict; neither may
	// produce a divergent materialized snapshot.
	secondMessage := projectorIntegrationEvent(t, "evt_msg_created_2", "message.created", 4,
		`{"conversationId":"cnv_room","messageId":"msg_2","clientMessageId":"msg_client_2","messageType":"text","text":"second"}`,
		when.Add(3*time.Second), workspace)
	if _, err := store.AppendBatch(t.Context(), events.AppendBatch{TenantID: "ten_alpha", WorkspaceID: &workspace,
		StreamID: "cnv_room", ExpectedVersion: 3, Events: []events.EventToAppend{secondMessage}}); err != nil {
		t.Fatalf("append second message: %v", err)
	}
	commitAckLost := false
	projector.commit = func(commitContext context.Context, transaction pgx.Tx) error {
		if !commitAckLost {
			commitAckLost = true
			if err := transaction.Commit(commitContext); err != nil {
				return err
			}
			return errors.New("synthetic projector commit acknowledgement loss")
		}
		return transaction.Commit(commitContext)
	}
	if _, err := projector.Run(t.Context(), tenantID, workspaceValue, 1); !errors.Is(err, imstore.ErrStoreUnavailable) {
		t.Fatalf("commit ACK-loss error=%v, want %v", err, imstore.ErrStoreUnavailable)
	}
	projector.commit = commitProjectorTransaction
	recovered, err := projector.Run(t.Context(), tenantID, workspaceValue, 1)
	if err != nil || recovered.Processed != 0 || recovered.Checkpoint.Position != 4 {
		t.Fatalf("commit ACK-loss recovery=%#v err=%v", recovered, err)
	}
	thirdMessage := projectorIntegrationEvent(t, "evt_msg_created_3", "message.created", 5,
		`{"conversationId":"cnv_room","messageId":"msg_3","clientMessageId":"msg_client_3","messageType":"text","text":"third"}`,
		when.Add(4*time.Second), workspace)
	if _, err := store.AppendBatch(t.Context(), events.AppendBatch{TenantID: "ten_alpha", WorkspaceID: &workspace,
		StreamID: "cnv_room", ExpectedVersion: 4, Events: []events.EventToAppend{thirdMessage}}); err != nil {
		t.Fatalf("append third message: %v", err)
	}
	var results [2]ProjectorResult
	var errs [2]error
	var done atomic.Uint32
	for i := range results {
		go func(i int) {
			results[i], errs[i] = projector.Run(t.Context(), tenantID, workspaceValue, 1)
			done.Add(1)
		}(i)
	}
	deadline := time.Now().Add(5 * time.Second)
	for done.Load() != 2 && time.Now().Before(deadline) {
		time.Sleep(5 * time.Millisecond)
	}
	if done.Load() != 2 {
		t.Fatal("concurrent projector runs did not finish")
	}
	for i, runErr := range errs {
		if runErr != nil && !errors.Is(runErr, ErrProjectorConflict) && !errors.Is(runErr, imstore.ErrStoreUnavailable) {
			t.Fatalf("concurrent projector %d err=%v", i, runErr)
		}
	}
	final, err := reader.ReadPage(t.Context(), imstore.MessageReadPageQuery{Conversation: conversation, WorkspaceID: workspaceValue, Limit: 10, ConversationRevision: 1, AccessRevision: 1})
	if err != nil || len(final.Messages) != 3 || final.Messages[0].Text() != "after" || final.Messages[1].Text() != "second" || final.Messages[2].Text() != "third" || final.ProjectionRevision != 5 {
		t.Fatalf("final materialized page=%#v err=%v", final, err)
	}
	pool.Close()
	reopened, err := runtimepool.Open(t.Context(), runtimepool.Config{
		ConnectionString: connectionString, Manifest: manifest, MaxConnections: 1,
		MinIdleConnections: 0, ConnectTimeout: 3 * time.Second, PingTimeout: time.Second,
		AllowInsecureLocalhost: true,
	})
	if err != nil {
		t.Fatalf("reopen runtime pool: %v", err)
	}
	defer reopened.Close()
	restartedProjector, err := NewProjector(reopened)
	if err != nil {
		t.Fatal(err)
	}
	restarted, err := restartedProjector.Run(t.Context(), tenantID, workspaceValue, 2)
	if err != nil || restarted.Processed != 0 || restarted.Checkpoint.Position != 5 {
		t.Fatalf("restart projector result=%#v err=%v", restarted, err)
	}
	restartedReader, err := NewReader(reopened)
	if err != nil {
		t.Fatal(err)
	}
	restartedPage, err := restartedReader.ReadPage(t.Context(), imstore.MessageReadPageQuery{Conversation: conversation,
		WorkspaceID: workspaceValue, Limit: 10, ConversationRevision: 1, AccessRevision: 1})
	if err != nil || len(restartedPage.Messages) != 3 || restartedPage.ProjectionRevision != 5 {
		t.Fatalf("restart materialized page=%#v err=%v", restartedPage, err)
	}
}

func projectorIntegrationEvent(t *testing.T, id, eventType string, sequence uint64, raw string, occurred time.Time, workspace string) events.EventToAppend {
	t.Helper()
	payload, err := events.NewInlinePayload([]byte(raw))
	if err != nil {
		t.Fatal(err)
	}
	ws, key := workspace, "key_"+id
	return events.EventToAppend{SchemaVersion: 1, EventID: id, StreamID: "cnv_room", EventType: eventType, TenantID: "ten_alpha", WorkspaceID: &ws, ActorID: "usr_alice", OccurredAt: occurred, CorrelationID: "corr_" + id, IdempotencyKey: &key, Payload: payload}
}
func mustTenant(t *testing.T, value string) im.TenantID {
	t.Helper()
	v, err := im.ParseTenantID(value)
	if err != nil {
		t.Fatal(err)
	}
	return v
}
func mustWorkspace(t *testing.T, value string) *im.WorkspaceID {
	t.Helper()
	v, err := im.ParseWorkspaceID(value)
	if err != nil {
		t.Fatal(err)
	}
	return &v
}
func mustConversation(t *testing.T, tenant, conversation string) im.ConversationRef {
	t.Helper()
	ref, err := im.NewConversationRef(mustTenant(t, tenant), mustConversationID(t, conversation))
	if err != nil {
		t.Fatal(err)
	}
	return ref
}
func mustConversationID(t *testing.T, value string) im.ConversationID {
	t.Helper()
	v, err := im.ParseConversationID(value)
	if err != nil {
		t.Fatal(err)
	}
	return v
}

func provisionProjectorRuntime(t *testing.T, adminURL string) (*pgx.Conn, string, migrations.AuthorityAccessManifest) {
	t.Helper()
	cfg, err := pgx.ParseConfig(adminURL)
	if err != nil {
		t.Fatal(err)
	}
	admin, err := pgx.ConnectConfig(t.Context(), cfg)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = admin.Close(context.Background()) })
	suffix := fmt.Sprintf("%d_%d", os.Getpid(), projectorIntegrationSequence.Add(1))
	databaseName := "wanwork_proj_" + suffix
	quotedDB := pgx.Identifier{databaseName}.Sanitize()
	if _, err := admin.Exec(t.Context(), "CREATE DATABASE "+quotedDB+" TEMPLATE template0"); err != nil {
		t.Fatal(err)
	}
	dbCfg := cfg.Copy()
	dbCfg.Database = databaseName
	db, err := pgx.ConnectConfig(t.Context(), dbCfg)
	if err != nil {
		t.Fatal(err)
	}
	roles := []string{"wanwork_proj_owner_" + suffix, "wanwork_proj_migrator_" + suffix, "wanwork_proj_runtime_" + suffix, "wanwork_proj_deploy_" + suffix, "wanwork_proj_app_" + suffix}
	t.Cleanup(func() {
		_ = db.Close(context.Background())
		_, _ = admin.Exec(context.Background(), "DROP DATABASE "+quotedDB+" WITH (FORCE)")
		quoted := make([]string, len(roles))
		for i, r := range roles {
			quoted[i] = pgx.Identifier{r}.Sanitize()
		}
		_, _ = admin.Exec(context.Background(), "DROP ROLE "+strings.Join(quoted, ", "))
	})
	if _, err := migrations.Apply(t.Context(), db); err != nil {
		t.Fatal(err)
	}
	var databaseOwner string
	if err := db.QueryRow(t.Context(), "SELECT current_user").Scan(&databaseOwner); err != nil {
		t.Fatal(err)
	}
	manifest := migrations.AuthorityAccessManifest{DatabaseName: databaseName, DatabaseOwnerRole: databaseOwner, OwnerRole: roles[0], MigratorRole: roles[1], RuntimeRole: roles[2], MigrationLoginRoles: []string{roles[3]}, RuntimeLoginRoles: []string{roles[4]}}
	for i, role := range roles {
		login := "NOLOGIN"
		if i >= 3 {
			login = "LOGIN"
		}
		if _, err := db.Exec(t.Context(), "CREATE ROLE "+pgx.Identifier{role}.Sanitize()+" "+login+" NOSUPERUSER NOINHERIT NOCREATEROLE NOCREATEDB NOREPLICATION NOBYPASSRLS CONNECTION LIMIT -1"); err != nil {
			t.Fatal(err)
		}
	}
	grant := func(parent, child string) {
		if _, err := db.Exec(t.Context(), "GRANT "+pgx.Identifier{parent}.Sanitize()+" TO "+pgx.Identifier{child}.Sanitize()+" WITH INHERIT FALSE"); err != nil {
			t.Fatal(err)
		}
	}
	grant(roles[0], roles[1])
	grant(roles[1], roles[3])
	grant(roles[2], roles[4])
	if _, err := db.Exec(t.Context(), "REVOKE CONNECT, CREATE, TEMPORARY ON DATABASE "+quotedDB+" FROM PUBLIC"); err != nil {
		t.Fatal(err)
	}
	if _, err := db.Exec(t.Context(), "GRANT CREATE ON DATABASE "+quotedDB+" TO "+pgx.Identifier{roles[0]}.Sanitize()); err != nil {
		t.Fatal(err)
	}
	if _, err := db.Exec(t.Context(), "GRANT CONNECT ON DATABASE "+quotedDB+" TO "+pgx.Identifier{roles[3]}.Sanitize()+", "+pgx.Identifier{roles[4]}.Sanitize()); err != nil {
		t.Fatal(err)
	}
	rows, err := db.Query(t.Context(), `SELECT pg_catalog.quote_ident(n.nspname)||'.'||pg_catalog.quote_ident(c.relname) FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname=ANY(ARRAY['wanwork_im','wanwork_meta']) AND c.relkind='r' ORDER BY 1`)
	if err != nil {
		t.Fatal(err)
	}
	relations, err := pgx.CollectRows(rows, pgx.RowTo[string])
	if err != nil {
		t.Fatal(err)
	}
	for _, relation := range relations {
		if _, err := db.Exec(t.Context(), "ALTER TABLE "+relation+" OWNER TO "+pgx.Identifier{roles[0]}.Sanitize()); err != nil {
			t.Fatal(err)
		}
	}
	rows, err = db.Query(t.Context(), `SELECT pg_catalog.quote_ident(p.proname)||'('||pg_catalog.pg_get_function_identity_arguments(p.oid)||')' FROM pg_catalog.pg_proc p JOIN pg_catalog.pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname='wanwork_im' ORDER BY 1`)
	if err != nil {
		t.Fatal(err)
	}
	functions, err := pgx.CollectRows(rows, pgx.RowTo[string])
	if err != nil {
		t.Fatal(err)
	}
	for _, function := range functions {
		if _, err := db.Exec(t.Context(), "ALTER FUNCTION wanwork_im."+function+" OWNER TO "+pgx.Identifier{roles[0]}.Sanitize()); err != nil {
			t.Fatal(err)
		}
	}
	for _, schema := range []string{"wanwork_im", "wanwork_meta"} {
		if _, err := db.Exec(t.Context(), "ALTER SCHEMA "+schema+" OWNER TO "+pgx.Identifier{roles[0]}.Sanitize()); err != nil {
			t.Fatal(err)
		}
	}
	if _, err := db.Exec(t.Context(), "SET ROLE "+pgx.Identifier{roles[0]}.Sanitize()); err != nil {
		t.Fatal(err)
	}
	defer func() { _, _ = db.Exec(context.Background(), "RESET ROLE") }()
	if _, err := db.Exec(t.Context(), "ALTER DEFAULT PRIVILEGES FOR ROLE "+pgx.Identifier{roles[0]}.Sanitize()+" REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC"); err != nil {
		t.Fatal(err)
	}
	if _, err := db.Exec(t.Context(), "GRANT USAGE ON SCHEMA wanwork_im TO "+pgx.Identifier{roles[2]}.Sanitize()); err != nil {
		t.Fatal(err)
	}
	spec, err := migrations.CurrentAuthorityAccessSpecification(manifest)
	if err != nil {
		t.Fatal(err)
	}
	var readTables []string
	for _, object := range spec.Privileges {
		if object.Scope == migrations.AuthorityPrivilegeRelation && object.Privilege == "SELECT" && object.GranteeRole == manifest.RuntimeRole {
			readTables = append(readTables, "wanwork_im."+pgx.Identifier{object.Object}.Sanitize())
		}
	}
	if _, err := db.Exec(t.Context(), "GRANT SELECT ON "+strings.Join(readTables, ", ")+" TO "+pgx.Identifier{roles[2]}.Sanitize()); err != nil {
		t.Fatal(err)
	}
	var executeFunctions []string
	for _, object := range spec.Privileges {
		if object.Scope == migrations.AuthorityPrivilegeFunction && object.GranteeRole == manifest.RuntimeRole && object.Privilege == "EXECUTE" {
			executeFunctions = append(executeFunctions, "wanwork_im."+object.Object+"("+object.IdentityArguments+")")
		}
	}
	if _, err := db.Exec(t.Context(), "GRANT EXECUTE ON FUNCTION "+strings.Join(executeFunctions, ", ")+" TO "+pgx.Identifier{roles[2]}.Sanitize()); err != nil {
		t.Fatal(err)
	}
	query := url.Values{"sslmode": []string{"disable"}}
	value := url.URL{Scheme: "postgresql", User: url.User(roles[4]), Path: "/" + databaseName}
	if strings.HasPrefix(cfg.Host, "/") {
		query.Set("host", cfg.Host)
		query.Set("port", strconv.Itoa(int(cfg.Port)))
	} else {
		ip := net.ParseIP(cfg.Host)
		if ip == nil || !ip.IsLoopback() {
			t.Fatalf("non-local host %q", cfg.Host)
		}
		value.Host = net.JoinHostPort(cfg.Host, strconv.Itoa(int(cfg.Port)))
	}
	value.RawQuery = query.Encode()
	return db, value.String(), manifest
}
