package store

import (
	"encoding/json"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/jma49/vouch/proxy/internal/receipt"
)

var key = []byte("test-key")

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
	if err := r.Sign(key); err != nil {
		t.Fatal(err)
	}
	return r
}

func TestAppendAndScan(t *testing.T) {
	path := filepath.Join(t.TempDir(), "receipts.jsonl")
	l, err := Open(path)
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

	got, err := Scan(path)
	if err != nil {
		t.Fatal(err)
	}
	if len(got) != 2 {
		t.Fatalf("scan: got %d receipts, want 2", len(got))
	}
	ok, err := got[0].Verify(key)
	if err != nil || !ok {
		t.Fatalf("scanned receipt failed signature verification: ok=%v err=%v", ok, err)
	}
	if got[1].TurnIndex != 1 {
		t.Fatalf("append order lost: got turn %d, want 1", got[1].TurnIndex)
	}
}

func TestRejectUnsigned(t *testing.T) {
	l, err := Open(filepath.Join(t.TempDir(), "receipts.jsonl"))
	if err != nil {
		t.Fatal(err)
	}
	defer l.Close()
	r := testReceipt(t, "s1", 0)
	r.Sig = ""
	if err := l.Append(r); err == nil || !strings.Contains(err.Error(), "unsigned") {
		t.Fatalf("append unsigned: got %v, want unsigned error", err)
	}
}

func TestRejectDuplicateSessionTurn(t *testing.T) {
	l, err := Open(filepath.Join(t.TempDir(), "receipts.jsonl"))
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
	l, err := Open(path)
	if err != nil {
		t.Fatal(err)
	}
	if err := l.Append(testReceipt(t, "s1", 0)); err != nil {
		t.Fatal(err)
	}
	if err := l.Close(); err != nil {
		t.Fatal(err)
	}

	l2, err := Open(path)
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

func TestScannedLinesAreCanonical(t *testing.T) {
	path := filepath.Join(t.TempDir(), "receipts.jsonl")
	l, err := Open(path)
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
