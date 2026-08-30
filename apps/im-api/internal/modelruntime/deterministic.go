package modelruntime

import "context"

type Deterministic struct{}

func NewDeterministic() Deterministic { return Deterministic{} }

func (Deterministic) Generate(ctx context.Context, request Request) (Result, error) {
	if ctx == nil {
		return Result{}, ErrInvalidRequest
	}
	if err := request.Validate(); err != nil {
		return Result{}, err
	}
	select {
	case <-ctx.Done():
		return Result{}, ctx.Err()
	default:
	}
	result := Result{
		Text:     "v0版研究 Agent 已在独立子群处理：" + request.Instruction,
		Provider: "local",
		Model:    "deterministic-fixture",
	}
	if err := result.Validate(); err != nil {
		return Result{}, err
	}
	return result, nil
}

func (Deterministic) Descriptor() Descriptor {
	return Descriptor{Mode: "synthetic", Provider: "local", Model: "deterministic-fixture", Status: "ready"}
}
