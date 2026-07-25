package receipt

import (
	"encoding/json"
	"os"
	"path/filepath"
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
