package store

import (
	"bytes"
	"encoding/json"
	"flag"
	"os"
	"path/filepath"
	"testing"
	"time"

	"github.com/jma49/vouch/proxy/internal/receipt"
	"github.com/jma49/vouch/proxy/internal/sign"
)

var update = flag.Bool("update", false, "regenerate the cross-language golden receipt log")

// goldenSigner signs the golden log with the committed, public test key
// (testdata/keys/README.md). Ed25519 is deterministic, so regenerating
// the log is byte-stable and CI can fail on drift.
func goldenSigner(t *testing.T) *sign.Signer {
	t.Helper()
	pemBytes, err := os.ReadFile(filepath.Join("..", "..", "..", "testdata", "keys", "golden.pem"))
	if err != nil {
		t.Fatal(err)
	}
	priv, err := sign.ParsePrivateKeyPEM(pemBytes)
	if err != nil {
		t.Fatal(err)
	}
	return sign.NewSigner(priv)
}

// goldenReceipts builds a fixed set of receipts covering the field
// shapes the Python verifier must reproduce byte-for-byte: facts with
// and without omitempty fields, a nil facts slice, unicode passthrough,
// and empty args.
func goldenReceipts(t *testing.T) []*receipt.Receipt {
	t.Helper()
	mk := func(id string, turn int, tool string, args, result string, facts []receipt.Fact, asof string) *receipt.Receipt {
		argsC, err := receipt.Canonicalize([]byte(args))
		if err != nil {
			t.Fatal(err)
		}
		resC, err := receipt.Canonicalize([]byte(result))
		if err != nil {
			t.Fatal(err)
		}
		// The response the agent received, shaped like a real MCP result:
		// the payload as structured content plus a text block, so the
		// golden log exercises response binding (#20) across languages.
		text, err := json.Marshal(string(resC))
		if err != nil {
			t.Fatal(err)
		}
		respC, err := receipt.Canonicalize([]byte(`{"content":[{"type":"text","text":` + string(text) +
			`}],"structuredContent":` + result + `}`))
		if err != nil {
			t.Fatal(err)
		}
		r := &receipt.Receipt{
			ReceiptID:         id,
			SessionID:         "s-golden",
			TurnIndex:         turn,
			ToolName:          tool,
			ArgsCanonical:     argsC,
			ResultCanonical:   resC,
			ResultDigest:      receipt.Digest(resC),
			PayloadSource:     "structuredContent",
			ResponseCanonical: respC,
			ResponseDigest:    receipt.Digest(respC),
			Facts:             facts,
			DataAsOf:          asof,
			WallTime:          time.Date(2026, 7, 25, 1, 12, 9, 0, time.UTC).Add(time.Duration(turn) * time.Minute),
			LogicalTime:       int64(turn + 1),
			UpstreamLatencyMS: 87,
		}
		return r
	}
	return []*receipt.Receipt{
		mk("golden-0", 0, "get_indicators",
			`{"symbol":"NVDA","timeframe":"1d"}`,
			`{"symbol":"NVDA","as_of":"2026-07-24T20:00:00Z","rsi_14":62.3,"macd":{"histogram":-0.42},"close":181.52}`,
			[]receipt.Fact{
				{Entity: "NVDA", Metric: "rsi_14", Value: 62.3, AsOf: "2026-07-24T20:00:00Z", Timeframe: "1d", JSONPtr: "/rsi_14", TolClass: "indicator"},
				{Entity: "NVDA", Metric: "close_price", Value: 181.52, Unit: "USD", AsOf: "2026-07-24T20:00:00Z", Timeframe: "1d", JSONPtr: "/close", TolClass: "price"},
			},
			"2026-07-24T20:00:00Z"),
		mk("golden-1", 1, "get_quote",
			`{"symbol":"AMD"}`,
			`{"symbol":"AMD","as_of":"2026-07-24T20:00:00Z","last":172.04,"change_pct":-1.35,"volume":52410000}`,
			[]receipt.Fact{
				{Entity: "AMD", Metric: "last_price", Value: 172.04, Unit: "USD", AsOf: "2026-07-24T20:00:00Z", JSONPtr: "/last", TolClass: "price"},
				{Entity: "AMD", Metric: "change_pct", Value: -1.35, Unit: "pct", AsOf: "2026-07-24T20:00:00Z", Timeframe: "1d", JSONPtr: "/change_pct", TolClass: "percentage"},
				{Entity: "AMD", Metric: "volume", Value: 52410000, AsOf: "2026-07-24T20:00:00Z", Timeframe: "1d", JSONPtr: "/volume", TolClass: "count"},
			},
			"2026-07-24T20:00:00Z"),
		// No schema for this tool: nil facts, no data_asof, unicode result.
		mk("golden-2", 2, "get_news",
			`{}`,
			`{"headline":"英伟达发布新品\u2028update","score":0.87}`,
			nil, ""),
	}
}

// TestGoldenLog verifies the committed golden log matches what the Go
// side produces today; -update regenerates it.
// TestGoldenLog writes the golden receipts through a real log, sealed
// with a checkpoint at a fixed time, and compares the result byte for
// byte with testdata/receipts_golden.jsonl. Ed25519 is deterministic, so
// any difference is a change in format, canonicalization, or signing.
func TestGoldenLog(t *testing.T) {
	goldenPath := filepath.Join("..", "..", "..", "testdata", "receipts_golden.jsonl")
	signer := goldenSigner(t)

	tmp := filepath.Join(t.TempDir(), "receipts.jsonl")
	l, err := Open(tmp, signer)
	if err != nil {
		t.Fatal(err)
	}
	for _, r := range goldenReceipts(t) {
		if err := l.Append(r); err != nil {
			t.Fatal(err)
		}
	}
	head, err := l.Seal("s-golden", time.Date(2026, 7, 25, 2, 0, 0, 0, time.UTC))
	if err != nil {
		t.Fatal(err)
	}
	if err := l.Close(); err != nil {
		t.Fatal(err)
	}
	built, err := os.ReadFile(tmp)
	if err != nil {
		t.Fatal(err)
	}

	if *update {
		if err := os.WriteFile(goldenPath, built, 0o644); err != nil {
			t.Fatal(err)
		}
		t.Logf("regenerated %s (head %s)", goldenPath, head)
	}
	golden, err := os.ReadFile(goldenPath)
	if err != nil {
		t.Fatal(err)
	}
	if !bytes.Equal(golden, built) {
		t.Fatalf("%s drifted from the generator; re-run with -update if the change is intended", goldenPath)
	}

	audit, err := Verify(goldenPath, sign.Keyring{signer.KeyID(): signer.Public()})
	if err != nil {
		t.Fatal(err)
	}
	if !audit.Sealed || audit.Head != head || len(audit.Receipts) != 3 || audit.Checkpoints != 1 {
		t.Fatalf("golden audit: %+v, want 3 receipts, 1 checkpoint, sealed at head %s", audit, head)
	}
}
