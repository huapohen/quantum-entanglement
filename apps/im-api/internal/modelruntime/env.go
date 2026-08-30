package modelruntime

import "strings"

const (
	RuntimeModeEnv = "WANWORK_IM_AGENT_RUNTIME"
	APIKeyEnv      = "WANWORK_IM_MODEL_API_KEY"
	BaseURLEnv     = "WANWORK_IM_MODEL_BASE_URL"
	ModelEnv       = "WANWORK_IM_MODEL"
)

type LookupEnv func(string) (string, bool)

// FromEnv composes the runtime only when the mode is explicitly selected. An absent mode always
// returns the deterministic runtime, so adding credentials to a shell cannot silently spend model
// budget or create outbound traffic.
func FromEnv(lookup LookupEnv) (Runtime, error) {
	if lookup == nil {
		return nil, ErrConfiguration
	}
	mode, _ := lookup(RuntimeModeEnv)
	mode = strings.TrimSpace(mode)
	switch mode {
	case "", "synthetic":
		return NewDeterministic(), nil
	case "openai-compatible":
		key, keySet := lookup(APIKeyEnv)
		baseURL, baseSet := lookup(BaseURLEnv)
		model, modelSet := lookup(ModelEnv)
		if !keySet || !baseSet || !modelSet {
			return nil, ErrConfiguration
		}
		return NewOpenAI(OpenAIConfig{APIKey: key, BaseURL: baseURL, Model: model}, nil)
	default:
		return nil, ErrUnsupportedMode
	}
}
