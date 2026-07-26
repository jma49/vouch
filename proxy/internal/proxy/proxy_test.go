package proxy

import (
	"encoding/json"
	"io"
	"path/filepath"
	"strings"
	"testing"

	"github.com/jma49/vouch/proxy/internal/clock"
	"github.com/jma49/vouch/proxy/internal/extract"
	"github.com/jma49/vouch/proxy/internal/mcp"
	"github.com/jma49/vouch/proxy/internal/store"
)

var key = []byte("test-key")

// fakeUpstream serves a minimal MCP server: one get_indicators tool
// returning a fixed indicator payload as structuredContent.
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
			result = map[string]any{
				"protocolVersion": "2025-06-18",
				"capabilities":    map[string]any{"tools": map[string]any{}},
				"serverInfo":      map[string]any{"name": "fake-upstream"},
			}
		case "tools/list":
			result = map[string]any{"tools": []map[string]any{{
				"name":        "get_indicators",
				"description": "test tool",
				"inputSchema": map[string]any{"type": "object"},
			}}}
		case "tools/call":
			result = map[string]any{
				"content": []map[string]any{{"type": "text", "text": "ok"}},
				"structuredContent": map[string]any{
					"symbol": "NVDA",
					"as_of":  "2026-07-24T20:00:00Z",
					"rsi_14": 62.3,
					"close":  181.52,
				},
			}
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

// startProxy wires a Server between an in-process fake upstream and a
// returned agent-side client.
func startProxy(t *testing.T) (*mcp.Client, func(), string) {
	t.Helper()

	upIn, proxyToUp := io.Pipe()   // proxy writes -> upstream reads
	upOut, upToProxy := io.Pipe()  // upstream writes -> proxy reads
	downIn, agentOut := io.Pipe()  // agent writes -> proxy reads
	downOut, proxyOut := io.Pipe() // proxy writes -> agent reads

	go fakeUpstream(t, mcp.NewConn(upIn, upToProxy))

	schemas, err := extract.LoadDir("../../../schemas")
	if err != nil {
		t.Fatal(err)
	}
	logPath := filepath.Join(t.TempDir(), "receipts.jsonl")
	rlog, err := store.Open(logPath)
	if err != nil {
		t.Fatal(err)
	}

	srv := &Server{
		Down:      mcp.NewConn(downIn, proxyOut),
		Upstreams: []*Upstream{{Name: "fake", Client: mcp.NewClient(mcp.NewConn(upOut, proxyToUp))}},
		Schemas:   schemas,
		Log:       rlog,
		Key:       key,
		SessionID: "s-test",
		Clock:     &clock.Wall{},
		Logf:      t.Logf,
	}
	done := make(chan error, 1)
	go func() { done <- srv.Run() }()

	agent := mcp.NewClient(mcp.NewConn(downOut, agentOut))
	shutdown := func() {
		agentOut.Close()
		if err := <-done; err != nil {
			t.Errorf("proxy run: %v", err)
		}
		rlog.Close()
	}
	return agent, shutdown, logPath
}

func TestFederationEndToEnd(t *testing.T) {
	agent, shutdown, logPath := startProxy(t)

	initRes, err := agent.Call("initialize", map[string]any{"protocolVersion": "2025-06-18"})
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(string(initRes), "vouch-proxy") {
		t.Fatalf("initialize result: %s", initRes)
	}
	if err := agent.Notify("notifications/initialized", nil); err != nil {
		t.Fatal(err)
	}

	listRes, err := agent.Call("tools/list", nil)
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(string(listRes), "get_indicators") {
		t.Fatalf("tools/list result: %s", listRes)
	}

	for range 2 {
		callRes, err := agent.Call("tools/call", map[string]any{
			"name":      "get_indicators",
			"arguments": map[string]any{"symbol": "NVDA", "timeframe": "1d"},
		})
		if err != nil {
			t.Fatal(err)
		}
		// Result must pass through unchanged (still carries upstream content).
		if !strings.Contains(string(callRes), `"rsi_14":62.3`) {
			t.Fatalf("tools/call result: %s", callRes)
		}
	}
	shutdown()

	receipts, err := store.Scan(logPath)
	if err != nil {
		t.Fatal(err)
	}
	if len(receipts) != 2 {
		t.Fatalf("got %d receipts, want 2", len(receipts))
	}
	for i, r := range receipts {
		if ok, err := r.Verify(key); err != nil || !ok {
			t.Fatalf("receipt %d signature: ok=%v err=%v", i, ok, err)
		}
		if r.TurnIndex != i {
			t.Fatalf("receipt %d: turn_index %d", i, r.TurnIndex)
		}
		if r.ToolName != "get_indicators" || r.SessionID != "s-test" {
			t.Fatalf("receipt %d metadata: %+v", i, r)
		}
		if r.DataAsOf != "2026-07-24T20:00:00Z" {
			t.Fatalf("receipt %d data_asof: %q", i, r.DataAsOf)
		}
		// Args canonicalized: keys sorted.
		if string(r.ArgsCanonical) != `{"symbol":"NVDA","timeframe":"1d"}` {
			t.Fatalf("receipt %d args: %s", i, r.ArgsCanonical)
		}
		if len(r.Facts) != 2 { // rsi_14 + close (schema also maps macd, absent here)
			t.Fatalf("receipt %d facts: %+v", i, r.Facts)
		}
		if r.Facts[0].Metric != "rsi_14" || r.Facts[0].Value != 62.3 {
			t.Fatalf("receipt %d fact 0: %+v", i, r.Facts[0])
		}
		if r.LogicalTime == 0 {
			t.Fatalf("receipt %d: logical_time not set", i)
		}
	}
	if receipts[0].LogicalTime >= receipts[1].LogicalTime {
		t.Fatal("logical time did not advance")
	}
}

func TestUnknownToolRejected(t *testing.T) {
	agent, shutdown, _ := startProxy(t)
	defer shutdown()

	if _, err := agent.Call("initialize", map[string]any{"protocolVersion": "2025-06-18"}); err != nil {
		t.Fatal(err)
	}
	_, err := agent.Call("tools/call", map[string]any{"name": "nope", "arguments": map[string]any{}})
	if err == nil || !strings.Contains(err.Error(), "unknown tool") {
		t.Fatalf("got %v, want unknown tool error", err)
	}
}

func TestUnfederatedMethodRejected(t *testing.T) {
	agent, shutdown, _ := startProxy(t)
	defer shutdown()

	if _, err := agent.Call("initialize", map[string]any{"protocolVersion": "2025-06-18"}); err != nil {
		t.Fatal(err)
	}
	_, err := agent.Call("resources/list", nil)
	if err == nil || !strings.Contains(err.Error(), "not federated") {
		t.Fatalf("got %v, want method-not-found error", err)
	}
}

func TestResultPayloadFallbacks(t *testing.T) {
	// Text content that parses as JSON is used as the payload.
	p := resultPayload(json.RawMessage(`{"content":[{"type":"text","text":"{\"x\":1}"}]}`))
	if string(p) != `{"x":1}` {
		t.Fatalf("text payload: %s", p)
	}
	// Non-JSON text falls back to the whole result.
	full := `{"content":[{"type":"text","text":"plain words"}]}`
	if p := resultPayload(json.RawMessage(full)); string(p) != full {
		t.Fatalf("fallback payload: %s", p)
	}
}
