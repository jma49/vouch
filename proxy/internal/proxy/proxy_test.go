package proxy

import (
	"encoding/base64"
	"encoding/json"
	"io"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/jma49/vouch/proxy/internal/clock"
	"github.com/jma49/vouch/proxy/internal/extract"
	"github.com/jma49/vouch/proxy/internal/mcp"
	"github.com/jma49/vouch/proxy/internal/receipt"
	"github.com/jma49/vouch/proxy/internal/sign"
	"github.com/jma49/vouch/proxy/internal/sign/signtest"
	"github.com/jma49/vouch/proxy/internal/store"
)

var signer = signtest.Signer(1)

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

// session is one running proxy wired between an in-process fake
// upstream and an agent-side connection. agent and raw share one Conn,
// so a test may mix typed calls with raw frames as long as it does not
// read from both concurrently.
type session struct {
	agent    *mcp.Client
	raw      *mcp.Conn
	send     io.Writer // raw bytes into the proxy's downstream reader
	logPath  string
	shutdown func()
}

// startProxy wires a Server between an in-process fake upstream and a
// returned agent-side client.
func startProxy(t *testing.T) (*mcp.Client, func(), string) {
	t.Helper()
	s := startSession(t)
	return s.agent, s.shutdown, s.logPath
}

func startSession(t *testing.T) *session {
	t.Helper()
	return startSessionAt(t, filepath.Join(t.TempDir(), "receipts.jsonl"))
}

// startSessionAt is startSession on an existing log, as session s-test.
func startSessionAt(t *testing.T, logPath string) *session {
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
	rlog, err := store.Open(logPath, signer)
	if err != nil {
		t.Fatal(err)
	}

	srv := &Server{
		Down:      mcp.NewConn(downIn, proxyOut),
		Upstreams: []*Upstream{{Name: "fake", Client: mcp.NewClient(mcp.NewConn(upOut, proxyToUp))}},
		Schemas:   schemas,
		Log:       rlog,
		SessionID: "s-test",
		Clock:     &clock.Wall{},
		Logf:      t.Logf,
	}
	done := make(chan error, 1)
	go func() { done <- srv.Run() }()

	raw := mcp.NewConn(downOut, agentOut)
	shutdown := func() {
		agentOut.Close()
		if err := <-done; err != nil {
			t.Errorf("proxy run: %v", err)
		}
		rlog.Close()
	}
	return &session{agent: mcp.NewClient(raw), raw: raw, send: agentOut, logPath: logPath, shutdown: shutdown}
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

	receipts, err := store.ScanVerified(logPath, signtest.Keyring(signer))
	if err != nil {
		t.Fatal(err)
	}
	if len(receipts) != 2 {
		t.Fatalf("got %d receipts, want 2", len(receipts))
	}
	for i, r := range receipts {
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

func TestResultPayloadSelection(t *testing.T) {
	cases := []struct {
		name, result, payload, source string
	}{
		{"structured content wins", `{"structuredContent":{"x":1},"content":[{"type":"text","text":"{\"x\":2}"}]}`, `{"x":1}`, "structuredContent"},
		{"JSON object text", `{"content":[{"type":"text","text":"{\"x\":1}"}]}`, `{"x":1}`, "content/0/text"},
		{"JSON array text", `{"content":[{"type":"text","text":"[1,2]"}]}`, `[1,2]`, "content/0/text"},
		// A scalar is valid JSON but not a document facts can come from;
		// taking it used to hide the real payload in a later block (#20).
		{"scalar text skipped", `{"content":[{"type":"text","text":"1"},{"type":"text","text":"{\"x\":1}"}]}`, `{"x":1}`, "content/1/text"},
		{"null structured content ignored", `{"structuredContent":null,"content":[{"type":"text","text":"{\"x\":1}"}]}`, `{"x":1}`, "content/0/text"},
		{"plain text falls back to the result", `{"content":[{"type":"text","text":"plain words"}]}`, `{"content":[{"type":"text","text":"plain words"}]}`, "result"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			p, source := resultPayload(json.RawMessage(tc.result))
			if string(p) != tc.payload || source != tc.source {
				t.Fatalf("got %s from %q, want %s from %q", p, source, tc.payload, tc.source)
			}
		})
	}
}

// TestReceiptBindsTheResponse pins #20: the signed receipt covers the
// whole result the agent received, not only the payload facts come
// from. Here the text the model reads says 12.0 while the structured
// payload says 62.3; both must be under the signature.
func TestReceiptBindsTheResponse(t *testing.T) {
	s, logPath := recordingServer(t)
	result := json.RawMessage(`{"content":[{"type":"text","text":"NVDA RSI is 12.0"}],` +
		`"structuredContent":{"symbol":"NVDA","rsi_14":62.3}}`)
	if err := s.record("get_indicators", json.RawMessage(`{"symbol":"NVDA"}`), result, 0); err != nil {
		t.Fatalf("record: %v", err)
	}
	receipts, err := store.Scan(logPath)
	if err != nil {
		t.Fatal(err)
	}
	r := receipts[0]
	want, err := receipt.Canonicalize(result)
	if err != nil {
		t.Fatal(err)
	}
	if string(r.ResponseCanonical) != string(want) {
		t.Fatalf("response_canonical = %s, want %s", r.ResponseCanonical, want)
	}
	if r.ResponseDigest != receipt.Digest(want) {
		t.Fatalf("response_digest = %s", r.ResponseDigest)
	}
	if r.PayloadSource != "structuredContent" || !strings.Contains(string(r.ResponseCanonical), "12.0") {
		t.Fatalf("payload_source = %q; response = %s", r.PayloadSource, r.ResponseCanonical)
	}
	// Editing the response in the log, without the signing key, breaks
	// verification.
	tamperPayload(t, logPath, func(body string) string {
		return strings.Replace(body, "12.0", "62.3", 1)
	})
	if _, err := store.ScanVerified(logPath, signtest.Keyring(signer)); err == nil {
		t.Fatal("a log with an edited response verified")
	}
}

// tamperPayload rewrites the body inside every envelope of the log at
// path with edit, re-encoding it without re-signing: what someone
// holding the log but not the key can do.
func tamperPayload(t *testing.T, path string, edit func(string) string) {
	t.Helper()
	raw, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	var out []string
	for _, line := range strings.Split(strings.TrimSpace(string(raw)), "\n") {
		var env sign.Envelope
		if err := json.Unmarshal([]byte(line), &env); err != nil {
			t.Fatal(err)
		}
		body, err := base64.StdEncoding.DecodeString(env.Payload)
		if err != nil {
			t.Fatal(err)
		}
		env.Payload = base64.StdEncoding.EncodeToString([]byte(edit(string(body))))
		b, err := json.Marshal(env)
		if err != nil {
			t.Fatal(err)
		}
		out = append(out, string(b))
	}
	if err := os.WriteFile(path, []byte(strings.Join(out, "\n")+"\n"), 0o644); err != nil {
		t.Fatal(err)
	}
}

// recordingServer is a Server with a real log and the repo schemas but
// no transport, for exercising record directly.
func recordingServer(t *testing.T) (*Server, string) {
	t.Helper()
	schemas, err := extract.LoadDir("../../../schemas")
	if err != nil {
		t.Fatal(err)
	}
	logPath := filepath.Join(t.TempDir(), "receipts.jsonl")
	rlog, err := store.Open(logPath, signer)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { rlog.Close() })
	return &Server{Schemas: schemas, Log: rlog, SessionID: "s-rec", Clock: &clock.Wall{}, Logf: t.Logf}, logPath
}

