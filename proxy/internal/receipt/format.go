package receipt

import (
	"encoding/json"
	"fmt"
	"reflect"
	"sort"
	"strings"
)

// Keys returns the JSON keys of a receipt body, a fact, or a checkpoint
// body (v is a Receipt, Fact, or Checkpoint), split by whether the
// writer always emits them: a field without omitempty is required, one
// with it is optional. docs/receipt-format.md specifies the same split,
// and a test holds the two together.
func Keys(v any) (required, optional []string) {
	t := reflect.TypeOf(v)
	for t.Kind() == reflect.Pointer {
		t = t.Elem()
	}
	var walk func(reflect.Type)
	walk = func(t reflect.Type) {
		for i := 0; i < t.NumField(); i++ {
			f := t.Field(i)
			name, opts, _ := strings.Cut(f.Tag.Get("json"), ",")
			if f.Anonymous && name == "" && f.Type.Kind() == reflect.Struct {
				walk(f.Type)
				continue
			}
			if name == "" || name == "-" || !f.IsExported() {
				continue
			}
			if strings.Contains(opts, "omitempty") {
				optional = append(optional, name)
			} else {
				required = append(required, name)
			}
		}
	}
	walk(t)
	sort.Strings(required)
	sort.Strings(optional)
	return required, optional
}

// RequirePresent reports an error unless the JSON object in data has
// every key Keys(v) calls required, and, for a receipt body, unless
// every fact does too. Without it, a signed body missing a field was
// read as a zero value by one verifier and rejected by the other, and a
// third party had no rule to follow (#124).
func RequirePresent(data []byte, v any) error {
	var obj map[string]json.RawMessage
	if err := json.Unmarshal(data, &obj); err != nil {
		return fmt.Errorf("not a JSON object: %w", err)
	}
	if err := present(obj, v, ""); err != nil {
		return err
	}
	if _, ok := v.(Receipt); !ok {
		if _, ok := v.(*Receipt); !ok {
			return nil
		}
	}
	var facts []map[string]json.RawMessage
	if err := json.Unmarshal(obj["facts"], &facts); err != nil {
		return fmt.Errorf("facts: %w", err)
	}
	for i, f := range facts {
		if err := present(f, Fact{}, fmt.Sprintf("fact %d: ", i)); err != nil {
			return err
		}
	}
	return nil
}

func present(obj map[string]json.RawMessage, v any, where string) error {
	required, _ := Keys(v)
	for _, k := range required {
		if _, ok := obj[k]; !ok {
			return fmt.Errorf("%smissing required key %q", where, k)
		}
	}
	return nil
}
