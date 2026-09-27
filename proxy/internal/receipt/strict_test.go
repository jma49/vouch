package receipt

import (
	"encoding/json"
	"strings"
	"testing"
)

// TestDecodeStrictRejectsCaseVariants pins #98: a key that encoding/json
// would silently fold onto a field is refused, at any depth; exact and
// unknown keys decode as usual.
func TestDecodeStrictRejectsCaseVariants(t *testing.T) {
	type call struct {
		Name      string          `json:"name"`
		Arguments json.RawMessage `json:"arguments"`
	}
	var c call
	if err := DecodeStrict([]byte(`{"name":"t","arguments":{"A":1},"_meta":{},"task":{}}`), &c); err != nil || c.Name != "t" {
		t.Fatalf("exact keys: %+v, %v", c, err)
	}
	for _, bad := range []string{
		`{"name":"t","arguments":{},"Arguments":{"x":1}}`,
		`{"Name":"t","name":"u"}`,
		`{"NAME":"t"}`,
		`{"name":"t","name":"u"}`,
	} {
		if err := DecodeStrict([]byte(bad), &c); err == nil {
			t.Errorf("accepted %s", bad)
		}
	}
	// Nested structs and slices are checked too, including embedded ones.
	var r Receipt
	body := `{"receipt_id":"r","facts":[{"entity":"NVDA","Entity":"AMD","metric":"m","value":1}]}`
	if err := DecodeStrict([]byte(body), &r); err == nil || !strings.Contains(err.Error(), "/facts/0") {
		t.Fatalf("nested case variant: %v", err)
	}
	if err := DecodeStrict([]byte(`{"seq":1,"SEQ":2}`), &r); err == nil {
		t.Fatal("embedded field case variant accepted")
	}
}

func TestExactKeys(t *testing.T) {
	if err := ExactKeys([]byte(`{"a":1,"b":2}`), "a", "b"); err != nil {
		t.Fatal(err)
	}
	for _, bad := range []string{`{"a":1}`, `{"a":1,"b":2,"c":3}`, `{"a":1,"B":2}`, `[1]`} {
		if err := ExactKeys([]byte(bad), "a", "b"); err == nil {
			t.Errorf("accepted %s", bad)
		}
	}
}
