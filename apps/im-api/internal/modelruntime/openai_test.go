package modelruntime

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
)

func testRequest() Request {
	return Request{
		TenantID: "ten_local", WorkspaceID: "wsp_local", ParentConversation: "cnv_parent",
		ChildConversation: "cnv_child", InvocationID: "inv_local", AgentActorID: "agt_local",
		AgentVersion: "1.0.0", Instruction: "输出一份带证据的比较",
	}
}

func TestDeterministicRuntimeHonorsContextAndDescriptor(t *testing.T) {
	runtime := NewDeterministic()
	result, err := runtime.Generate(context.Background(), testRequest())
	if err != nil || !strings.Contains(result.Text, "输出一份带证据的比较") || result.Validate() != nil {
		t.Fatalf("deterministic result = %#v, %v", result, err)
	}
	if descriptor := runtime.Descriptor(); descriptor.Mode != "synthetic" || descriptor.Status != "ready" {
		t.Fatalf("deterministic descriptor = %#v", descriptor)
	}
	canceled, cancel := context.WithCancel(context.Background())
	cancel()
	if _, err := runtime.Generate(canceled, testRequest()); !errors.Is(err, context.Canceled) {
		t.Fatalf("canceled runtime error = %v", err)
	}
}

func TestFromEnvRequiresExplicitCompleteModelBundle(t *testing.T) {
	lookup := func(values map[string]string) LookupEnv {
		return func(name string) (string, bool) { value, ok := values[name]; return value, ok }
	}
	runtime, err := FromEnv(lookup(map[string]string{}))
	if err != nil || runtime.Descriptor().Mode != "synthetic" {
		t.Fatalf("default runtime = %#v, %v", runtime, err)
	}
	if _, err := FromEnv(lookup(map[string]string{RuntimeModeEnv: "openai-compatible", APIKeyEnv: "sk-test"})); !errors.Is(err, ErrConfiguration) {
		t.Fatalf("incomplete bundle error = %v", err)
	}
	if _, err := FromEnv(lookup(map[string]string{RuntimeModeEnv: "other"})); !errors.Is(err, ErrUnsupportedMode) {
		t.Fatalf("unsupported mode error = %v", err)
	}
}

func TestOpenAIParsesResponsesSSEWithoutLeakingConfiguration(t *testing.T) {
	server := httptest.NewTLSServer(http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
		if request.URL.Path != "/v1/responses" || request.Method != http.MethodPost ||
			request.Header.Get("Authorization") != "Bearer sk-test-secret" {
			t.Fatalf("request shape = %s %s auth=%q", request.Method, request.URL.Path, request.Header.Get("Authorization"))
		}
		var payload struct {
			Model  string `json:"model"`
			Stream bool   `json:"stream"`
			Input  []struct {
				Content []struct {
					Text string `json:"text"`
				} `json:"content"`
			} `json:"input"`
		}
		if err := json.NewDecoder(request.Body).Decode(&payload); err != nil || payload.Model != "gpt-test" || !payload.Stream ||
			len(payload.Input) != 1 || len(payload.Input[0].Content) != 1 || !strings.Contains(payload.Input[0].Content[0].Text, "用户任务数据") {
			t.Fatalf("request payload = %#v, err=%v", payload, err)
		}
		writer.Header().Set("Content-Type", "text/event-stream")
		writer.WriteHeader(http.StatusOK)
		_, _ = writer.Write([]byte("event: response.output_text.delta\ndata: {\"type\":\"response.output_text.delta\",\"delta\":\"# 结论\\n\"}\n\n"))
		_, _ = writer.Write([]byte("event: response.output_text.delta\ndata: {\"type\":\"response.output_text.delta\",\"delta\":\"已完成\"}\n\n"))
		_, _ = writer.Write([]byte("event: response.completed\ndata: {\"type\":\"response.completed\",\"response\":{\"id\":\"resp_test_1\"}}\n\n"))
	}))
	defer server.Close()
	runtime, err := NewOpenAI(OpenAIConfig{APIKey: "sk-test-secret", BaseURL: server.URL + "/v1", Model: "gpt-test"}, server.Client())
	if err != nil {
		t.Fatal(err)
	}
	result, err := runtime.Generate(context.Background(), testRequest())
	if err != nil || result.Text != "# 结论\n已完成" || result.ResponseID != "resp_test_1" || result.Provider != "openai-compatible" {
		t.Fatalf("SSE result = %#v, %v", result, err)
	}
	if descriptor := runtime.Descriptor(); descriptor.Model != "gpt-test" || descriptor.Status != "configured" {
		t.Fatalf("descriptor = %#v", descriptor)
	}
	if err := runtime.Close(); err != nil {
		t.Fatal(err)
	}
	if _, err := runtime.Generate(context.Background(), testRequest()); !errors.Is(err, ErrRuntimeClosed) {
		t.Fatalf("closed runtime error = %v", err)
	}
}

