package receipt

import (
	"bytes"
	"encoding/json"
	"os"
	"path/filepath"
	"testing"
)

// FuzzCanonicalize checks the properties Go can check alone: no panic,
// and every accepted document canonicalizes to valid JSON that is its
// own canonical form. Agreement with the Python verifier is checked by
// verifier/tests/test_differential.py, which replays the corpus this
// target builds (testdata/fuzz/FuzzCanonicalize, committed). To grow it:
//
//	go test ./internal/receipt -run '^$' -fuzz FuzzCanonicalize -fuzztime 5m
//
// then commit new entries, and run the differential test on them.
func FuzzCanonicalize(f *testing.F) {
	raw, err := os.ReadFile(filepath.Join("..", "..", "..", "testdata", "canonical_vectors.json"))
	if err != nil {
		f.Fatalf("read vectors: %v", err)
	}
	var vf vectorFile
	if err := json.Unmarshal(raw, &vf); err != nil {
		f.Fatalf("parse vectors: %v", err)
	}
	for _, v := range vf.Vectors {
		f.Add([]byte(v.Input))
	}
	for _, s := range []string{`1E+2`, `-0`, `[[[]]]`, `{"":{}}`, `"\ud83d\ude00"`, "\"\u2028\"", `1 `, `{}]`} {
		f.Add([]byte(s))
	}

	f.Fuzz(func(t *testing.T, in []byte) {
		out, err := Canonicalize(in)
		if err != nil {
			return
		}
		if !json.Valid(out) {
			t.Fatalf("canonical form of %q is not valid JSON: %q", in, out)
		}
		again, err := Canonicalize(out)
		if err != nil {
			t.Fatalf("canonical form of %q is rejected: %q: %v", in, out, err)
		}
		if !bytes.Equal(out, again) {
			t.Fatalf("not a fixed point: %q -> %q -> %q", in, out, again)
		}
	})
}
