package localdemo

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"sort"
	"strings"
	"time"

	"github.com/huapohen/quantum-entanglement/apps/im-api/internal/im"
)

// TaskView, ArtifactView and NeedsYouView are intentionally explicit projections. They are
// not encoded as chat text: the Web client can render lifecycle, review and recovery surfaces
// without scraping messages. The local demo keeps them in memory; W2 will persist the same
// boundaries in PostgreSQL with tenant/revision/dedupe guarantees.
type TaskView struct {
	ID                   string   `json:"id"`
	Title                string   `json:"title"`
	Instruction          string   `json:"instruction"`
	Status               string   `json:"status"`
	ParentConversationID string   `json:"parentConversationId"`
	ChildConversationID  string   `json:"childConversationId"`
	InvocationID         string   `json:"invocationId"`
	ArtifactIDs          []string `json:"artifactIds"`
	NeedsYouIDs          []string `json:"needsYouIds"`
	CreatedAt            string   `json:"createdAt"`
	UpdatedAt            string   `json:"updatedAt"`
}

type ArtifactView struct {
	ID                 string `json:"id"`
	TaskID             string `json:"taskId"`
	Title              string `json:"title"`
	Kind               string `json:"kind"`
	Content            string `json:"content"`
	Status             string `json:"status"`
	Digest             string `json:"digest"`
	CreatedAt          string `json:"createdAt"`
	AcceptedAt         string `json:"acceptedAt,omitempty"`
	PublishedAt        string `json:"publishedAt,omitempty"`
	PublishedMessageID string `json:"publishedMessageId,omitempty"`
}

type NeedsYouView struct {
	ID         string `json:"id"`
	TaskID     string `json:"taskId"`
	ArtifactID string `json:"artifactId"`
	Kind       string `json:"kind"`
	Prompt     string `json:"prompt"`
	Status     string `json:"status"`
	CreatedAt  string `json:"createdAt"`
	ResolvedAt string `json:"resolvedAt,omitempty"`
}

type TaskPage struct {
	Tasks []TaskView `json:"tasks"`
}
type ArtifactPage struct {
	Artifacts []ArtifactView `json:"artifacts"`
}
type NeedsYouPage struct {
	NeedsYou []NeedsYouView `json:"needsYou"`
}

type ResolveNeedsYouInput struct {
	Decision string `json:"decision"`
}
type ResolveNeedsYouResult struct {
	NeedsYou NeedsYouView `json:"needsYou"`
	Task     TaskView     `json:"task"`
	Artifact ArtifactView `json:"artifact"`
	Replayed bool         `json:"replayed"`
}

type PublishArtifactInput struct{}

type PublishArtifactResult struct {
	Artifact ArtifactView `json:"artifact"`
	Message  MessageView  `json:"message"`
	Replayed bool         `json:"replayed"`
}

type artifactReferenceExtInfo struct {
	ArtifactID    string `json:"artifactId"`
	Digest        string `json:"digest"`
	MessageType   string `json:"messageType"`
	SchemaVersion int    `json:"schemaVersion"`
}

const (
	taskStatusWaitingReview = "waiting_for_review"
	taskStatusCompleted     = "completed"
	taskStatusRejected      = "rejected"
	artifactStatusDraft     = "draft"
	artifactStatusAccepted  = "accepted"
	artifactStatusRejected  = "rejected"
	needsYouStatusOpen      = "open"
	needsYouStatusResolved  = "resolved"
)

