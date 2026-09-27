package proxy

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/jma49/vouch/proxy/internal/clock"
	"github.com/jma49/vouch/proxy/internal/extract"
	"github.com/jma49/vouch/proxy/internal/mcp"
	"github.com/jma49/vouch/proxy/internal/sign/signtest"
	"github.com/jma49/vouch/proxy/internal/store"
)

// gatedUpstream serves two tools concurrently: get_indicators answers at
// once; slow_tool reports each request on started, sends one progress
// notification if the request carries a progressToken, and answers
// only when release is closed. Every notifications/cancelled it
// receives is reported on cancels. A cancelled slow call still answers
// on release, as a real upstream racing the cancellation might.
type gatedUpstream struct {
	started chan json.RawMessage // upstream-side request ids of slow calls
	cancels chan json.RawMessage // requestIds named by notifications/cancelled
	release chan struct{}
}

func newGatedUpstream() *gatedUpstream {
	return &gatedUpstream{
		started: make(chan json.RawMessage, 64),
		cancels: make(chan json.RawMessage, 64),
		release: make(chan struct{}),
	}
}

func (g *gatedUpstream) serve(conn *mcp.Conn) {
	for {
		m, err := conn.Read()
		if err != nil {
			return
		}
		if m.IsNotification() {
			if m.Method == "notifications/cancelled" {
				var p struct {
					RequestID json.RawMessage `json:"requestId"`
				}
				_ = json.Unmarshal(m.Params, &p)
				g.cancels <- p.RequestID
			}
			continue
		}
		go g.answer(conn, m)
	}
}

func (g *gatedUpstream) answer(conn *mcp.Conn, m *mcp.Message) {
	var result any
	switch m.Method {
	case "initialize":
		result = map[string]any{"protocolVersion": "2025-06-18", "capabilities": map[string]any{"tools": map[string]any{}}}
	case "tools/list":
		result = map[string]any{"tools": []map[string]any{
			{"name": "get_indicators", "inputSchema": map[string]any{"type": "object"}},
			{"name": "slow_tool", "inputSchema": map[string]any{"type": "object"}},
		}}
	case "tools/call":
		var p struct {
			Name string `json:"name"`
			Meta struct {
				ProgressToken json.RawMessage `json:"progressToken"`
			} `json:"_meta"`
		}
		_ = json.Unmarshal(m.Params, &p)
		if p.Name == "slow_tool" {
			if len(p.Meta.ProgressToken) > 0 {
				note, _ := json.Marshal(map[string]any{"progressToken": p.Meta.ProgressToken, "progress": 1, "total": 2})
				_ = conn.Write(&mcp.Message{Method: "notifications/progress", Params: note})
			}
			g.started <- m.ID
			<-g.release
		}
		result = map[string]any{
			"content":           []map[string]any{{"type": "text", "text": "ok"}},
			"structuredContent": map[string]any{"symbol": "NVDA", "as_of": "2026-07-24T20:00:00Z", "rsi_14": 62.3, "close": 181.52},
		}
	}
	raw, _ := json.Marshal(result)
	_ = conn.Write(&mcp.Message{ID: m.ID, Result: raw})
}

// agentLog collects what the agent-side client logs, to catch a reply
// the proxy should not have sent.
type agentLog struct {
	mu    sync.Mutex
	lines []string
}

func (l *agentLog) logf(format string, args ...any) {
	l.mu.Lock()
	defer l.mu.Unlock()
	l.lines = append(l.lines, fmt.Sprintf(format, args...))
}

func (l *agentLog) String() string {
	l.mu.Lock()
	defer l.mu.Unlock()
	return strings.Join(l.lines, "\n")
}

type progressHandler struct{ notes chan *mcp.Message }

func (h progressHandler) HandleNotification(m *mcp.Message) { h.notes <- m }
func (h progressHandler) HandleRequest(context.Context, *mcp.Message) (json.RawMessage, error) {
	return nil, errors.New("unexpected request")
}

