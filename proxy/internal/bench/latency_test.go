// Package bench measures what the proxy adds to a tools/call
// (docs/roadmap.md Phase 5). `make bench` runs TestLatencyReport, which
// writes docs/bench/latency.json; the README renders its latency table
// from that file. BenchmarkToolsCall is the same path for `go test
// -bench`.
package bench

import (
	"encoding/json"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"runtime"
	"sort"
	"testing"
	"time"

	"github.com/jma49/vouch/proxy/internal/clock"
	"github.com/jma49/vouch/proxy/internal/extract"
	"github.com/jma49/vouch/proxy/internal/mcp"
	"github.com/jma49/vouch/proxy/internal/proxy"
	"github.com/jma49/vouch/proxy/internal/receipt"
	"github.com/jma49/vouch/proxy/internal/sign/signtest"
	"github.com/jma49/vouch/proxy/internal/store"
)

// result is a get_indicators answer of realistic shape, so the proxied
// path does the work it does in production: canonicalizing the args,
// the response, and the payload, extracting four facts, signing, and
// an fsynced append.
var result = json.RawMessage(`{"content":[{"type":"text","text":"NVDA 1d indicators"}],` +
	`"structuredContent":{"symbol":"NVDA","as_of":"2026-07-24T20:00:00Z","timeframe":"1d",` +
	`"rsi_14":62.3,"macd":{"line":1.84,"signal":1.52,"histogram":0.32},"close":181.52,` +
	`"sma_50":171.08,"sma_200":142.77,"volume":201345600}}`)

// upstream answers every request at once from memory, so what is
// measured is transport and proxy work, not a data source.
func upstream(conn *mcp.Conn) {
	for {
		m, err := conn.Read()
		if err != nil {
			return
		}
		if m.IsNotification() {
			continue
		}
		var res json.RawMessage
		switch m.Method {
		case "initialize":
			res = json.RawMessage(`{"protocolVersion":"2025-06-18","capabilities":{"tools":{}}}`)
		case "tools/list":
			res = json.RawMessage(`{"tools":[{"name":"get_indicators","inputSchema":{"type":"object"}}]}`)
		default:
			res = result
		}
		if err := conn.Write(&mcp.Message{ID: m.ID, Result: res}); err != nil {
			return
		}
	}
}

func pipes() (clientSide, serverSide *mcp.Conn, closeAll func()) {
	sIn, cOut := io.Pipe()
	cIn, sOut := io.Pipe()
	return mcp.NewConn(cIn, cOut), mcp.NewConn(sIn, sOut), func() { cOut.Close(); sOut.Close() }
}

// direct connects an agent straight to the upstream.
func direct(tb testing.TB) (*mcp.Client, func()) {
	c, s, closeAll := pipes()
	go upstream(s)
	return mcp.NewClient(c), closeAll
}

// proxied puts a proxy, with schemas and a signed log on disk, between
// the agent and the same upstream.
func proxied(tb testing.TB) (*mcp.Client, func()) {
	tb.Helper()
	upClient, upServer, closeUp := pipes()
	go upstream(upServer)
	schemas, err := extract.LoadDir("../../../schemas")
	if err != nil {
		tb.Fatal(err)
	}
	rlog, err := store.Open(filepath.Join(tb.TempDir(), "receipts.jsonl"), signtest.Signer(1))
	if err != nil {
		tb.Fatal(err)
	}
	agentSide, proxySide, closeDown := pipes()
	srv := &proxy.Server{
		Down: proxySide, Upstreams: []*proxy.Upstream{{Name: "bench", Client: mcp.NewClient(upClient)}},
		Schemas: schemas, Log: rlog, SessionID: "s-bench", Clock: &clock.Wall{}, Logf: tb.Logf,
	}
	done := make(chan error, 1)
	go func() { done <- srv.Run() }()
	agent := mcp.NewClient(agentSide)
	if _, err := agent.Call("initialize", map[string]any{"protocolVersion": "2025-06-18"}); err != nil {
		tb.Fatal(err)
	}
	return agent, func() {
		closeDown()
		if err := <-done; err != nil {
			tb.Errorf("proxy: %v", err)
		}
		rlog.Close()
		closeUp()
	}
}

var args = map[string]any{"name": "get_indicators", "arguments": map[string]any{"symbol": "NVDA", "timeframe": "1d"}}

