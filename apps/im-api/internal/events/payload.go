package events

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"io"
	"regexp"
	"strconv"
	"strings"
	"unicode/utf8"
)

const (
	maxInlinePayloadBytes = 256 * 1024
	maxJSONDepth          = 32
	maxPayloadObjectKeys  = 4096
	maxPayloadArrayItems  = 4096
	maxPayloadRefBytes    = 256
)

var (
	sha256DigestPattern = regexp.MustCompile(`^sha256:[0-9a-f]{64}$`)
	storageIDPattern    = regexp.MustCompile(`^[a-z][a-z0-9.-]*$`)
)

func NewInlinePayload(raw []byte) (Payload, error) {
	canonical, err := canonicalizeJSONObject(raw)
	if err != nil {
		return Payload{}, err
	}
	return Payload{
		kind:   PayloadInline,
		inline: canonical,
		digest: digestRawBytes(canonical),
	}, nil
}

func NewReferencedPayload(reference OpaquePayloadRef, digest SHA256Digest) (Payload, error) {
	if !storageIDPattern.MatchString(reference.Storage) ||
		!validOpaqueText(reference.ReferenceID, maxPayloadRefBytes) ||
		!sha256DigestPattern.MatchString(string(digest)) {
		return Payload{}, ErrInvalidPayload
	}
	return Payload{
		kind:      PayloadReference,
		reference: clonePayloadReference(&reference),
		digest:    digest,
	}, nil
}

func validatePayload(payload Payload) error {
	switch payload.kind {
	case PayloadInline:
		if payload.reference != nil || len(payload.inline) == 0 ||
			!sha256DigestPattern.MatchString(string(payload.digest)) ||
			digestRawBytes(payload.inline) != payload.digest {
			return ErrInvalidPayload
		}
		canonical, err := canonicalizeJSONObject(payload.inline)
		if err != nil || !bytes.Equal(canonical, payload.inline) {
			return ErrInvalidPayload
		}
	case PayloadReference:
		if len(payload.inline) != 0 || payload.reference == nil ||
			!storageIDPattern.MatchString(payload.reference.Storage) ||
			!validOpaqueText(payload.reference.ReferenceID, maxPayloadRefBytes) ||
			!sha256DigestPattern.MatchString(string(payload.digest)) {
			return ErrInvalidPayload
		}
	default:
		return ErrInvalidPayload
	}
	return nil
}

func canonicalizeJSONObject(raw []byte) ([]byte, error) {
	if len(raw) == 0 || len(raw) > maxInlinePayloadBytes {
		if len(raw) > maxInlinePayloadBytes {
			return nil, ErrPayloadTooLarge
		}
		return nil, ErrInvalidPayload
	}
	if !utf8.Valid(raw) {
		return nil, ErrInvalidPayload
	}
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.UseNumber()
	value, err := decodeStrictJSONValue(decoder, 0)
	if err != nil {
		return nil, err
	}
	if _, ok := value.(map[string]any); !ok {
		return nil, ErrInvalidPayload
	}
	if _, err := decoder.Token(); !errors.Is(err, io.EOF) {
		return nil, ErrInvalidPayload
	}
	var output bytes.Buffer
	encoder := json.NewEncoder(&output)
	encoder.SetEscapeHTML(false)
	if err := encoder.Encode(value); err != nil {
		return nil, ErrInvalidPayload
	}
	canonical := bytes.TrimSuffix(output.Bytes(), []byte("\n"))
	if len(canonical) > maxInlinePayloadBytes {
		return nil, ErrPayloadTooLarge
	}
	return cloneBytes(canonical), nil
}

func decodeStrictJSONValue(decoder *json.Decoder, depth int) (any, error) {
	if depth > maxJSONDepth {
		return nil, ErrInvalidPayload
	}
	token, err := decoder.Token()
	if err != nil {
		return nil, ErrInvalidPayload
	}
	switch value := token.(type) {
	case json.Delim:
		switch value {
		case '{':
			result := make(map[string]any)
			for decoder.More() {
				if len(result) >= maxPayloadObjectKeys {
					return nil, ErrInvalidPayload
				}
				keyToken, keyErr := decoder.Token()
				if keyErr != nil {
					return nil, ErrInvalidPayload
				}
				key, ok := keyToken.(string)
				if !ok || !utf8.ValidString(key) {
					return nil, ErrInvalidPayload
				}
				if _, exists := result[key]; exists {
					return nil, ErrInvalidPayload
				}
				child, childErr := decodeStrictJSONValue(decoder, depth+1)
				if childErr != nil {
					return nil, childErr
				}
				result[key] = child
			}
			end, endErr := decoder.Token()
			if endErr != nil || end != json.Delim('}') {
				return nil, ErrInvalidPayload
			}
			return result, nil
		case '[':
			result := make([]any, 0)
			for decoder.More() {
				if len(result) >= maxPayloadArrayItems {
					return nil, ErrInvalidPayload
				}
				child, childErr := decodeStrictJSONValue(decoder, depth+1)
				if childErr != nil {
					return nil, childErr
				}
				result = append(result, child)
			}
			end, endErr := decoder.Token()
			if endErr != nil || end != json.Delim(']') {
				return nil, ErrInvalidPayload
			}
			return result, nil
		default:
			return nil, ErrInvalidPayload
		}
	case json.Number:
		if strings.ContainsAny(string(value), ".eE") {
			return nil, ErrInvalidPayload
		}
		integer, parseErr := strconv.ParseInt(string(value), 10, 64)
		if parseErr != nil {
			return nil, ErrInvalidPayload
		}
		return integer, nil
	case string:
		if !utf8.ValidString(value) {
			return nil, ErrInvalidPayload
		}
		return value, nil
	case bool:
		return value, nil
	case nil:
		return nil, nil
	default:
		return nil, ErrInvalidPayload
	}
}

func digestBytes(domain string, value []byte) SHA256Digest {
	hash := sha256.New()
	_, _ = hash.Write([]byte(domain))
	_, _ = hash.Write(value)
	return SHA256Digest("sha256:" + hex.EncodeToString(hash.Sum(nil)))
}

func digestRawBytes(value []byte) SHA256Digest {
	hash := sha256.Sum256(value)
	return SHA256Digest("sha256:" + hex.EncodeToString(hash[:]))
}

func validOpaqueText(value string, maximum int) bool {
	if value == "" || len(value) > maximum || !utf8.ValidString(value) || strings.TrimSpace(value) != value {
		return false
	}
	for _, character := range value {
		if character < 0x21 || character == 0x7f {
			return false
		}
	}
	return true
}
