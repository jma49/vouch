// Package integration runs the proxy against real MCP servers rather
// than the in-process fakes the other packages use (docs/roadmap.md
// Phase 5). The fakes pin vouch's intent; these tests pin that the
// intent matches what the official SDK actually sends.
//
// The reference server is @modelcontextprotocol/server-everything,
// installed at a pinned version by `make integration`, which points
// $VOUCH_MCP_EVERYTHING at its entry script (run with node). Without
// that variable the tests skip.
package integration

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http/httptest"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/jma49/vouch/proxy/internal/clock"
	"github.com/jma49/vouch/proxy/internal/mcp"
	"github.com/jma49/vouch/proxy/internal/proxy"
	"github.com/jma49/vouch/proxy/internal/sign/signtest"
	"github.com/jma49/vouch/proxy/internal/store"
)

var signer = signtest.Signer(1)

// agent is the client side: it answers what the reference server asks
// for through the proxy (sampling, roots, elicitation) and records
// every notification and request it sees.
type agent struct {
	mu       sync.Mutex
	notes    []*mcp.Message
	requests []string
}

func (a *agent) HandleNotification(m *mcp.Message) {
	a.mu.Lock()
	defer a.mu.Unlock()
	a.notes = append(a.notes, m)
}

func (a *agent) HandleRequest(_ context.Context, m *mcp.Message) (json.RawMessage, error) {
	a.mu.Lock()
	a.requests = append(a.requests, m.Method)
	a.mu.Unlock()
	switch m.Method {
	case "sampling/createMessage":
		return json.RawMessage(`{"role":"assistant","content":{"type":"text","text":"sampled through vouch"},"model":"test-model","stopReason":"endTurn"}`), nil
	case "roots/list":
		return json.RawMessage(`{"roots":[{"uri":"file:///vouch-root","name":"vouch-root"}]}`), nil
	case "elicitation/create":
		return json.RawMessage(`{"action":"decline"}`), nil
	}
	return nil, &mcp.Error{Code: mcp.CodeMethodNotFound, Message: m.Method}
}

func (a *agent) sawNote(method string) []*mcp.Message {
	a.mu.Lock()
	defer a.mu.Unlock()
	var out []*mcp.Message
	for _, n := range a.notes {
		if n.Method == method {
			out = append(out, n)
		}
	}
	return out
}

func (a *agent) sawRequest(method string) bool {
	a.mu.Lock()
	defer a.mu.Unlock()
	for _, r := range a.requests {
		if r == method {
			return true
		}
	}
	return false
}

type session struct {
	client  *mcp.Client
	agent   *agent
	logPath string
	log     *store.Log
	stop    func() error
}

// start puts the reference server behind a proxy, exactly as `vouch
// proxy --upstream` does, and initializes it as an agent offering
// sampling, roots, and elicitation. Over stdio the proxy spawns the
// server and the agent talks to the proxy through pipes; over HTTP the
// server runs in its Streamable HTTP mode and the agent reaches the
// proxy through vouch's own HTTP server transport, so both of vouch's
// HTTP sides meet a real SDK or each other.
func start(t *testing.T, overHTTP bool) *session {
	t.Helper()
	script := os.Getenv("VOUCH_MCP_EVERYTHING")
	if script == "" {
		t.Skip("VOUCH_MCP_EVERYTHING is not set; run `make integration`")
	}
	// Quoted as --upstream would be: the path may contain spaces.
	upstream := "node '" + strings.ReplaceAll(script, "'", `'\''`) + "' stdio"
	if overHTTP {
		upstream = serveEverythingHTTP(t, script)
	}
	specs, err := proxy.ParseUpstreams([]string{"everything=" + upstream})
	if err != nil {
		t.Fatal(err)
	}
	up, err := proxy.Connect(specs[0], nil)
	if err != nil {
		t.Fatal(err)
	}
	if c, ok := up.Client.(*mcp.Client); ok {
		c.Logf = t.Logf
	}
	logPath := filepath.Join(t.TempDir(), "receipts.jsonl")
	rlog, err := store.Open(logPath, signer)
	if err != nil {
		t.Fatal(err)
	}
	var down mcp.Transport
	var agentSide mcp.Transport
	var hangUp func()
	if overHTTP {
		hs := mcp.NewHTTPServer(t.Logf)
		web := httptest.NewServer(hs)
		t.Cleanup(web.Close)
		hc := mcp.NewHTTPClient(web.URL, nil, t.Logf)
		down, agentSide, hangUp = hs, hc, func() { hc.Close() }
	} else {
		downIn, agentOut := io.Pipe()
		downOut, proxyOut := io.Pipe()
		down, agentSide, hangUp = mcp.NewConn(downIn, proxyOut), mcp.NewConn(downOut, agentOut), func() { agentOut.Close() }
	}
	srv := &proxy.Server{
		Down: down, Upstreams: []*proxy.Upstream{up},
		Log: rlog, SessionID: "s-everything", Clock: &clock.Wall{}, Logf: t.Logf,
	}
	done := make(chan error, 1)
	go func() { done <- srv.Run() }()

	a := &agent{}
	client := mcp.NewClient(agentSide)
	client.Logf = t.Logf
	client.Handle(a)
	res, err := client.Call("initialize", map[string]any{
		"protocolVersion": "2025-06-18",
		"capabilities": map[string]any{
			"sampling": map[string]any{}, "roots": map[string]any{"listChanged": true},
			"elicitation": map[string]any{"form": map[string]any{}},
		},
		"clientInfo": map[string]any{"name": "vouch-integration", "version": "0"},
	})
	if err != nil {
		t.Fatalf("initialize through the proxy: %v", err)
	}
	var init struct {
		ProtocolVersion string `json:"protocolVersion"`
	}
	if err := json.Unmarshal(res, &init); err != nil || init.ProtocolVersion != "2025-06-18" {
		t.Fatalf("initialize result %s", res)
	}
	if err := client.Notify("notifications/initialized", nil); err != nil {
		t.Fatal(err)
	}
	var once sync.Once
	var stopErr error
	s := &session{client: client, agent: a, logPath: logPath, log: rlog, stop: func() error {
		once.Do(func() {
			hangUp()
			stopErr = <-done
			if err := up.Close(); err != nil {
				t.Logf("closing the reference server: %v", err)
			}
		})
		return stopErr
	}}
	t.Cleanup(func() { s.stop(); rlog.Close() })
	return s
}

