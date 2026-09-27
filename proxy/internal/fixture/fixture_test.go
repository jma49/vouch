package fixture_test

import (
	"encoding/json"
	"io"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/jma49/vouch/proxy/internal/clock"
	"github.com/jma49/vouch/proxy/internal/extract"
	"github.com/jma49/vouch/proxy/internal/fixture"
	"github.com/jma49/vouch/proxy/internal/mcp"
	"github.com/jma49/vouch/proxy/internal/proxy"
	"github.com/jma49/vouch/proxy/internal/store"
)

var key = []byte("test-key")

func fakeUpstream(t *testing.T, conn *mcp.Conn) {
	t.Helper()
	for {
		m, err := conn.Read()
		if err != nil {
			return
		}
		if m.IsNotification() {
			continue
		}
		var result any
		switch m.Method {
		case "initialize":
			result = map[string]any{"protocolVersion": "2025-06-18",
				"capabilities": map[string]any{"tools": map[string]any{}},
				"serverInfo":   map[string]any{"name": "fake-upstream"}}
		case "tools/list":
			result = map[string]any{"tools": []map[string]any{{
				"name": "get_indicators", "inputSchema": map[string]any{"type": "object"}}}}
		case "tools/call":
			result = map[string]any{
				"content": []map[string]any{{"type": "text", "text": "ok"}},
				"structuredContent": map[string]any{
					"symbol": "NVDA", "as_of": "2026-07-24T20:00:00Z", "rsi_14": 62.3}}
		default:
			t.Errorf("fake upstream: unexpected method %s", m.Method)
			return
		}
		raw, _ := json.Marshal(result)
		if err := conn.Write(&mcp.Message{ID: m.ID, Result: raw}); err != nil {
			return
		}
	}
}

// runSession drives one proxy session (initialize + the given tool
// calls) against the provided upstream and returns the receipts.
func runSession(t *testing.T, up *proxy.Upstream, clk clock.Clock, calls []map[string]any) []struct {
	Digest   string
	WallTime time.Time
} {
	t.Helper()
	downIn, agentOut := io.Pipe()
	downOut, proxyOut := io.Pipe()

	schemas, err := extract.LoadDir("../../../schemas")
	if err != nil {
		t.Fatal(err)
	}
	logPath := filepath.Join(t.TempDir(), "receipts.jsonl")
	rlog, err := store.Open(logPath)
	if err != nil {
		t.Fatal(err)
	}

	srv := &proxy.Server{
		Down: mcp.NewConn(downIn, proxyOut), Upstreams: []*proxy.Upstream{up},
		Schemas: schemas, Log: rlog, Key: key, SessionID: "s-fix", Clock: clk, Logf: t.Logf,
	}
	done := make(chan error, 1)
	go func() { done <- srv.Run() }()

	agent := mcp.NewClient(mcp.NewConn(downOut, agentOut))
	if _, err := agent.Call("initialize", map[string]any{"protocolVersion": "2025-06-18"}); err != nil {
		t.Fatal(err)
	}
	for _, args := range calls {
		if _, err := agent.Call("tools/call", map[string]any{"name": "get_indicators", "arguments": args}); err != nil {
			t.Fatal(err)
		}
	}
	agentOut.Close()
	if err := <-done; err != nil {
		t.Fatalf("proxy run: %v", err)
	}
	rlog.Close()

	receipts, err := store.Scan(logPath)
	if err != nil {
		t.Fatal(err)
	}
	out := make([]struct {
		Digest   string
		WallTime time.Time
	}, len(receipts))
	for i, r := range receipts {
		if ok, err := r.Verify(key); err != nil || !ok {
			t.Fatalf("receipt %d signature: ok=%v err=%v", i, ok, err)
		}
		out[i].Digest = r.ResultDigest
		out[i].WallTime = r.WallTime
	}
	return out
}

