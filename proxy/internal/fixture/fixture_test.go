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
	"github.com/jma49/vouch/proxy/internal/sign/signtest"
	"github.com/jma49/vouch/proxy/internal/store"
)

var signer = signtest.Signer(1)

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
	rlog, err := store.Open(logPath, signer)
	if err != nil {
		t.Fatal(err)
	}

	srv := &proxy.Server{
		Down: mcp.NewConn(downIn, proxyOut), Upstreams: []*proxy.Upstream{up},
		Schemas: schemas, Log: rlog, SessionID: "s-fix", Clock: clk, Logf: t.Logf,
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

	receipts, err := store.ScanVerified(logPath, signtest.Keyring(signer))
	if err != nil {
		t.Fatal(err)
	}
	out := make([]struct {
		Digest   string
		WallTime time.Time
	}, len(receipts))
	for i, r := range receipts {
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

// TestKeyComparesArgumentsAsValues pins #64: literals of one number
// share a fixture, and different values never do, including integers
// that float64 would round to the same value.
func TestKeyComparesArgumentsAsValues(t *testing.T) {
	key := func(args string) string {
		t.Helper()
		k, err := fixture.Key("get_bars", []byte(args))
		if err != nil {
			t.Fatalf("Key(%s): %v", args, err)
		}
		return k
	}
	same := [][]string{
		{`{"limit":5}`, `{"limit":5.0}`, `{"limit":5e0}`, `{"limit":50e-1}`, `{"limit":0.5E+1}`, `{"limit":500E-2}`},
		{`{"x":0}`, `{"x":-0}`, `{"x":0.000}`, `{"x":0e10}`},
		{`{"x":[1,{"y":2.50}]}`, `{"x":[1.0,{"y":2.5}]}`},
		{`{"x":-1.5}`, `{"x":-15e-1}`},
	}
	for _, group := range same {
		for _, args := range group[1:] {
			if key(args) != key(group[0]) {
				t.Errorf("%s and %s should share a fixture", group[0], args)
			}
		}
	}
	differ := [][2]string{
		{`{"limit":5}`, `{"limit":6}`},
		{`{"limit":5}`, `{"limit":"5"}`},
		{`{"x":1.5}`, `{"x":-1.5}`},
		{`{"x":12345678901234567890}`, `{"x":12345678901234567891}`},
		{`{"x":1e2}`, `{"x":1e-2}`},
	}
	for _, d := range differ {
		if key(d[0]) == key(d[1]) {
			t.Errorf("%s and %s must not share a fixture", d[0], d[1])
		}
	}
	if k1, k2 := key(`{"limit":5}`), func() string {
		k, _ := fixture.Key("get_quote", []byte(`{"limit":5}`))
		return k
	}(); k1 == k2 {
		t.Error("different tools must not share a fixture")
	}
}

// TestReplayMatchesANumberSpelledDifferently is #64 end to end: a call
// recorded with 5 replays when the agent sends 5.0.
func TestReplayMatchesANumberSpelledDifferently(t *testing.T) {
	fs := &fixture.Store{Dir: t.TempDir()}
	at := time.Date(2026, 7, 25, 12, 0, 0, 0, time.UTC)
	if err := fs.SaveCall("get_bars", json.RawMessage(`{"limit":5}`), json.RawMessage(`{"content":[]}`), at); err != nil {
		t.Fatal(err)
	}
	rep := &fixture.Replayer{Upstream: "fake", Store: fs, Tools: json.RawMessage(`{"tools":[]}`)}
	params := map[string]any{"name": "get_bars", "arguments": map[string]any{"limit": json.Number("5.0")}}
	if _, err := rep.Call("tools/call", params); err != nil {
		t.Fatalf("replay of limit=5.0 against a limit=5 recording: %v", err)
	}
}