// sample times n sequential calls after a warmup.
func sample(tb testing.TB, agent *mcp.Client, warmup, n int) []time.Duration {
	tb.Helper()
	for range warmup {
		if _, err := agent.Call("tools/call", args); err != nil {
			tb.Fatal(err)
		}
	}
	out := make([]time.Duration, n)
	for i := range out {
		start := time.Now()
		if _, err := agent.Call("tools/call", args); err != nil {
			tb.Fatal(err)
		}
		out[i] = time.Since(start)
	}
	sort.Slice(out, func(i, j int) bool { return out[i] < out[j] })
	return out
}

// quantile is the nearest-rank quantile of sorted durations.
func quantile(sorted []time.Duration, q float64) time.Duration {
	i := int(q*float64(len(sorted))+0.5) - 1
	return sorted[max(0, min(i, len(sorted)-1))]
}

type dist struct {
	P50US float64 `json:"p50_us"`
	P99US float64 `json:"p99_us"`
}

func summarize(sorted []time.Duration) dist {
	us := func(d time.Duration) float64 { return float64(d.Nanoseconds()) / 1e3 }
	return dist{P50US: us(quantile(sorted, 0.50)), P99US: us(quantile(sorted, 0.99))}
}

// Report is what docs/bench/latency.json holds.
type Report struct {
	Calls   int    `json:"calls"`
	Machine string `json:"machine"`
	GOOS    string `json:"goos"`
	GOARCH  string `json:"goarch"`
	CPUs    int    `json:"cpus"`
	Go      string `json:"go"`
	Direct  dist   `json:"direct"`
	Proxied dist   `json:"proxied"`
	// Append is store.Log.Append alone: signing plus the fsynced write,
	// the part of the proxied path that durability costs.
	Append dist `json:"append"`
}

// appends times n Log.Append calls of the receipt the proxy writes for
// result, on a log in a temporary directory.
func appends(tb testing.TB, n int) []time.Duration {
	tb.Helper()
	rlog, err := store.Open(filepath.Join(tb.TempDir(), "receipts.jsonl"), signtest.Signer(1))
	if err != nil {
		tb.Fatal(err)
	}
	defer rlog.Close()
	canon, err := receipt.Canonicalize(result)
	if err != nil {
		tb.Fatal(err)
	}
	out := make([]time.Duration, n)
	for i := range out {
		r := &receipt.Receipt{
			ReceiptID: fmt.Sprintf("r-%d", i), SessionID: "s-bench", TurnIndex: i, ToolName: "get_indicators",
			ArgsCanonical: json.RawMessage(`{"symbol":"NVDA","timeframe":"1d"}`), ResultCanonical: canon,
			ResultDigest: receipt.Digest(canon), ResponseCanonical: canon, ResponseDigest: receipt.Digest(canon),
			WallTime: time.Date(2026, 7, 25, 0, 0, 0, 0, time.UTC),
		}
		start := time.Now()
		if err := rlog.Append(r); err != nil {
			tb.Fatal(err)
		}
		out[i] = time.Since(start)
	}
	sort.Slice(out, func(i, j int) bool { return out[i] < out[j] })
	return out
}

// TestLatencyReport writes the latency report to $VOUCH_BENCH_OUT.
// Without it the test skips: the numbers describe one machine, so only
// `make bench` records them, never CI.
func TestLatencyReport(t *testing.T) {
	out := os.Getenv("VOUCH_BENCH_OUT")
	if out == "" {
		t.Skip("VOUCH_BENCH_OUT is not set; run `make bench`")
	}
	const warmup, n = 200, 5000
	agent, stop := direct(t)
	d := sample(t, agent, warmup, n)
	stop()
	agent, stop = proxied(t)
	p := sample(t, agent, warmup, n)
	stop()
	a := appends(t, n)

	r := Report{
		Calls: n, Machine: os.Getenv("VOUCH_BENCH_MACHINE"), GOOS: runtime.GOOS, GOARCH: runtime.GOARCH,
		CPUs: runtime.NumCPU(), Go: runtime.Version(), Direct: summarize(d), Proxied: summarize(p),
		Append: summarize(a),
	}
	raw, err := json.MarshalIndent(r, "", "  ")
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(out, append(raw, '\n'), 0o644); err != nil {
		t.Fatal(err)
	}
	t.Logf("%s", raw)
}

// BenchmarkToolsCall compares one tools/call with and without the proxy.
func BenchmarkToolsCall(b *testing.B) {
	for _, bc := range []struct {
		name  string
		setup func(testing.TB) (*mcp.Client, func())
	}{{"direct", direct}, {"proxied", proxied}} {
		b.Run(bc.name, func(b *testing.B) {
			agent, stop := bc.setup(b)
			defer stop()
			b.ResetTimer()
			for range b.N {
				if _, err := agent.Call("tools/call", args); err != nil {
					b.Fatal(err)
				}
			}
		})
	}
}
