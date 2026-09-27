package store

import (
	"bytes"
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/jma49/vouch/proxy/internal/receipt"
	"github.com/jma49/vouch/proxy/internal/sign/signtest"
)

var signer = signtest.Signer(1)

func testReceipt(t *testing.T, session string, turn int) *receipt.Receipt {
	t.Helper()
	result := json.RawMessage(`{"symbol":"NVDA","rsi_14":62.3}`)
	canon, err := receipt.Canonicalize(result)
	if err != nil {
		t.Fatal(err)
	}
	r := &receipt.Receipt{
		ReceiptID:       "r-" + session + "-" + string(rune('0'+turn)),
		SessionID:       session,
		TurnIndex:       turn,
		ToolName:        "get_indicators",
		ArgsCanonical:   json.RawMessage(`{"symbol":"NVDA"}`),
		ResultCanonical: canon,
		ResultDigest:    receipt.Digest(canon),
		WallTime:        time.Date(2026, 7, 25, 1, 12, 9, 0, time.UTC),
	}
	return r
}

func TestAppendAndScan(t *testing.T) {
	path := filepath.Join(t.TempDir(), "receipts.jsonl")
	l, err := Open(path, signer)
	if err != nil {
		t.Fatal(err)
	}
	if err := l.Append(testReceipt(t, "s1", 0)); err != nil {
		t.Fatal(err)
	}
	if err := l.Append(testReceipt(t, "s1", 1)); err != nil {
		t.Fatal(err)
	}
	if err := l.Close(); err != nil {
		t.Fatal(err)
	}

	got, err := ScanVerified(path, signtest.Keyring(signer))
	if err != nil {
		t.Fatal(err)
	}
	if len(got) != 2 {
		t.Fatalf("scan: got %d receipts, want 2", len(got))
	}
	if _, err := ScanVerified(path, signtest.Keyring(signtest.Signer(2))); err == nil {
		t.Fatal("a log verified under a key that did not sign it")
	}
	if got[1].TurnIndex != 1 {
		t.Fatalf("append order lost: got turn %d, want 1", got[1].TurnIndex)
	}
}

func TestAppendNeedsASigningKey(t *testing.T) {
	l, err := Open(filepath.Join(t.TempDir(), "receipts.jsonl"), nil)
	if err != nil {
		t.Fatal(err)
	}
	defer l.Close()
	if err := l.Append(testReceipt(t, "s1", 0)); err == nil || !strings.Contains(err.Error(), "signing key") {
		t.Fatalf("append without a key: got %v, want a signing-key error", err)
	}
}

func TestRejectDuplicateSessionTurn(t *testing.T) {
	l, err := Open(filepath.Join(t.TempDir(), "receipts.jsonl"), signer)
	if err != nil {
		t.Fatal(err)
	}
	defer l.Close()
	if err := l.Append(testReceipt(t, "s1", 0)); err != nil {
		t.Fatal(err)
	}
	err = l.Append(testReceipt(t, "s1", 0))
	if err == nil || !strings.Contains(err.Error(), "duplicate") {
		t.Fatalf("duplicate append: got %v, want duplicate error", err)
	}
}

func TestUniquenessSurvivesReopen(t *testing.T) {
	path := filepath.Join(t.TempDir(), "receipts.jsonl")
	l, err := Open(path, signer)
	if err != nil {
		t.Fatal(err)
	}
	if err := l.Append(testReceipt(t, "s1", 0)); err != nil {
		t.Fatal(err)
	}
	if err := l.Close(); err != nil {
		t.Fatal(err)
	}

	l2, err := Open(path, signer)
	if err != nil {
		t.Fatal(err)
	}
	defer l2.Close()
	err = l2.Append(testReceipt(t, "s1", 0))
	if err == nil || !strings.Contains(err.Error(), "duplicate") {
		t.Fatalf("duplicate after reopen: got %v, want duplicate error", err)
	}
	if err := l2.Append(testReceipt(t, "s1", 1)); err != nil {
		t.Fatalf("fresh turn after reopen: %v", err)
	}
}

// chainedLines writes n receipts through a real log and returns its
// lines, so each links correctly to the one before it.
func chainedLines(t *testing.T, n int) [][]byte {
	t.Helper()
	path := filepath.Join(t.TempDir(), "source.jsonl")
	l, err := Open(path, signer)
	if err != nil {
		t.Fatal(err)
	}
	for turn := 0; turn < n; turn++ {
		if err := l.Append(testReceipt(t, "s1", turn)); err != nil {
			t.Fatal(err)
		}
	}
	if err := l.Close(); err != nil {
		t.Fatal(err)
	}
	raw, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	return bytes.Split(bytes.TrimSuffix(raw, []byte("\n")), []byte("\n"))
}