// TestRecordFactsByResultKind pins which results become evidence. A
// tool error is receipted (the agent saw it) but yields no facts: its
// numbers describe a failure, not data the tool returned.
func TestRecordFactsByResultKind(t *testing.T) {
	cases := []struct {
		name   string
		result string
		facts  int
	}{
		{"structured result", `{"structuredContent":{"symbol":"NVDA","rsi_14":62.3}}`, 1},
		{"tool error with JSON text", `{"isError":true,"content":[{"type":"text","text":"{\"symbol\":\"NVDA\",\"rsi_14\":0}"}]}`, 0},
		{"tool error with structured content", `{"isError":true,"structuredContent":{"symbol":"NVDA","rsi_14":0}}`, 0},
		{"explicit isError false", `{"isError":false,"structuredContent":{"symbol":"NVDA","rsi_14":62.3}}`, 1},
		{"null value", `{"structuredContent":{"symbol":"NVDA","rsi_14":null,"close":181.52}}`, 1},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			s, logPath := recordingServer(t)
			args := json.RawMessage(`{"symbol":"NVDA"}`)
			if err := s.record("get_indicators", args, json.RawMessage(tc.result), 0); err != nil {
				t.Fatalf("record: %v", err)
			}
			receipts, err := store.Scan(logPath)
			if err != nil {
				t.Fatal(err)
			}
			if len(receipts) != 1 {
				t.Fatalf("got %d receipts, want 1", len(receipts))
			}
			if got := len(receipts[0].Facts); got != tc.facts {
				t.Fatalf("got %d facts, want %d: %+v", got, tc.facts, receipts[0].Facts)
			}
		})
	}
}

// TestReusedSessionContinuesItsTurns is #69 end to end: a proxy
// restarted with the same --session on the same log keeps receipting
// instead of failing every call on a duplicate (session, turn).
func TestReusedSessionContinuesItsTurns(t *testing.T) {
	logPath := filepath.Join(t.TempDir(), "receipts.jsonl")
	for run := 0; run < 2; run++ {
		s := startSessionAt(t, logPath)
		if _, err := s.agent.Call("initialize", map[string]any{"protocolVersion": "2025-06-18"}); err != nil {
			t.Fatal(err)
		}
		if _, err := s.agent.Call("tools/call", map[string]any{
			"name": "get_indicators", "arguments": map[string]any{"symbol": "NVDA"},
		}); err != nil {
			t.Fatalf("run %d: %v", run, err)
		}
		s.shutdown()
	}
	receipts, err := store.ScanVerified(logPath, signtest.Keyring(signer))
	if err != nil {
		t.Fatal(err)
	}
	if len(receipts) != 2 || receipts[0].TurnIndex != 0 || receipts[1].TurnIndex != 1 {
		t.Fatalf("got %d receipts, want turns 0 and 1", len(receipts))
	}
}