func (s *session) call(t *testing.T, ctx context.Context, tool string, args map[string]any, meta map[string]any) (string, error) {
	t.Helper()
	params := map[string]any{"name": tool, "arguments": args}
	if meta != nil {
		params["_meta"] = meta
	}
	res, err := s.client.CallContext(ctx, "tools/call", params)
	return string(res), err
}

// eventually polls cond, for effects that arrive asynchronously.
func eventually(t *testing.T, what string, cond func() bool) {
	t.Helper()
	deadline := time.Now().Add(10 * time.Second)
	for !cond() {
		if time.Now().After(deadline) {
			t.Fatalf("timed out waiting for %s", what)
		}
		time.Sleep(20 * time.Millisecond)
	}
}

// TestEverythingServer drives the reference server through the proxy:
// the tools that exist only because the agent offered sampling, roots,
// and elicitation (registered after initialize, announced by
// tools/list_changed), plain and structured results, progress, a slow
// call beside a fast one, a cancelled call, and finally the receipt
// log's integrity.
func TestEverythingServer(t *testing.T) {
	for _, tc := range []struct {
		name     string
		overHTTP bool
	}{{"stdio", false}, {"streamable HTTP", true}} {
		t.Run(tc.name, func(t *testing.T) { everything(t, start(t, tc.overHTTP)) })
	}
}

