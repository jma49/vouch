package receipt

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

type vectorFile struct {
	Vectors []struct {
		Name      string `json:"name"`
		Input     string `json:"input"`
		Canonical string `json:"canonical"`
	} `json:"vectors"`
}

func TestCanonicalizeVectors(t *testing.T) {
	raw, err := os.ReadFile(filepath.Join("..", "..", "..", "testdata", "canonical_vectors.json"))
	if err != nil {
		t.Fatalf("read vectors: %v", err)
	}
	var vf vectorFile
	if err := json.Unmarshal(raw, &vf); err != nil {
		t.Fatalf("parse vectors: %v", err)
	}
	if len(vf.Vectors) == 0 {
		t.Fatal("no vectors loaded")
	}
	for _, v := range vf.Vectors {
		t.Run(v.Name, func(t *testing.T) {
			got, err := Canonicalize([]byte(v.Input))
			if err != nil {
				t.Fatalf("Canonicalize: %v", err)
			}
			if string(got) != v.Canonical {
				t.Errorf("mismatch\n got: %s\nwant: %s", got, v.Canonical)
			}
		})
	}
}

func TestCanonicalizeIsIdempotent(t *testing.T) {
	in := []byte(`{"b": {"y": 2, "x": 1}, "a": [1, 2.5, "s"]}`)
	once, err := Canonicalize(in)
	if err != nil {
		t.Fatal(err)
	}
	twice, err := Canonicalize(once)
	if err != nil {
		t.Fatal(err)
	}
	if string(once) != string(twice) {
		t.Errorf("not idempotent:\n once: %s\ntwice: %s", once, twice)
	}
}

func TestCanonicalizeRejectsInvalid(t *testing.T) {
	for _, bad := range []string{"", "{", `{"a":1}garbage`} {
		if _, err := Canonicalize([]byte(bad)); err == nil {
			t.Errorf("expected error for input %q", bad)
		}
	}
}

// TestCanonicalizeRejectsAmbiguous pins that input whose canonical form
// would mean something other than what the agent received is rejected
// rather than normalized: duplicate keys (the receipt would keep one of
// two values the agent saw) and strings that are not valid Unicode (the
// receipt would hold U+FFFD where the agent got the original bytes).
func TestCanonicalizeRejectsAmbiguous(t *testing.T) {
	cases := []struct {
		name  string
		input string
		want  string
	}{
		{"duplicate key", `{"rsi_14":99,"rsi_14":10}`, "duplicate key"},
		{"nested duplicate key", `{"a":[{"x":1,"x":1}]}`, "duplicate key"},
		{"duplicate after unescaping", `{"a":1,"a":2}`, "duplicate key"},
		{"lone high surrogate", `{"s":"\ud800"}`, "surrogate"},
		{"lone low surrogate", `{"s":"\udc00"}`, "surrogate"},
		{"high surrogate then non-low escape", `{"s":"\ud800A"}`, "surrogate"},
		{"high surrogate then text", `{"s":"\ud800x"}`, "surrogate"},
		{"reversed pair", `{"s":"\ude00\ud83d"}`, "surrogate"},
		{"lone surrogate in key", `{"\udfff":1}`, "surrogate"},
		{"invalid utf-8", "{\"s\":\"\xff\"}", "UTF-8"},
		{"truncated utf-8", "{\"s\":\"\xe2\x9c\"}", "UTF-8"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got, err := Canonicalize([]byte(tc.input))
			if err == nil || !strings.Contains(err.Error(), tc.want) {
				t.Fatalf("got %q, %v; want error mentioning %q", got, err, tc.want)
			}
		})
	}
}

func TestCanonicalizeAcceptsLookalikes(t *testing.T) {
	cases := []struct {
		name, input, canonical string
	}{
		{"surrogate pair", `{"s":"😀"}`, `{"s":"😀"}`},
		{"escaped backslash before u", `{"s":"\\ud800"}`, `{"s":"\\ud800"}`},
		{"same key in sibling objects", `[{"a":1},{"a":2}]`, `[{"a":1},{"a":2}]`},
		{"same key at different depths", `{"a":{"a":1}}`, `{"a":{"a":1}}`},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got, err := Canonicalize([]byte(tc.input))
			if err != nil {
				t.Fatalf("Canonicalize: %v", err)
			}
			if string(got) != tc.canonical {
				t.Fatalf("got %s, want %s", got, tc.canonical)
			}
		})
	}
}