func TestOpenAIRejectsHTTPFailureAndOversizedResponse(t *testing.T) {
	server := httptest.NewTLSServer(http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
		if strings.HasPrefix(request.URL.Path, "/large/") {
			writer.Header().Set("Content-Type", "application/json")
			_, _ = writer.Write([]byte(`{"output_text":"0123456789"}`))
			return
		}
		writer.WriteHeader(http.StatusBadGateway)
		_, _ = writer.Write([]byte(`{"error":"secret must not be returned"}`))
	}))
	defer server.Close()
	runtime, err := NewOpenAI(OpenAIConfig{APIKey: "sk-test", BaseURL: server.URL, Model: "gpt-test", MaxResponseBytes: 8}, server.Client())
	if err != nil {
		t.Fatal(err)
	}
	if _, err := runtime.Generate(context.Background(), testRequest()); !errors.Is(err, ErrUnavailable) {
		t.Fatalf("HTTP failure = %v", err)
	}
	largeRuntime, err := NewOpenAI(OpenAIConfig{APIKey: "sk-test", BaseURL: server.URL + "/large", Model: "gpt-test", MaxResponseBytes: 8}, server.Client())
	if err != nil {
		t.Fatal(err)
	}
	if _, err := largeRuntime.Generate(context.Background(), testRequest()); !errors.Is(err, ErrResponseTooLarge) {
		t.Fatalf("oversized response = %v", err)
	}
}

func TestOpenAICloseCancelsInFlightRequest(t *testing.T) {
	started := make(chan struct{})
	doer := blockingDoer{started: started}
	runtime, err := NewOpenAI(OpenAIConfig{APIKey: "sk-test", BaseURL: "https://example.com/v1", Model: "gpt-test", Timeout: time.Minute}, doer)
	if err != nil {
		t.Fatal(err)
	}
	result := make(chan error, 1)
	go func() {
		_, callErr := runtime.Generate(context.Background(), testRequest())
		result <- callErr
	}()
	select {
	case <-started:
	case <-time.After(2 * time.Second):
		t.Fatal("model request did not start")
	}
	if err := runtime.Close(); err != nil {
		t.Fatal(err)
	}
	select {
	case callErr := <-result:
		if !errors.Is(callErr, context.Canceled) {
			t.Fatalf("in-flight close error = %v", callErr)
		}
	case <-time.After(2 * time.Second):
		t.Fatal("in-flight model request was not canceled")
	}
}

type blockingDoer struct {
	started chan struct{}
}

func (doer blockingDoer) Do(request *http.Request) (*http.Response, error) {
	close(doer.started)
	<-request.Context().Done()
	return nil, request.Context().Err()
}

func TestOpenAIParsesJSONContainingDataColon(t *testing.T) {
	result, err := parseResponse([]byte(`{"id":"resp_json_1","output_text":"data: this is plain JSON"}`), "application/json", "gpt-test")
	if err != nil || result.Text != "data: this is plain JSON" || result.ResponseID != "resp_json_1" {
		t.Fatalf("JSON result = %#v, %v", result, err)
	}
}

func TestOpenAIConfigRejectsUnsafeEndpoints(t *testing.T) {
	base := OpenAIConfig{APIKey: "sk-test", BaseURL: "https://example.com/v1", Model: "gpt-test"}
	unsafe := []OpenAIConfig{
		{APIKey: "", BaseURL: base.BaseURL, Model: base.Model},
		{APIKey: base.APIKey, BaseURL: "http://example.com/v1", Model: base.Model},
		{APIKey: base.APIKey, BaseURL: "https://example.com/v1/responses", Model: base.Model},
		{APIKey: base.APIKey, BaseURL: "https://user:pass@example.com/v1", Model: base.Model},
		{APIKey: base.APIKey, BaseURL: "https://example.com/v1?x=1", Model: base.Model},
		{APIKey: base.APIKey, BaseURL: base.BaseURL, Model: "bad model"},
		{APIKey: base.APIKey, BaseURL: base.BaseURL + " ", Model: base.Model},
	}
	for index, config := range unsafe {
		if err := config.Validate(); !errors.Is(err, ErrConfiguration) {
			t.Errorf("unsafe config %d error = %v", index, err)
		}
	}
}
