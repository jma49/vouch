package store

import (
	"encoding/json"
	"flag"
	"os"
	"path/filepath"
	"testing"
	"time"

	"github.com/jma49/vouch/proxy/internal/receipt"
)

var update = flag.Bool("update", false, "regenerate the cross-language golden receipt log")

const goldenKey = "vouch-golden-key"

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
		r := &receipt.Receipt{
			ReceiptID:         id,
			SessionID:         "s-golden",
			TurnIndex:         turn,
			ToolName:          tool,
			ArgsCanonical:     argsC,
			ResultCanonical:   resC,
			ResultDigest:      receipt.Digest(resC),
			Facts:             facts,
			DataAsOf:          asof,
			WallTime:          time.Date(2026, 7, 25, 1, 12, 9, 0, time.UTC).Add(time.Duration(turn) * time.Minute),
			LogicalTime:       int64(turn + 1),
			UpstreamLatencyMS: 87,
		}
		if err := r.Sign([]byte(goldenKey)); err != nil {
			t.Fatal(err)
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
			`{"headline":"英伟达发布新品","score":0.87}`,
			nil, ""),
	}
}

// TestGoldenLog verifies the committed golden log matches what the Go
// side produces today; -update regenerates it.
func TestGoldenLog(t *testing.T) {
	goldenPath := filepath.Join("..", "..", "..", "testdata", "receipts_golden.jsonl")
	receipts := goldenReceipts(t)

	if *update {
		tmp := filepath.Join(t.TempDir(), "receipts.jsonl")
		l, err := Open(tmp)
		if err != nil {
			t.Fatal(err)
		}
		for _, r := range receipts {
			if err := l.Append(r); err != nil {
				t.Fatal(err)
			}
		}
		l.Close()
		raw, err := os.ReadFile(tmp)
		if err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(goldenPath, raw, 0o644); err != nil {
			t.Fatal(err)
		}
		t.Logf("regenerated %s", goldenPath)
	}

	got, err := Scan(goldenPath)
	if err != nil {
		t.Fatal(err)
	}
	if len(got) != len(receipts) {
		t.Fatalf("golden log has %d receipts, want %d (re-run with -update?)", len(got), len(receipts))
	}
	for i := range got {
		if ok, err := got[i].Verify([]byte(goldenKey)); err != nil || !ok {
			t.Fatalf("golden receipt %d signature: ok=%v err=%v", i, ok, err)
		}
		want, _ := json.Marshal(receipts[i])
		wantCanon, _ := receipt.Canonicalize(want)
		gotRaw, _ := json.Marshal(&got[i])
		gotCanon, _ := receipt.Canonicalize(gotRaw)
		if string(wantCanon) != string(gotCanon) {
			t.Fatalf("golden receipt %d drifted from generator (re-run with -update?):\nwant %s\ngot  %s", i, wantCanon, gotCanon)
		}
	}
}
