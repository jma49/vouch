// Package receipt implements vouch's receipt model: canonical JSON
// serialization, fact-carrying receipts, and HMAC signing.
//
// Canonicalization contract (seed version):
//   - object keys sorted lexicographically, recursively
//   - compact output: no insignificant whitespace
//   - UTF-8 passthrough: no HTML escaping, no \uXXXX for printable unicode
//   - number literals preserved as they appeared in the source document
//
// Preserving source number literals keeps digests stable without a full
// number-normalization pass. Full RFC 8785 (JCS) number handling is a
// tracked follow-up; until then, cross-language behavior is pinned by the
// shared vectors in testdata/canonical_vectors.json.
package receipt

import (
	"bytes"
	"encoding/json"
	"fmt"
	"sort"
)

// Canonicalize parses raw JSON and re-serializes it in canonical form.
// It returns an error on invalid JSON or on non-object/array/scalar roots
// that encoding/json cannot represent.
func Canonicalize(raw []byte) ([]byte, error) {
	dec := json.NewDecoder(bytes.NewReader(raw))
	dec.UseNumber()

	var v any
	if err := dec.Decode(&v); err != nil {
		return nil, fmt.Errorf("canonicalize: parse: %w", err)
	}
	if dec.More() {
		return nil, fmt.Errorf("canonicalize: trailing data after JSON value")
	}

	var buf bytes.Buffer
	if err := writeCanonical(&buf, v); err != nil {
		return nil, err
	}
	return buf.Bytes(), nil
}

func writeCanonical(buf *bytes.Buffer, v any) error {
	switch t := v.(type) {
	case nil:
		buf.WriteString("null")
	case bool:
		if t {
			buf.WriteString("true")
		} else {
			buf.WriteString("false")
		}
	case json.Number:
		buf.WriteString(t.String())
	case string:
		return writeString(buf, t)
	case []any:
		buf.WriteByte('[')
		for i, elem := range t {
			if i > 0 {
				buf.WriteByte(',')
			}
			if err := writeCanonical(buf, elem); err != nil {
				return err
			}
		}
		buf.WriteByte(']')
	case map[string]any:
		keys := make([]string, 0, len(t))
		for k := range t {
			keys = append(keys, k)
		}
		sort.Strings(keys)
		buf.WriteByte('{')
		for i, k := range keys {
			if i > 0 {
				buf.WriteByte(',')
			}
			if err := writeString(buf, k); err != nil {
				return err
			}
			buf.WriteByte(':')
			if err := writeCanonical(buf, t[k]); err != nil {
				return err
			}
		}
		buf.WriteByte('}')
	default:
		return fmt.Errorf("canonicalize: unsupported type %T", v)
	}
	return nil
}

// writeString emits a JSON string without HTML escaping, matching Python's
// json.dumps(..., ensure_ascii=False) for printable input.
func writeString(buf *bytes.Buffer, s string) error {
	var tmp bytes.Buffer
	enc := json.NewEncoder(&tmp)
	enc.SetEscapeHTML(false)
	if err := enc.Encode(s); err != nil {
		return fmt.Errorf("canonicalize: string encode: %w", err)
	}
	b := tmp.Bytes()
	// json.Encoder appends a trailing newline; strip it.
	buf.Write(bytes.TrimRight(b, "\n"))
	return nil
}