type gatedSession struct {
	up       *gatedUpstream
	agent    *mcp.Client
	agentLog *agentLog
	notes    chan *mcp.Message
	logPath  string
	shutdown func()
}

func startGated(t *testing.T) *gatedSession {
	t.Helper()
	upIn, proxyToUp := io.Pipe()
	upOut, upToProxy := io.Pipe()
	downIn, agentOut := io.Pipe()
	downOut, proxyOut := io.Pipe()

	up := newGatedUpstream()
	go up.serve(mcp.NewConn(upIn, upToProxy))

	schemas, err := extract.LoadDir("../../../schemas")
	if err != nil {
		t.Fatal(err)
	}
	logPath := filepath.Join(t.TempDir(), "receipts.jsonl")
	rlog, err := store.Open(logPath, signer)
	if err != nil {
		t.Fatal(err)
	}
	upClient := mcp.NewClient(mcp.NewConn(upOut, proxyToUp))
	upClient.Logf = t.Logf
	srv := &Server{
		Down:      mcp.NewConn(downIn, proxyOut),
		Upstreams: []*Upstream{{Name: "gated", Client: upClient}},
		Schemas:   schemas,
		Log:       rlog,
		SessionID: "s-gated",
		Clock:     &clock.Wall{},
		Logf:      t.Logf,
	}
	done := make(chan error, 1)
	go func() { done <- srv.Run() }()

	alog := &agentLog{}
	agent := mcp.NewClient(mcp.NewConn(downOut, agentOut))
	agent.Logf = alog.logf
	notes := make(chan *mcp.Message, 16)
	agent.Handle(progressHandler{notes})
	if _, err := agent.Call("initialize", map[string]any{"protocolVersion": "2025-06-18"}); err != nil {
		t.Fatal(err)
	}
	var once sync.Once
	return &gatedSession{up: up, agent: agent, agentLog: alog, notes: notes, logPath: logPath, shutdown: func() {
		once.Do(func() {
			agentOut.Close()
			if err := <-done; err != nil {
				t.Errorf("proxy run: %v", err)
			}
			rlog.Close()
		})
	}}
}

func (g *gatedSession) call(ctx context.Context, tool string, meta map[string]any) <-chan error {
	out := make(chan error, 1)
	go func() {
		params := map[string]any{"name": tool, "arguments": map[string]any{"symbol": "NVDA"}}
		if meta != nil {
			params["_meta"] = meta
		}
		_, err := g.agent.CallContext(ctx, "tools/call", params)
		out <- err
	}()
	return out
}

func waitFor[T any](t *testing.T, ch <-chan T, what string) T {
	t.Helper()
	select {
	case v := <-ch:
		return v
	case <-time.After(5 * time.Second):
		t.Fatalf("timed out waiting for %s", what)
		var zero T
		return zero
	}
}

// TestSlowCallDoesNotBlockOthers pins #67: while one tools/call waits
// on its upstream, ping and other calls are answered, and every
// completed call is receipted under its own turn.
func TestSlowCallDoesNotBlockOthers(t *testing.T) {
	g := startGated(t)
	defer g.shutdown()

	slow := g.call(context.Background(), "slow_tool", nil)
	waitFor(t, g.up.started, "the slow call to reach the upstream")

	if _, err := g.agent.Call("ping", nil); err != nil {
		t.Fatalf("ping during a slow call: %v", err)
	}
	if err := waitFor(t, g.call(context.Background(), "get_indicators", nil), "a fast call during a slow one"); err != nil {
		t.Fatal(err)
	}
	close(g.up.release)
	if err := waitFor(t, slow, "the slow call"); err != nil {
		t.Fatal(err)
	}
	g.shutdown()

	receipts, err := store.ScanVerified(g.logPath, signtest.Keyring(signer))
	if err != nil {
		t.Fatal(err)
	}
	if len(receipts) != 2 {
		t.Fatalf("got %d receipts, want 2", len(receipts))
	}
	// Turns follow completion, not arrival: the fast call finished first.
	if receipts[0].ToolName != "get_indicators" || receipts[0].TurnIndex != 0 ||
		receipts[1].ToolName != "slow_tool" || receipts[1].TurnIndex != 1 {
		t.Fatalf("receipts %s/%d, %s/%d", receipts[0].ToolName, receipts[0].TurnIndex, receipts[1].ToolName, receipts[1].TurnIndex)
	}
}