func (service *Service) materializeTaskOutcome(parentID, childID, invocationID, instruction, reply string) (TaskView, ArtifactView, NeedsYouView, error) {
	if service == nil || parentID == "" || childID == "" || invocationID == "" || instruction == "" || reply == "" {
		return TaskView{}, ArtifactView{}, NeedsYouView{}, ErrIntegrity
	}
	digest := sha256.Sum256([]byte("wanwork.local-demo-task/1\x00" + invocationID))
	taskID := "task_local_" + hex.EncodeToString(digest[:12])
	artifactDigest := sha256.Sum256([]byte("wanwork.local-demo-artifact/1\x00" + invocationID + "\x00" + reply))
	artifactID := "artifact_local_" + hex.EncodeToString(artifactDigest[:12])
	needsDigest := sha256.Sum256([]byte("wanwork.local-demo-needs-you/1\x00" + invocationID))
	needsID := "needs_local_" + hex.EncodeToString(needsDigest[:12])
	now := service.nowUTC().Format(time.RFC3339Nano)
	task := TaskView{ID: taskID, Title: taskTitle(instruction), Instruction: instruction, Status: taskStatusWaitingReview,
		ParentConversationID: parentID, ChildConversationID: childID, InvocationID: invocationID,
		ArtifactIDs: []string{artifactID}, NeedsYouIDs: []string{needsID}, CreatedAt: now, UpdatedAt: now}
	artifact := ArtifactView{ID: artifactID, TaskID: taskID, Title: "v0版 Agent 研究结果（草稿）", Kind: "research_markdown",
		Content: reply, Status: artifactStatusDraft, Digest: hex.EncodeToString(artifactDigest[:]), CreatedAt: now}
	needs := NeedsYouView{ID: needsID, TaskID: taskID, ArtifactID: artifactID, Kind: "artifact_acceptance",
		Prompt: "请审阅 Agent 生成的研究结果，确认后才可标记为正式产物。", Status: needsYouStatusOpen, CreatedAt: now}
	service.mu.Lock()
	defer service.mu.Unlock()
	if existing, ok := service.tasks[taskID]; ok {
		return existing, service.artifacts[artifactID], service.needsYou[needsID], nil
	}
	service.tasks[taskID] = task
	service.taskOrder = append(service.taskOrder, taskID)
	service.artifacts[artifactID] = artifact
	service.needsYou[needsID] = needs
	return task, artifact, needs, nil
}

func taskTitle(instruction string) string {
	trimmed := strings.TrimSpace(instruction)
	if len([]rune(trimmed)) > 28 {
		return string([]rune(trimmed)[:28]) + "…"
	}
	return trimmed
}

func (service *Service) ListTasks(ctx context.Context, bearerToken string) (TaskPage, error) {
	if service == nil || ctx == nil {
		return TaskPage{}, ErrInvalidInput
	}
	if err := service.verifyRequester(ctx, bearerToken); err != nil {
		return TaskPage{}, err
	}
	service.mu.Lock()
	defer service.mu.Unlock()
	items := make([]TaskView, 0, len(service.taskOrder))
	for _, id := range service.taskOrder {
		if task, ok := service.tasks[id]; ok {
			items = append(items, cloneTask(task))
		}
	}
	return TaskPage{Tasks: items}, nil
}

func (service *Service) ListArtifacts(ctx context.Context, bearerToken string) (ArtifactPage, error) {
	if service == nil || ctx == nil {
		return ArtifactPage{}, ErrInvalidInput
	}
	if err := service.verifyRequester(ctx, bearerToken); err != nil {
		return ArtifactPage{}, err
	}
	service.mu.Lock()
	defer service.mu.Unlock()
	items := make([]ArtifactView, 0, len(service.artifacts))
	for _, artifact := range service.artifacts {
		items = append(items, artifact)
	}
	sort.Slice(items, func(i, j int) bool {
		return items[i].CreatedAt < items[j].CreatedAt || (items[i].CreatedAt == items[j].CreatedAt && items[i].ID < items[j].ID)
	})
	return ArtifactPage{Artifacts: items}, nil
}

func (service *Service) ListNeedsYou(ctx context.Context, bearerToken string) (NeedsYouPage, error) {
	if service == nil || ctx == nil {
		return NeedsYouPage{}, ErrInvalidInput
	}
	if err := service.verifyRequester(ctx, bearerToken); err != nil {
		return NeedsYouPage{}, err
	}
	service.mu.Lock()
	defer service.mu.Unlock()
	items := make([]NeedsYouView, 0, len(service.needsYou))
	for _, needs := range service.needsYou {
		items = append(items, needs)
	}
	sort.Slice(items, func(i, j int) bool {
		return items[i].CreatedAt < items[j].CreatedAt || (items[i].CreatedAt == items[j].CreatedAt && items[i].ID < items[j].ID)
	})
	return NeedsYouPage{NeedsYou: items}, nil
}

func (service *Service) ResolveNeedsYou(ctx context.Context, bearerToken, needsID string, input ResolveNeedsYouInput) (ResolveNeedsYouResult, error) {
	if service == nil || ctx == nil || (input.Decision != "accept" && input.Decision != "reject") {
		return ResolveNeedsYouResult{}, ErrInvalidInput
	}
	if err := service.verifyRequester(ctx, bearerToken); err != nil {
		return ResolveNeedsYouResult{}, err
	}
	service.mu.Lock()
	defer service.mu.Unlock()
	needs, ok := service.needsYou[needsID]
	if !ok {
		return ResolveNeedsYouResult{}, ErrNotFound
	}
	artifact, ok := service.artifacts[needs.ArtifactID]
	if !ok {
		return ResolveNeedsYouResult{}, ErrIntegrity
	}
	task, ok := service.tasks[needs.TaskID]
	if !ok {
		return ResolveNeedsYouResult{}, ErrIntegrity
	}
	if needs.Status == needsYouStatusResolved {
		return ResolveNeedsYouResult{NeedsYou: needs, Task: task, Artifact: artifact, Replayed: true}, nil
	}
	now := service.nowUTC().Format(time.RFC3339Nano)
	needs.Status, needs.ResolvedAt = needsYouStatusResolved, now
	if input.Decision == "accept" {
		artifact.Status, artifact.AcceptedAt, task.Status = artifactStatusAccepted, now, taskStatusCompleted
	} else {
		artifact.Status, task.Status = artifactStatusRejected, taskStatusRejected
	}
	task.UpdatedAt = now
	service.needsYou[needs.ID], service.artifacts[artifact.ID], service.tasks[task.ID] = needs, artifact, task
	return ResolveNeedsYouResult{NeedsYou: needs, Task: task, Artifact: artifact}, nil
}

