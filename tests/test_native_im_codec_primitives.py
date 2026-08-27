from __future__ import annotations

import hashlib
import json
import unicodedata

import pytest

from quantum_entanglement._native_im_codec import (
    MAX_SIGNED_64,
    MIN_SIGNED_64,
    NativeIMCodecTooLargeError,
    _boolean,
    _canonical_json_bytes,
    _decode_json_bytes,
    _digest,
    _display_text,
    _enum,
    _id,
    _integer,
    _media_type,
    _message_text,
    _model_digest,
    _non_negative_integer,
    _ordered_unique_text,
    _plain_dict,
    _plain_list,
    _positive_integer,
    _schema_version,
    _timestamp,
    _traceparent,
)


def test_plain_containers_and_schema_version_are_exact() -> None:
    assert _plain_dict({"schemaVersion": 1}, {"schemaVersion"}, "model") == {"schemaVersion": 1}
    assert _plain_list([1], "items", maximum_items=1) == [1]
    assert _schema_version(1) == 1

    class DictSubclass(dict[str, object]):
        pass

    class ListSubclass(list[object]):
        pass

    for value in (DictSubclass(schemaVersion=1), {1: "value"}, {"future": 1}):
        with pytest.raises((TypeError, ValueError)):
            _plain_dict(value, {"schemaVersion"}, "model")
    for value in (ListSubclass([1]), (1,)):
        with pytest.raises(TypeError):
            _plain_list(value, "items", maximum_items=1)
    with pytest.raises(NativeIMCodecTooLargeError):
        _plain_list([1, 2], "items", maximum_items=1)
    for value in (True, 0, 2):
        with pytest.raises((TypeError, ValueError)):
            _schema_version(value)


def test_text_policies_reject_non_nfc_surrogates_and_forbidden_controls() -> None:
    decomposed = unicodedata.normalize("NFD", "résumé")
    assert _id("opaque id", "id") == "opaque id"
    assert _display_text("", "display") == ""
    assert _message_text("line one\n\tline two", "text") == "line one\n\tline two"

    for value in ("", decomposed, "bad\x00id", "bad\nid", "bad\x7fid", "\ud800"):
        with pytest.raises(ValueError):
            _id(value, "id")
    for value in (decomposed, "bad\tdisplay", "bad\ndisplay", "\ud800"):
        with pytest.raises(ValueError):
            _display_text(value, "display")
    for value in (decomposed, "bad\rtext", "bad\x00text", "bad\x7ftext", "\ud800"):
        with pytest.raises(ValueError):
            _message_text(value, "text")
    with pytest.raises(ValueError):
        _message_text("", "text", allow_empty=False)
    with pytest.raises(NativeIMCodecTooLargeError):
        _id("a" * 4_097, "id")


def test_integer_boolean_timestamp_digest_traceparent_and_media_type_are_canonical() -> None:
    assert _integer(MIN_SIGNED_64, "integer") == MIN_SIGNED_64
    assert _integer(MAX_SIGNED_64, "integer") == MAX_SIGNED_64
    assert _non_negative_integer(0, "integer") == 0
    assert _positive_integer(1, "integer") == 1
    assert _boolean(False, "flag") is False
    assert _timestamp("2026-08-28T00:00:00.000001Z", "time").endswith("Z")
    assert _digest("a" * 64, "digest") == "a" * 64
    assert (
        _traceparent("00-0123456789abcdef0123456789abcdef-0123456789abcdef-01", "trace")
        == "00-0123456789abcdef0123456789abcdef-0123456789abcdef-01"
    )
    assert _media_type("application/vnd.test+json", "media") == "application/vnd.test+json"
    assert _enum("human", {"human", "agent"}, "kind") == "human"

    for value in (True, 1.0, MIN_SIGNED_64 - 1, MAX_SIGNED_64 + 1):
        with pytest.raises((TypeError, ValueError)):
            _integer(value, "integer")
    for value in (-1, True):
        with pytest.raises((TypeError, ValueError)):
            _non_negative_integer(value, "integer")
    for value in (0, -1, True):
        with pytest.raises((TypeError, ValueError)):
            _positive_integer(value, "integer")
    for value in (0, 1, "true"):
        with pytest.raises(TypeError):
            _boolean(value, "flag")
    for value in (
        "2026-08-28T00:00:00Z",
        "2026-08-28T00:00:00.000001+00:00",
        "2026-02-30T00:00:00.000001Z",
    ):
        with pytest.raises(ValueError):
            _timestamp(value, "time")
    for value in ("A" * 64, "sha256:" + "a" * 64, "a" * 63):
        with pytest.raises(ValueError):
            _digest(value, "digest")
    for value in (
        "00-00000000000000000000000000000000-0123456789abcdef-01",
        "00-0123456789abcdef0123456789abcdef-0000000000000000-01",
        "00-0123456789ABCDEF0123456789ABCDEF-0123456789abcdef-01",
        "01-0123456789abcdef0123456789abcdef-0123456789abcdef-01",
    ):
        with pytest.raises(ValueError):
            _traceparent(value, "trace")
    for value in ("Text/Plain", "text/plain; charset=utf-8", "text /plain", "text/plain/"):
        with pytest.raises(ValueError):
            _media_type(value, "media")
    with pytest.raises(ValueError):
        _enum("future", {"human", "agent"}, "kind")


def test_utf8_lexical_order_is_distinct_from_locale_or_input_order() -> None:
    values = tuple(sorted(("z", "é"), key=lambda item: item.encode("utf-8")))
    assert _ordered_unique_text(values, "values") == values
    with pytest.raises(ValueError):
        _ordered_unique_text(tuple(reversed(values)), "values")
    with pytest.raises(ValueError):
        _ordered_unique_text(("same", "same"), "values")


def test_canonical_json_and_model_digest_use_the_frozen_domain() -> None:
    body = {"schemaVersion": 1, "text": "résumé\nline", "value": 7}
    canonical = b'{"schemaVersion":1,"text":"r\xc3\xa9sum\xc3\xa9\\nline","value":7}'
    assert _canonical_json_bytes(body) == canonical
    expected = hashlib.sha256(
        b"quantum-entanglement.native-im/ExampleModelV1/1\n" + canonical
    ).hexdigest()
    assert _model_digest("ExampleModelV1", body) == expected
    assert expected != hashlib.sha256(canonical).hexdigest()


def test_json_decoder_accepts_semantic_whitespace_but_rejects_unsafe_json() -> None:
    decoded = _decode_json_bytes(
        b'{ "schemaVersion" : 1, "nested" : {"value": true} }',
        "model",
        maximum_bytes=1_024,
    )
    assert decoded == {"schemaVersion": 1, "nested": {"value": True}}

    for encoded in (
        b'{"schemaVersion":1,"schemaVersion":1}',
        b'{"nested":{"value":1,"value":2}}',
        b'{"value":NaN}',
        b'{"value":Infinity}',
        b"\xff",
        b'{"value":',
    ):
        with pytest.raises(ValueError):
            _decode_json_bytes(encoded, "model", maximum_bytes=1_024)
    with pytest.raises(TypeError):
        _decode_json_bytes(bytearray(b"{}"), "model", maximum_bytes=1_024)
    with pytest.raises(NativeIMCodecTooLargeError):
        _decode_json_bytes(b"{}", "model", maximum_bytes=1)


def test_canonical_json_remains_plain_json_and_does_not_mutate_input() -> None:
    body = {"items": [1, True, None], "nested": {"value": "text"}}
    before = json.loads(json.dumps(body))
    encoded = _canonical_json_bytes(body)
    assert json.loads(encoded) == body
    assert body == before