// TestCancelledCallGetsNoReplyAndNoReceipt pins MCP cancellation
// through the proxy: the upstream is told to stop, under its own
// request id, and when it answers anyway the agent gets nothing and no
// receipt is written, because the agent never saw that result.
func TestCancelledCallGetsNoReplyAndNoReceipt(t *testing.T) {
	g := startGated(t)
	defer g.shutdown()

	ctx, cancel := context.WithCancel(context.Background())
	slow := g.call(ctx, "slow_tool", nil)
	upstreamID := waitFor(t, g.up.started, "the slow call to reach the upstream")
	cancel()
	if err := waitFor(t, slow, "the cancelled call"); !errors.Is(err, context.Canceled) {
		t.Fatalf("cancelled call returned %v", err)
	}
	if got := waitFor(t, g.up.cancels, "the upstream's cancellation"); string(got) != string(upstreamID) {
		t.Fatalf("upstream told to cancel %s, want its own id %s", got, upstreamID)
	}
	close(g.up.release) // the upstream answers anyway
	if err := waitFor(t, g.call(context.Background(), "get_indicators", nil), "a call after cancelling"); err != nil {
		t.Fatal(err)
	}
	g.shutdown()

	if strings.Contains(g.agentLog.String(), "unknown id") {
		t.Fatalf("the proxy answered a cancelled request:\n%s", g.agentLog)
	}
	receipts, err := store.Scan(g.logPath)
	if err != nil {
		t.Fatal(err)
	}
	if len(receipts) != 1 || receipts[0].ToolName != "get_indicators" {
		t.Fatalf("got %d receipts, want only the uncancelled call", len(receipts))
	}
}

// TestProgressReachesTheAgent pins progress forwarding: the agent's
// progressToken travels to the upstream in the params, and the
// upstream's progress notification comes back to the agent unchanged,
// before the result.
func TestProgressReachesTheAgent(t *testing.T) {
	g := startGated(t)
	defer g.shutdown()

	slow := g.call(context.Background(), "slow_tool", map[string]any{"progressToken": "tok-1"})
	note := waitFor(t, g.notes, "a progress notification")
	var p struct {
		ProgressToken string `json:"progressToken"`
		Progress      int    `json:"progress"`
	}
	if note.Method != "notifications/progress" || json.Unmarshal(note.Params, &p) != nil || p.ProgressToken != "tok-1" || p.Progress != 1 {
		t.Fatalf("agent got %s %s", note.Method, note.Params)
	}
	close(g.up.release)
	if err := waitFor(t, slow, "the slow call"); err != nil {
		t.Fatal(err)
	}
}

// TestConcurrentCallsGetDistinctTurns drives many calls at once through
// the proxy (run it under -race): each gets a receipt, turns are
// unique, and the chain stays intact.
func TestConcurrentCallsGetDistinctTurns(t *testing.T) {
	g := startGated(t)
	defer g.shutdown()

	const n = 20
	var calls []<-chan error
	for range n {
		calls = append(calls, g.call(context.Background(), "get_indicators", nil))
	}
	for i, c := range calls {
		if err := waitFor(t, c, "a concurrent call"); err != nil {
			t.Fatalf("call %d: %v", i, err)
		}
	}
	g.shutdown()

	receipts, err := store.ScanVerified(g.logPath, signtest.Keyring(signer))
	if err != nil {
		t.Fatal(err)
	}
	if len(receipts) != n {
		t.Fatalf("got %d receipts, want %d", len(receipts), n)
	}
	for i, r := range receipts {
		if r.TurnIndex != i {
			t.Fatalf("receipt %d has turn %d", i, r.TurnIndex)
		}
	}
}