// PublishArtifact sends only a digest-bound reference to the parent conversation after a human
// has accepted the Artifact. The artifact body stays in the Workboard/authority projection; the
// parent room receives an explicit, idempotent publication notice rather than an implicit copy.
func (service *Service) PublishArtifact(
	ctx context.Context,
	bearerToken string,
	artifactID string,
	_ PublishArtifactInput,
) (PublishArtifactResult, error) {
	if service == nil || ctx == nil || !validLocalID(artifactID) {
		return PublishArtifactResult{}, ErrInvalidInput
	}
	if err := service.verifyRequester(ctx, bearerToken); err != nil {
		return PublishArtifactResult{}, err
	}
	service.mu.Lock()
	artifact, ok := service.artifacts[artifactID]
	if !ok {
		service.mu.Unlock()
		return PublishArtifactResult{}, ErrNotFound
	}
	if artifact.Status != artifactStatusAccepted {
		service.mu.Unlock()
		return PublishArtifactResult{}, ErrConflict
	}
	if artifact.PublishedMessageID != "" {
		service.mu.Unlock()
		return PublishArtifactResult{Artifact: artifact, Replayed: true}, nil
	}
	task, ok := service.tasks[artifact.TaskID]
	if !ok {
		service.mu.Unlock()
		return PublishArtifactResult{}, ErrIntegrity
	}
	parent, ok := service.conversations[parseConversationIDOrZero(task.ParentConversationID)]
	if !ok || parent.snapshot.ConversationType() != im.ConversationGroup || !service.canSend(parent) {
		service.mu.Unlock()
		return PublishArtifactResult{}, ErrForbidden
	}
	digest := sha256.Sum256([]byte("wanwork.local-demo-artifact-publication/1\x00" + artifact.ID + "\x00" + artifact.Digest))
	clientMessageID := "msg_artifact_" + hex.EncodeToString(digest[:12])
	extInfoBytes, err := json.Marshal(artifactReferenceExtInfo{
		ArtifactID: artifact.ID, Digest: artifact.Digest, MessageType: "artifact_reference", SchemaVersion: 1,
	})
	if err != nil {
		service.mu.Unlock()
		return PublishArtifactResult{}, ErrIntegrity
	}
	parentID := task.ParentConversationID
	service.mu.Unlock()

	messageResult, err := service.SendText(ctx, bearerToken, parentID, SendTextInput{
		ClientMessageID: clientMessageID,
		Text:            "已发布 Artifact 引用：" + artifact.ID,
		ExtInfo:         string(extInfoBytes),
	})
	if err != nil {
		return PublishArtifactResult{}, err
	}
	service.mu.Lock()
	defer service.mu.Unlock()
	artifact, ok = service.artifacts[artifactID]
	if !ok {
		return PublishArtifactResult{}, ErrIntegrity
	}
	if artifact.PublishedMessageID != "" {
		return PublishArtifactResult{Artifact: artifact, Message: messageResult.Message, Replayed: true}, nil
	}
	artifact.PublishedAt = service.nowUTC().Format(time.RFC3339Nano)
	artifact.PublishedMessageID = messageResult.Message.ID
	service.artifacts[artifactID] = artifact
	return PublishArtifactResult{Artifact: artifact, Message: messageResult.Message, Replayed: messageResult.Replayed}, nil
}

func parseConversationIDOrZero(value string) im.ConversationID {
	parsed, err := im.ParseConversationID(value)
	if err != nil {
		return im.ConversationID{}
	}
	return parsed
}

func cloneTask(task TaskView) TaskView {
	task.ArtifactIDs = append([]string(nil), task.ArtifactIDs...)
	task.NeedsYouIDs = append([]string(nil), task.NeedsYouIDs...)
	return task
}