func everything(t *testing.T, s *session) {
	ctx := context.Background()

	// The server registers client-dependent tools after initialized
	// and says so with tools/list_changed; the proxy must refresh its
	// routes and pass the notification on.
	eventually(t, "tools/list_changed to reach the agent", func() bool {
		return len(s.agent.sawNote("notifications/tools/list_changed")) > 0
	})
	list, err := s.client.Call("tools/list", nil)
	if err != nil {
		t.Fatal(err)
	}
	for _, tool := range []string{"echo", "get-sum", "trigger-sampling-request", "get-roots-list", "trigger-elicitation-request"} {
		if !strings.Contains(string(list), `"name":"`+tool+`"`) {
			t.Fatalf("tools/list through the proxy lacks %s", tool)
		}
	}

	receipted := 0
	t.Run("plain result", func(t *testing.T) {
		res, err := s.call(t, ctx, "get-sum", map[string]any{"a": 2, "b": 3}, nil)
		if err != nil || !strings.Contains(res, "5") {
			t.Fatalf("get-sum: %s, %v", res, err)
		}
		receipted++
	})
	t.Run("structured result", func(t *testing.T) {
		res, err := s.call(t, ctx, "get-structured-content", map[string]any{"location": "New York"}, nil)
		if err != nil || !strings.Contains(res, "structuredContent") {
			t.Fatalf("get-structured-content: %s, %v", res, err)
		}
		receipted++
	})
	t.Run("sampling is forwarded to the agent", func(t *testing.T) {
		res, err := s.call(t, ctx, "trigger-sampling-request", map[string]any{"prompt": "hi", "maxTokens": 5}, nil)
		if err != nil || !strings.Contains(res, "sampled through vouch") {
			t.Fatalf("trigger-sampling-request: %s, %v", res, err)
		}
		receipted++
	})
	t.Run("roots are forwarded to the agent", func(t *testing.T) {
		res, err := s.call(t, ctx, "get-roots-list", map[string]any{}, nil)
		if err != nil || !strings.Contains(res, "vouch-root") {
			t.Fatalf("get-roots-list: %s, %v", res, err)
		}
		if !s.agent.sawRequest("roots/list") {
			t.Fatal("the agent was never asked for roots")
		}
		receipted++
	})
	t.Run("elicitation is forwarded to the agent", func(t *testing.T) {
		res, err := s.call(t, ctx, "trigger-elicitation-request", map[string]any{}, nil)
		if err != nil || !strings.Contains(res, "decline") {
			t.Fatalf("trigger-elicitation-request: %s, %v", res, err)
		}
		receipted++
	})
	t.Run("progress reaches the agent", func(t *testing.T) {
		before := len(s.agent.sawNote("notifications/progress"))
		_, err := s.call(t, ctx, "trigger-long-running-operation", map[string]any{"duration": 0.3, "steps": 3},
			map[string]any{"progressToken": "p-1"})
		if err != nil {
			t.Fatal(err)
		}
		eventually(t, "three progress notifications", func() bool {
			return len(s.agent.sawNote("notifications/progress"))-before >= 3
		})
		receipted++
	})
	t.Run("a slow call does not block a fast one", func(t *testing.T) {
		slow := make(chan error, 1)
		go func() {
			_, err := s.call(t, ctx, "trigger-long-running-operation", map[string]any{"duration": 1.5, "steps": 1}, nil)
			slow <- err
		}()
		time.Sleep(100 * time.Millisecond)
		startFast := time.Now()
		if _, err := s.call(t, ctx, "echo", map[string]any{"message": "fast"}, nil); err != nil {
			t.Fatal(err)
		}
		if waited := time.Since(startFast); waited > time.Second {
			t.Fatalf("echo waited %v behind a slow call", waited)
		}
		if err := <-slow; err != nil {
			t.Fatal(err)
		}
		receipted += 2
	})
	t.Run("a cancelled call is not receipted", func(t *testing.T) {
		cctx, cancel := context.WithTimeout(ctx, 200*time.Millisecond)
		defer cancel()
		_, err := s.call(t, cctx, "trigger-long-running-operation", map[string]any{"duration": 3, "steps": 1}, nil)
		if !errors.Is(err, context.DeadlineExceeded) {
			t.Fatalf("got %v, want the call cancelled", err)
		}
	})

	if err := s.stop(); err != nil {
		t.Fatalf("proxy run: %v", err)
	}
	if _, err := s.log.Seal("s-everything", time.Now()); err != nil {
		t.Fatal(err)
	}
	audit, err := store.Verify(s.logPath, signtest.Keyring(signer))
	if err != nil {
		t.Fatal(err)
	}
	if len(audit.Receipts) != receipted || !audit.Sealed {
		t.Fatalf("log has %d receipts (sealed %v), want %d", len(audit.Receipts), audit.Sealed, receipted)
	}
	for _, r := range audit.Receipts {
		if r.ToolName == "get-structured-content" && r.PayloadSource != "structuredContent" {
			t.Fatalf("structured result receipted from %s", r.PayloadSource)
		}
	}
}

// serveEverythingHTTP runs the reference server in its Streamable HTTP
// mode on a free loopback port and returns its endpoint URL.
func serveEverythingHTTP(t *testing.T, script string) string {
	t.Helper()
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	port := ln.Addr().(*net.TCPAddr).Port
	ln.Close()
	cmd := exec.Command("node", script, "streamableHttp")
	cmd.Env = append(os.Environ(), fmt.Sprintf("PORT=%d", port))
	cmd.Stderr = os.Stderr
	if err := cmd.Start(); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = cmd.Process.Kill(); _ = cmd.Wait() })
	addr := fmt.Sprintf("127.0.0.1:%d", port)
	eventually(t, "the reference server to listen", func() bool {
		c, err := net.Dial("tcp", addr)
		if err == nil {
			c.Close()
		}
		return err == nil
	})
	return "http://" + addr + "/mcp"
}
