// Package modelruntime defines the narrow model execution port used by the IM Agent thread.
// The package owns no tenant authorization, conversation membership, provider transport, or
// long-lived secret. Callers must complete those checks before invoking a runtime.
package modelruntime

import (
	"context"
	"errors"
	"regexp"
	"strings"
	"unicode"
	"unicode/utf8"

	"golang.org/x/text/unicode/norm"
)

var (
	ErrInvalidRequest     = errors.New("invalid model runtime request")
	ErrConfiguration      = errors.New("model runtime configuration is invalid")
	ErrUnavailable        = errors.New("model runtime unavailable")
	ErrProtocol           = errors.New("model runtime response violated protocol")
	ErrResponseTooLarge   = errors.New("model runtime response exceeded limit")
	ErrOutputInvalid      = errors.New("model runtime output is invalid")
	ErrRuntimeClosed      = errors.New("model runtime is closed")
	ErrUnsupportedMode    = errors.New("model runtime mode is unsupported")
	modelIDPattern        = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$`)
	conversationIDPattern = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$`)
	invocationIDPattern   = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$`)
)

const (
	DefaultMaxOutputBytes = 64 * 1024
	MaxInstructionBytes   = 16 * 1024
	MaxOutputBytes        = 4 * 1024 * 1024
)

// Request is the minimum recorded context a runtime needs to generate a child-thread reply.
// Instruction is untrusted user input; the runtime must not treat it as a system instruction.
type Request struct {
	TenantID           string
	WorkspaceID        string
	ParentConversation string
	ChildConversation  string
	InvocationID       string
	AgentActorID       string
	AgentVersion       string
	Instruction        string
}

func (request Request) Validate() error {
	if !conversationIDPattern.MatchString(request.TenantID) ||
		!conversationIDPattern.MatchString(request.WorkspaceID) ||
		!conversationIDPattern.MatchString(request.ParentConversation) ||
		!conversationIDPattern.MatchString(request.ChildConversation) ||
		!invocationIDPattern.MatchString(request.InvocationID) ||
		!conversationIDPattern.MatchString(request.AgentActorID) ||
		!modelIDPattern.MatchString(request.AgentVersion) ||
		!validText(request.Instruction, MaxInstructionBytes) {
		return ErrInvalidRequest
	}
	return nil
}

func validText(value string, maxBytes int) bool {
	return value != "" && value == strings.TrimSpace(value) && len(value) <= maxBytes &&
		!strings.ContainsAny(value, "\x00\r\n")
}

func validOutput(value string, maxBytes int) bool {
	if value == "" || value != strings.TrimSpace(value) || len(value) > maxBytes ||
		!utf8.ValidString(value) || !norm.NFC.IsNormalString(value) {
		return false
	}
	for _, character := range value {
		if unicode.IsControl(character) && character != '\n' && character != '\r' && character != '\t' {
			return false
		}
	}
	return true
}

// Result contains untrusted model output and non-secret diagnostic identity. ResponseID is
// optional and must never be used as an authorization or idempotency key by callers.
type Result struct {
	Text       string
	Provider   string
	Model      string
	ResponseID string
}

func (result Result) Validate() error {
	if !validOutput(result.Text, MaxOutputBytes) {
		return ErrOutputInvalid
	}
	if result.Provider != "" && !modelIDPattern.MatchString(result.Provider) {
		return ErrOutputInvalid
	}
	if result.Model != "" && !modelIDPattern.MatchString(result.Model) {
		return ErrOutputInvalid
	}
	if result.ResponseID != "" && !modelIDPattern.MatchString(result.ResponseID) {
		return ErrOutputInvalid
	}
	return nil
}

type Descriptor struct {
	Mode     string `json:"mode"`
	Provider string `json:"provider"`
	Model    string `json:"model"`
	Status   string `json:"status"`
}

// Runtime is intentionally synchronous at the port boundary. Implementations must honor
// context cancellation and must not perform side effects other than the model request.
type Runtime interface {
	Generate(context.Context, Request) (Result, error)
	Descriptor() Descriptor
}
