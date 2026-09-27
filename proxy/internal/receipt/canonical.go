// Package receipt implements vouch's receipt model: canonical JSON
// serialization and fact-carrying receipts. Signing is package sign.
//
// Canonicalize implements vouch canonical JSON v2, specified in
// docs/canonical-json.md and pinned, byte for byte against the Python
// verifier, by testdata/canonical_vectors.json. In short: keys sorted by
// code point, compact output, number literals copied exactly as written,
// and input with duplicate keys, invalid UTF-8, lone-surrogate escapes,
// or nesting past MaxDepth rejected rather than repaired. It is deliberately not RFC 8785 (JCS):
// JCS rewrites numbers as doubles, and a receipt must record the numbers
// a tool actually returned.
package receipt

import (
	"bytes"
	"encoding/json"
	"fmt"
	"sort"
	"unicode/utf16"
	"unicode/utf8"
)

// Canonicalize parses raw JSON and re-serializes it in canonical form.
// It returns an error on invalid JSON or on non-object/array/scalar roots
// that encoding/json cannot represent.
//
// It also rejects input that encoding/json would silently normalize:
// duplicate object keys (last one wins), invalid UTF-8, and \u escapes
// that are lone surrogates (both become U+FFFD). Normalizing would sign
// a document that differs in meaning from the one the agent received,
// so the call fails instead (invariant 2 in AGENTS.md).
func Canonicalize(raw []byte) ([]byte, error) {
	if err := checkStrings(raw); err != nil {
		return nil, err
	}
	dec := json.NewDecoder(bytes.NewReader(raw))
	dec.UseNumber()

	v, err := decodeValue(dec, 0)
	if err != nil {
		return nil, err
	}
	// Only whitespace may follow the value. dec.More() is not enough:
	// it reports false before a stray '}' or ']', so "0}" would pass.
	if rest := bytes.TrimLeft(raw[dec.InputOffset():], " \t\r\n"); len(rest) > 0 {
		return nil, fmt.Errorf("canonicalize: trailing data after JSON value")
	}

	var buf bytes.Buffer
	if err := writeCanonical(&buf, v); err != nil {
		return nil, err
	}
	return buf.Bytes(), nil
}

// MaxDepth is the deepest nesting of arrays and objects vouch canonical
// JSON accepts (docs/canonical-json.md rule 1). Without a shared limit
// the implementations disagree: encoding/json stops at 10000 levels and
// CPython's json recurses into a RecursionError near 1000 (#62).
const MaxDepth = 256

// decodeValue decodes one JSON value token by token, which, unlike
// Decode into map[string]any, sees every key of an object and so can
// reject duplicates. Keys are compared after unescaping: "a" and
// "\u0061" are the same key to any JSON consumer. depth is the number
// of containers enclosing the value.
func decodeValue(dec *json.Decoder, depth int) (any, error) {
	tok, err := dec.Token()
	if err != nil {
		return nil, fmt.Errorf("canonicalize: parse: %w", err)
	}
	switch t := tok.(type) {
	case json.Delim:
		if depth == MaxDepth {
			return nil, fmt.Errorf("canonicalize: nested deeper than %d levels", MaxDepth)
		}
		switch t {
		case '{':
			obj := make(map[string]any)
			for dec.More() {
				kt, err := dec.Token()
				if err != nil {
					return nil, fmt.Errorf("canonicalize: parse: %w", err)
				}
				k, ok := kt.(string)
				if !ok {
					return nil, fmt.Errorf("canonicalize: parse: object key is %T", kt)
				}
				if _, dup := obj[k]; dup {
					return nil, fmt.Errorf("canonicalize: duplicate key %q", k)
				}
				v, err := decodeValue(dec, depth+1)
				if err != nil {
					return nil, err
				}
				obj[k] = v
			}
			if _, err := dec.Token(); err != nil { // '}'
				return nil, fmt.Errorf("canonicalize: parse: %w", err)
			}
			return obj, nil
		case '[':
			arr := []any{}
			for dec.More() {
				v, err := decodeValue(dec, depth+1)
				if err != nil {
					return nil, err
				}
				arr = append(arr, v)
			}
			if _, err := dec.Token(); err != nil { // ']'
				return nil, fmt.Errorf("canonicalize: parse: %w", err)
			}
			return arr, nil
		default:
			return nil, fmt.Errorf("canonicalize: parse: unexpected %q", t)
		}
	default:
		return t, nil // string, json.Number, bool, or nil
	}
}

// checkStrings rejects invalid UTF-8 and lone-surrogate \u escapes in
// raw JSON. It tracks only enough lexical state to find string
// escapes; structural validity is left to the decoder.
func checkStrings(raw []byte) error {
	if !utf8.Valid(raw) {
		return fmt.Errorf("canonicalize: invalid UTF-8")
	}
	inString := false
	for i := 0; i < len(raw); i++ {
		c := raw[i]
		if !inString {
			if c == '"' {
				inString = true
			}
			continue
		}
		switch c {
		case '"':
			inString = false
		case '\\':
			if i+1 >= len(raw) {
				return nil // truncated; the decoder reports it
			}
			if raw[i+1] != 'u' {
				i++ // skip the escaped character, which may be '"' or '\\'
				continue
			}
			r, ok := hex4(raw, i+2)
			if !ok {
				return nil // malformed escape; the decoder reports it
			}
			i += 5
			switch {
			case utf16.IsSurrogate(r) && r < 0xdc00: // high: needs a low next
				lo, ok := rune(0), false
				if i+6 < len(raw) && raw[i+1] == '\\' && raw[i+2] == 'u' {
					lo, ok = hex4(raw, i+3)
				}
				if !ok || lo < 0xdc00 || lo > 0xdfff {
					return fmt.Errorf("canonicalize: lone surrogate \\u%04x in string", r)
				}
				i += 6
			case utf16.IsSurrogate(r):
				return fmt.Errorf("canonicalize: lone surrogate \\u%04x in string", r)
			}
		}
	}
	return nil
}

// hex4 parses the four hex digits of a \u escape starting at raw[i].
func hex4(raw []byte, i int) (rune, bool) {
	if i+4 > len(raw) {
		return 0, false
	}
	var r rune
	for _, c := range raw[i : i+4] {
		switch {
		case '0' <= c && c <= '9':
			r = r<<4 | rune(c-'0')
		case 'a' <= c && c <= 'f':
			r = r<<4 | rune(c-'a'+10)
		case 'A' <= c && c <= 'F':
			r = r<<4 | rune(c-'A'+10)
		default:
			return 0, false
		}
	}
	return r, true
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