func TestRecordThenReplay(t *testing.T) {
	fixDir := t.TempDir()
	fs := &fixture.Store{Dir: fixDir}

	// Record pass: live fake upstream wrapped in a Recorder.
	upIn, proxyToUp := io.Pipe()
	upOut, upToProxy := io.Pipe()
	go fakeUpstream(t, mcp.NewConn(upIn, upToProxy))
	live := mcp.NewClient(mcp.NewConn(upOut, proxyToUp))
	recordedAt := time.Date(2026, 7, 25, 12, 0, 0, 0, time.UTC)
	recUp := &proxy.Upstream{Name: "fake", Client: fixture.NewRecorder("fake", live, fs, func() time.Time { return recordedAt })}

	recorded := runSession(t, recUp, &clock.Wall{},
		[]map[string]any{{"symbol": "NVDA", "timeframe": "1d"}})
	if len(recorded) != 1 {
		t.Fatalf("record pass: %d receipts", len(recorded))
	}

	// Replay pass: no live upstream. Same call with argument keys in a
	// different order must hit the same content-addressed fixture.
	tools, err := fs.LoadAllTools()
	if err != nil {
		t.Fatal(err)
	}
	if len(tools) != 1 || tools[0].Name != "fake" {
		t.Fatalf("recorded tools: %v", tools)
	}
	epoch, err := fs.Epoch()
	if err != nil {
		t.Fatal(err)
	}
	if !epoch.Equal(recordedAt) {
		t.Fatalf("epoch %v, want %v", epoch, recordedAt)
	}
	repUp := &proxy.Upstream{Name: "fake", Client: &fixture.Replayer{Upstream: "fake", Store: fs, Tools: tools[0].Tools}}

	replayed := runSession(t, repUp, &clock.Logical{Epoch: epoch},
		[]map[string]any{{"timeframe": "1d", "symbol": "NVDA"}})
	if len(replayed) != 1 {
		t.Fatalf("replay pass: %d receipts", len(replayed))
	}

	if replayed[0].Digest != recorded[0].Digest {
		t.Fatalf("digest drift between record and replay:\nrecord: %s\nreplay: %s",
			recorded[0].Digest, replayed[0].Digest)
	}
	if !replayed[0].WallTime.Equal(epoch) {
		t.Fatalf("replay wall_time %v, want frozen epoch %v", replayed[0].WallTime, epoch)
	}
}

// TestLoadAllToolsKeepsEveryUpstreamInStableOrder pins replay's view
// of the recorded upstreams: one entry per upstream, even when they
// share an executable, in an order that does not depend on map
// iteration, so the merged tools/list is the same on every run.
func TestLoadAllToolsKeepsEveryUpstreamInStableOrder(t *testing.T) {
	fs := &fixture.Store{Dir: t.TempDir()}
	at := time.Date(2026, 7, 25, 12, 0, 0, 0, time.UTC)
	names := []string{"python3 fake.py tool_b", "python3 fake.py tool_a", "zeta", "alpha", "market"}
	for _, name := range names {
		if err := fs.SaveTools(name, json.RawMessage(`{"tools":[{"name":"`+name+`"}]}`), at); err != nil {
			t.Fatal(err)
		}
	}
	want := []string{"alpha", "market", "python3 fake.py tool_a", "python3 fake.py tool_b", "zeta"}
	for run := 0; run < 20; run++ {
		got, err := fs.LoadAllTools()
		if err != nil {
			t.Fatal(err)
		}
		if len(got) != len(want) {
			t.Fatalf("got %d upstreams, want %d", len(got), len(want))
		}
		for i, u := range got {
			if u.Name != want[i] || !strings.Contains(string(u.Tools), `"`+want[i]+`"`) {
				t.Fatalf("run %d position %d: got %s %s, want %s", run, i, u.Name, u.Tools, want[i])
			}
		}
	}
}

func TestReplayMissingFixtureFails(t *testing.T) {
	fs := &fixture.Store{Dir: t.TempDir()}
	rep := &fixture.Replayer{Upstream: "fake", Store: fs, Tools: json.RawMessage(`{"tools":[]}`)}
	_, err := rep.Call("tools/call", map[string]any{"name": "get_quote", "arguments": map[string]any{"symbol": "AMD"}})
	if err == nil || !strings.Contains(err.Error(), "no fixture") {
		t.Fatalf("got %v, want no-fixture error", err)
	}
}