// TestOpenRecoversFromCrashedAppend pins crash recovery for the
// append-only log: only a final line without its newline can be the
// residue of a crash mid-append, and such a receipt was never
// acknowledged (Append returns only after the full line is written).
// Damage anywhere else stays fatal.
func TestOpenRecoversFromCrashedAppend(t *testing.T) {
	lines := chainedLines(t, 2)
	first, second := lines[0], lines[1]
	cases := []struct {
		name     string
		content  []byte
		wantErr  string // non-empty: Open must fail with this
		wantWarn bool
		want     int // receipts after recovery
	}{
		{"clean", concat(first, "\n"), "", false, 1},
		{"partial final line", concat(first, "\n", string(second[:150])), "", true, 1},
		{"partial only line", second[:150], "", true, 0},
		{"complete final line without newline", concat(first, "\n", string(second)), "", false, 2},
		{"corrupt terminated final line", concat(first, "\n", string(second[:150]), "\n"), "line 2", false, 0},
		{"corrupt middle line", concat(second[:150], "\n", string(first), "\n"), "line 1", false, 0},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			path := filepath.Join(t.TempDir(), "receipts.jsonl")
			if err := os.WriteFile(path, tc.content, 0o644); err != nil {
				t.Fatal(err)
			}
			var warn bytes.Buffer
			stderr = &warn
			defer func() { stderr = os.Stderr }()

			l, err := Open(path, signer)
			if tc.wantErr != "" {
				if err == nil || !strings.Contains(err.Error(), tc.wantErr) {
					t.Fatalf("Open: got %v, want error mentioning %q", err, tc.wantErr)
				}
				return
			}
			if err != nil {
				t.Fatalf("Open: %v", err)
			}
			if got := warn.Len() > 0; got != tc.wantWarn {
				t.Fatalf("warning printed = %v, want %v (%q)", got, tc.wantWarn, warn.String())
			}
			// The next append must land on a line of its own.
			if err := l.Append(testReceipt(t, "s2", 0)); err != nil {
				t.Fatalf("append after recovery: %v", err)
			}
			l.Close()
			got, err := Scan(path)
			if err != nil {
				t.Fatalf("scan after recovery: %v", err)
			}
			if len(got) != tc.want+1 {
				t.Fatalf("got %d receipts, want %d", len(got), tc.want+1)
			}
		})
	}
}

func concat(b []byte, rest ...string) []byte {
	out := append([]byte(nil), b...)
	for _, s := range rest {
		out = append(out, s...)
	}
	return out
}

// failingFile simulates a disk that fails mid-write: the first write
// lands only partially and reports an error.
type failingFile struct {
	*os.File
	failWrites int
	failSync   bool
}

func (f *failingFile) Write(p []byte) (int, error) {
	if f.failWrites > 0 {
		f.failWrites--
		n, _ := f.File.Write(p[:len(p)/2])
		return n, errors.New("disk full")
	}
	return f.File.Write(p)
}

func (f *failingFile) Sync() error {
	if f.failSync {
		f.failSync = false
		return errors.New("fsync failed")
	}
	return f.File.Sync()
}

func TestFailedAppendLeavesNoPartialLine(t *testing.T) {
	cases := []struct {
		name string
		file func(*os.File) *failingFile
	}{
		{"short write", func(f *os.File) *failingFile { return &failingFile{File: f, failWrites: 1} }},
		{"failed fsync", func(f *os.File) *failingFile { return &failingFile{File: f, failSync: true} }},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			path := filepath.Join(t.TempDir(), "receipts.jsonl")
			l, err := Open(path, signer)
			if err != nil {
				t.Fatal(err)
			}
			if err := l.Append(testReceipt(t, "s1", 0)); err != nil {
				t.Fatal(err)
			}
			l.f = tc.file(l.f.(*os.File))
			if err := l.Append(testReceipt(t, "s1", 1)); err == nil {
				t.Fatal("append on failing disk succeeded")
			}
			// The failed turn was never acknowledged, so it may be retried.
			if err := l.Append(testReceipt(t, "s1", 1)); err != nil {
				t.Fatalf("append after failure: %v", err)
			}
			l.Close()
			got, err := Scan(path)
			if err != nil {
				t.Fatalf("log corrupted by failed append: %v", err)
			}
			if len(got) != 2 {
				t.Fatalf("got %d receipts, want 2", len(got))
			}
		})
	}
}

func TestScannedLinesAreCanonical(t *testing.T) {
	path := filepath.Join(t.TempDir(), "receipts.jsonl")
	l, err := Open(path, signer)
	if err != nil {
		t.Fatal(err)
	}
	if err := l.Append(testReceipt(t, "s1", 0)); err != nil {
		t.Fatal(err)
	}
	l.Close()

	got, err := Scan(path)
	if err != nil {
		t.Fatal(err)
	}
	// The stored digest must still match the stored result bytes: if the
	// round trip through the log reformatted numbers, verification on the
	// Python side would break.
	if d := receipt.Digest(got[0].ResultCanonical); d != got[0].ResultDigest {
		t.Fatalf("digest drift through log round trip:\nstored: %s\nrecomputed: %s", got[0].ResultDigest, d)
	}
}
