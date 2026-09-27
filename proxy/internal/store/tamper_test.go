package store

import (
	"bytes"
	"encoding/base64"
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/jma49/vouch/proxy/internal/sign"
	"github.com/jma49/vouch/proxy/internal/sign/signtest"
)

// sealedLines builds a log of three receipts and a checkpoint and
// returns its lines and head digest.
func sealedLines(t *testing.T) ([][]byte, string) {
	t.Helper()
	path := filepath.Join(t.TempDir(), "source.jsonl")
	l, err := Open(path, signer)
	if err != nil {
		t.Fatal(err)
	}
	for turn := 0; turn < 3; turn++ {
		if err := l.Append(testReceipt(t, "s1", turn)); err != nil {
			t.Fatal(err)
		}
	}
	head, err := l.Seal("s1", time.Date(2026, 7, 25, 2, 0, 0, 0, time.UTC))
	if err != nil {
		t.Fatal(err)
	}
	l.Close()
	raw, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	return bytes.Split(bytes.TrimSuffix(raw, []byte("\n")), []byte("\n")), head
}

// editPayload rewrites one envelope's payload without re-signing.
func editPayload(t *testing.T, line []byte, edit func(string) string) []byte {
	t.Helper()
	var env sign.Envelope
	if err := json.Unmarshal(line, &env); err != nil {
		t.Fatal(err)
	}
	body, err := base64.StdEncoding.DecodeString(env.Payload)
	if err != nil {
		t.Fatal(err)
	}
	env.Payload = base64.StdEncoding.EncodeToString([]byte(edit(string(body))))
	out, err := json.Marshal(env)
	if err != nil {
		t.Fatal(err)
	}
	return out
}

// resign re-signs one line's (possibly edited) payload with key: what
// someone holding a signing key, but not the chain, could do.
func resign(t *testing.T, line []byte, key *sign.Signer, edit func(string) string) []byte {
	t.Helper()
	var env sign.Envelope
	if err := json.Unmarshal(line, &env); err != nil {
		t.Fatal(err)
	}
	body, err := sign.Decode(env)
	if err != nil {
		t.Fatal(err)
	}
	out, err := json.Marshal(key.Sign(env.PayloadType, []byte(edit(string(body)))))
	if err != nil {
		t.Fatal(err)
	}
	return out
}

func identity(s string) string { return s }

// TestVerifyDetectsTampering is the Go half of the tamper suite (#55):
// each way of altering a log must be caught by Verify, or be the one
// case only an external head can catch.
func TestVerifyDetectsTampering(t *testing.T) {
	lines, head := sealedLines(t)
	r0, r1, r2, cp := lines[0], lines[1], lines[2], lines[3]
	keys := signtest.Keyring(signer)
	other := signtest.Signer(9)

	cases := []struct {
		name  string
		lines [][]byte
		want  string // substring of Verify's error
	}{
		{"delete a middle receipt", [][]byte{r0, r2, cp}, "chain broken"},
		{"reorder receipts", [][]byte{r1, r0, r2, cp}, "chain broken"},
		{"duplicate a receipt", [][]byte{r0, r1, r1, r2, cp}, "chain broken"},
		{"delete the first receipt", [][]byte{r1, r2, cp}, "chain broken"},
		{"edit a fact", [][]byte{r0, editPayload(t, r1, func(b string) string {
			return strings.Replace(b, "62.3", "68.1", 1)
		}), r2, cp}, "no valid signature"},
		{"sign with an unknown key", [][]byte{r0, resign(t, r1, other, identity), r2, cp}, "no valid signature"},
		{"re-sign an edit with the right key but a broken link", [][]byte{r0, resign(t, r1, signer, func(b string) string {
			return strings.Replace(b, `"seq":1`, `"seq":7`, 1)
		}), r2, cp}, "chain broken"},
		{"miscount a checkpoint", [][]byte{r0, r1, r2, resign(t, cp, signer, func(b string) string {
			return strings.Replace(b, `"receipts":3`, `"receipts":2`, 1)
		})}, "checkpoint 3 counts 2 receipts"},
		{"swap in a checkpoint's signature", [][]byte{r0, r1, r2, func() []byte {
			var a, b sign.Envelope
			_ = json.Unmarshal(cp, &a)
			_ = json.Unmarshal(r0, &b)
			a.Signatures = b.Signatures
			out, _ := json.Marshal(a)
			return out
		}()}, "no valid signature"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			path := filepath.Join(t.TempDir(), "receipts.jsonl")
			if err := os.WriteFile(path, append(bytes.Join(tc.lines, []byte("\n")), '\n'), 0o644); err != nil {
				t.Fatal(err)
			}
			if _, err := Verify(path, keys); err == nil || !strings.Contains(err.Error(), tc.want) {
				t.Fatalf("Verify error = %v, want %q", err, tc.want)
			}
		})
	}

	// Truncating the tail leaves a valid chain; only sealing and an
	// external head show it.
	t.Run("truncate the tail", func(t *testing.T) {
		path := filepath.Join(t.TempDir(), "receipts.jsonl")
		if err := os.WriteFile(path, append(bytes.Join([][]byte{r0, r1}, []byte("\n")), '\n'), 0o644); err != nil {
			t.Fatal(err)
		}
		a, err := Verify(path, keys)
		if err != nil {
			t.Fatal(err) // a prefix of a valid chain is a valid chain
		}
		if a.Sealed || a.Head == head {
			t.Fatalf("truncated log must be unsealed with a different head: %+v", a)
		}
	})
}
