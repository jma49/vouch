package receipt

import (
	"bytes"
	"encoding/json"
	"fmt"
	"reflect"
	"strings"
)

// DecodeStrict unmarshals data into v the way every reader of the same
// bytes will: encoding/json matches object keys to struct fields
// case-insensitively and keeps the last match, while the other readers
// of these documents (the Python verifier, MCP SDKs) match exactly. A
// document with both "arguments" and "Arguments" would then mean one
// thing to the proxy and another to the upstream, and a receipt could
// record a call that never ran (#98). So a key that differs from one of
// v's field names only in case is an error, at every level v's type
// reaches, and so are duplicate keys and anything else Canonicalize
// refuses. Keys v does not name are allowed: MCP messages grow fields.
func DecodeStrict(data []byte, v any) error {
	if _, err := Canonicalize(data); err != nil {
		return err
	}
	dec := json.NewDecoder(bytes.NewReader(data))
	dec.UseNumber()
	var tree any
	if err := dec.Decode(&tree); err != nil {
		return fmt.Errorf("decode: %w", err)
	}
	if err := checkKeys(tree, reflect.TypeOf(v), ""); err != nil {
		return err
	}
	return json.Unmarshal(data, v)
}

// ExactKeys reports an error unless the JSON object in data has exactly
// the given keys, for formats that allow nothing else (DSSE envelopes).
func ExactKeys(data []byte, keys ...string) error {
	var obj map[string]json.RawMessage
	if err := json.Unmarshal(data, &obj); err != nil {
		return fmt.Errorf("not a JSON object: %w", err)
	}
	want := make(map[string]bool, len(keys))
	for _, k := range keys {
		want[k] = true
	}
	for k := range obj {
		if !want[k] {
			return fmt.Errorf("unexpected key %q (want exactly %s)", k, strings.Join(keys, ", "))
		}
	}
	for _, k := range keys {
		if _, ok := obj[k]; !ok {
			return fmt.Errorf("missing key %q", k)
		}
	}
	return nil
}

var rawMessage = reflect.TypeOf(json.RawMessage(nil))

func checkKeys(value any, t reflect.Type, path string) error {
	for t != nil && t.Kind() == reflect.Pointer {
		t = t.Elem()
	}
	if t == nil || t == rawMessage {
		return nil
	}
	switch t.Kind() {
	case reflect.Struct:
		obj, ok := value.(map[string]any)
		if !ok {
			return nil // not an object; json.Unmarshal reports the type error
		}
		fields := jsonFields(t)
		for key, child := range obj {
			if ft, ok := fields[key]; ok {
				if err := checkKeys(child, ft, path+"/"+key); err != nil {
					return err
				}
				continue
			}
			for name := range fields {
				if strings.EqualFold(name, key) {
					return fmt.Errorf("key %q at %s differs from %q only in case", key, orRoot(path), name)
				}
			}
		}
	case reflect.Slice, reflect.Array:
		list, ok := value.([]any)
		if !ok {
			return nil
		}
		for i, child := range list {
			if err := checkKeys(child, t.Elem(), fmt.Sprintf("%s/%d", path, i)); err != nil {
				return err
			}
		}
	}
	return nil
}

// jsonFields maps each JSON key a struct type decodes to its field type,
// flattening embedded structs as encoding/json does.
func jsonFields(t reflect.Type) map[string]reflect.Type {
	out := make(map[string]reflect.Type)
	for i := 0; i < t.NumField(); i++ {
		f := t.Field(i)
		tag, _, _ := strings.Cut(f.Tag.Get("json"), ",")
		if tag == "-" || (!f.IsExported() && !f.Anonymous) {
			continue
		}
		if f.Anonymous && tag == "" {
			ft := f.Type
			if ft.Kind() == reflect.Pointer {
				ft = ft.Elem()
			}
			if ft.Kind() == reflect.Struct {
				for k, v := range jsonFields(ft) {
					out[k] = v
				}
				continue
			}
		}
		if tag == "" {
			tag = f.Name
		}
		out[tag] = f.Type
	}
	return out
}

func orRoot(path string) string {
	if path == "" {
		return "the top level"
	}
	return path
}
