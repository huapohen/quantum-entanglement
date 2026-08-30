package authoritycutover

import (
	"bytes"
	"encoding/json"
	"errors"
	"io"
	"strconv"
	"strings"
	"unicode/utf8"

	"golang.org/x/text/unicode/norm"
)

const (
	maximumJSONDepth = 32
	maximumJSONKeys  = 4096
	maximumJSONItems = 4096
)

// DecodePlan performs a duplicate-aware structural pass before typed decoding, normalizes only
// declared semantic sets, recomputes all derived authority digests, and verifies the self-binding
// plan digest. It never performs database or filesystem I/O.
func DecodePlan(raw []byte) (Plan, error) {
	if len(raw) == 0 {
		return Plan{}, ErrInvalidPlan
	}
	if len(raw) > maximumPlanBytes {
		return Plan{}, ErrPlanTooLarge
	}
	if !utf8.Valid(raw) || !norm.NFC.IsNormal(raw) {
		return Plan{}, ErrInvalidPlan
	}
	structuralDecoder := json.NewDecoder(bytes.NewReader(raw))
	structuralDecoder.UseNumber()
	value, err := decodeStrictJSONValue(structuralDecoder, 0)
	if err != nil {
		return Plan{}, err
	}
	if _, object := value.(map[string]any); !object {
		return Plan{}, ErrInvalidPlan
	}
	if _, err := structuralDecoder.Token(); !errors.Is(err, io.EOF) {
		return Plan{}, ErrInvalidPlan
	}

	var snapshot PlanSnapshot
	typedDecoder := json.NewDecoder(bytes.NewReader(raw))
	typedDecoder.DisallowUnknownFields()
	if err := typedDecoder.Decode(&snapshot); err != nil {
		return Plan{}, ErrInvalidPlan
	}
	if err := typedDecoder.Decode(&struct{}{}); !errors.Is(err, io.EOF) {
		return Plan{}, ErrInvalidPlan
	}
	normalizePlan(&snapshot)
	return sealPlan(snapshot)
}

func decodeStrictJSONValue(decoder *json.Decoder, depth int) (any, error) {
	if depth > maximumJSONDepth {
		return nil, ErrInvalidPlan
	}
	token, err := decoder.Token()
	if err != nil {
		return nil, ErrInvalidPlan
	}
	switch value := token.(type) {
	case json.Delim:
		switch value {
		case '{':
			result := make(map[string]any)
			for decoder.More() {
				if len(result) >= maximumJSONKeys {
					return nil, ErrInvalidPlan
				}
				keyToken, keyErr := decoder.Token()
				if keyErr != nil {
					return nil, ErrInvalidPlan
				}
				key, ok := keyToken.(string)
				if !ok || !utf8.ValidString(key) || strings.ContainsRune(key, utf8.RuneError) ||
					!norm.NFC.IsNormalString(key) {
					return nil, ErrInvalidPlan
				}
				if _, duplicate := result[key]; duplicate {
					return nil, ErrInvalidPlan
				}
				child, childErr := decodeStrictJSONValue(decoder, depth+1)
				if childErr != nil {
					return nil, childErr
				}
				result[key] = child
			}
			end, endErr := decoder.Token()
			if endErr != nil || end != json.Delim('}') {
				return nil, ErrInvalidPlan
			}
			return result, nil
		case '[':
			result := make([]any, 0)
			for decoder.More() {
				if len(result) >= maximumJSONItems {
					return nil, ErrInvalidPlan
				}
				child, childErr := decodeStrictJSONValue(decoder, depth+1)
				if childErr != nil {
					return nil, childErr
				}
				result = append(result, child)
			}
			end, endErr := decoder.Token()
			if endErr != nil || end != json.Delim(']') {
				return nil, ErrInvalidPlan
			}
			return result, nil
		default:
			return nil, ErrInvalidPlan
		}
	case json.Number:
		if strings.ContainsAny(string(value), ".eE") {
			return nil, ErrInvalidPlan
		}
		integer, parseErr := strconv.ParseInt(string(value), 10, 64)
		if parseErr != nil {
			return nil, ErrInvalidPlan
		}
		return integer, nil
	case string:
		if !utf8.ValidString(value) || strings.ContainsRune(value, utf8.RuneError) ||
			!norm.NFC.IsNormalString(value) {
			return nil, ErrInvalidPlan
		}
		return value, nil
	case bool:
		return value, nil
	case nil:
		return nil, ErrInvalidPlan
	default:
		return nil, ErrInvalidPlan
	}
}
