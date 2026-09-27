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

	"github.com/jma49/vouch/proxy/internal/clock"
	"github.com/jma49/vouch/proxy/internal/mcp"
	"github.com/jma49/vouch/proxy/internal/store"
)

// peerServer is a fake upstream built on mcp.Client in the server role,
// so a tool can call back into the proxy the way a real upstream asks
// its client for sampling or roots.
type peerServer struct {
	conn    *mcp.Client
	version string

	mu     sync.Mutex
	tools  []string
	onCall func(ctx context.Context, tool string) (string, error)
	notes  chan *mcp.Message
}

func (p *peerServer) HandleNotification(m *mcp.Message) { p.notes <- m }

func (p *peerServer) HandleRequest(ctx context.Context, m *mcp.Message) (json.RawMessage, error) {
	p.mu.Lock()
	tools, onCall := append([]string(nil), p.tools...), p.onCall
	p.mu.Unlock()
	switch m.Method {
	case "initialize":
		return json.Marshal(map[string]any{"protocolVersion": p.version, "capabilities": map[string]any{"tools": map[string]any{"listChanged": true}}})
	case "tools/list":
		var list []map[string]any
		for _, name := range tools {
			list = append(list, map[string]any{"name": name, "inputSchema": map[string]any{"type": "object"}})
		}
		return json.Marshal(map[string]any{"tools": list})
	case "tools/call":
		var params struct {
			Name string `json:"name"`
		}
		_ = json.Unmarshal(m.Params, &params)
		text := "ok"
		if onCall != nil {
			var err error
			if text, err = onCall(ctx, params.Name); err != nil {
				return nil, err
			}
		}
		return json.Marshal(map[string]any{"content": []map[string]any{{"type": "text", "text": text}}})
	}
	return nil, &mcp.Error{Code: mcp.CodeMethodNotFound, Message: m.Method}
}

// agentPeer answers the proxy's requests to the agent with answer.
type agentPeer struct {
	notes  chan *mcp.Message
	asked  chan *mcp.Message
	answer func(ctx context.Context, m *mcp.Message) (json.RawMessage, error)
}

func (a *agentPeer) HandleNotification(m *mcp.Message) { a.notes <- m }
func (a *agentPeer) HandleRequest(ctx context.Context, m *mcp.Message) (json.RawMessage, error) {
	a.asked <- m
	return a.answer(ctx, m)
}

type fwdSession struct {
	ups      []*peerServer
	agent    *mcp.Client
	peer     *agentPeer
	logPath  string
	done     chan error
	closeOut func()
}

// startForwarding wires a proxy between one peerServer per version and
// an agent whose requests-from-the-proxy are answered by answer. It
// does not initialize.
func startForwarding(t *testing.T, versions []string, answer func(context.Context, *mcp.Message) (json.RawMessage, error)) *fwdSession {
	t.Helper()
	var ups []*peerServer
	var upstreams []*Upstream
	for i, v := range versions {
		upIn, proxyToUp := io.Pipe()
		upOut, upToProxy := io.Pipe()
		t.Cleanup(func() { proxyToUp.Close(); upToProxy.Close() })
		ps := &peerServer{conn: mcp.NewClient(mcp.NewConn(upIn, upToProxy)), version: v,
			tools: []string{fmt.Sprintf("tool_%d", i)}, notes: make(chan *mcp.Message, 16)}
		ps.conn.Logf = t.Logf
		ps.conn.Handle(ps)
		ps.conn.Start()
		ups = append(ups, ps)
		c := mcp.NewClient(mcp.NewConn(upOut, proxyToUp))
		c.Logf = t.Logf
		upstreams = append(upstreams, &Upstream{Name: fmt.Sprintf("up%d", i), Client: c})
	}
	downIn, agentOut := io.Pipe()
	downOut, proxyOut := io.Pipe()
	logPath := filepath.Join(t.TempDir(), "receipts.jsonl")
	rlog, err := store.Open(logPath, signer)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { rlog.Close() })
	srv := &Server{
		Down: mcp.NewConn(downIn, proxyOut), Upstreams: upstreams, Log: rlog,
		SessionID: "s-fwd", Clock: &clock.Wall{}, Logf: t.Logf,
	}
	done := make(chan error, 1)
	go func() { done <- srv.Run() }()

	peer := &agentPeer{notes: make(chan *mcp.Message, 16), asked: make(chan *mcp.Message, 16), answer: answer}
	agent := mcp.NewClient(mcp.NewConn(downOut, agentOut))
	agent.Logf = t.Logf
	agent.Handle(peer)
	return &fwdSession{ups: ups, agent: agent, peer: peer, logPath: logPath, done: done,
		closeOut: func() { agentOut.Close() }}
}

func (f *fwdSession) initialize(t *testing.T, version string) (string, error) {
	t.Helper()
	res, err := f.agent.Call("initialize", map[string]any{"protocolVersion": version, "capabilities": map[string]any{"sampling": map[string]any{}}})
	if err != nil {
		return "", err
	}
	var r struct {
		ProtocolVersion string `json:"protocolVersion"`
	}
	_ = json.Unmarshal(res, &r)
	return r.ProtocolVersion, nil
}

func (f *fwdSession) stop(t *testing.T) {
	t.Helper()
	f.closeOut()
	if err := waitFor(t, f.done, "the proxy to stop"); err != nil {
		t.Errorf("proxy run: %v", err)
	}
}

// TestSamplingIsForwardedToTheAgent pins #68: an upstream's sampling
// request reaches the agent under a proxy id, the agent's answer goes
// back to the upstream, and the tool call that needed it completes and
// is receipted.
func TestSamplingIsForwardedToTheAgent(t *testing.T) {
	f := startForwarding(t, []string{"2025-06-18"}, func(context.Context, *mcp.Message) (json.RawMessage, error) {
		return json.RawMessage(`{"role":"assistant","content":{"type":"text","text":"sampled"},"model":"m"}`), nil
	})
	f.ups[0].onCall = func(ctx context.Context, _ string) (string, error) {
		res, err := f.ups[0].conn.CallContext(ctx, "sampling/createMessage", map[string]any{"messages": []any{}, "maxTokens": 5})
		if err != nil {
			return "", err
		}
		return string(res), nil
	}
	if _, err := f.initialize(t, "2025-06-18"); err != nil {
		t.Fatal(err)
	}
	res, err := f.agent.Call("tools/call", map[string]any{"name": "tool_0", "arguments": map[string]any{}})
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(string(res), "sampled") {
		t.Fatalf("tool result %s does not carry the agent's answer", res)
	}
	asked := <-f.peer.asked
	if asked.Method != "sampling/createMessage" || !strings.HasPrefix(string(asked.ID), `"vouch-`) {
		t.Fatalf("agent was asked %s with id %s", asked.Method, asked.ID)
	}
	f.stop(t)
	if receipts, err := store.Scan(f.logPath); err != nil || len(receipts) != 1 {
		t.Fatalf("receipts: %d, %v", len(receipts), err)
	}
}

// TestUpstreamCancelReachesTheAgent pins cancellation of a forwarded
// request: when the upstream gives up, the agent is told to stop under
// the id the proxy gave it.
func TestUpstreamCancelReachesTheAgent(t *testing.T) {
	stopped := make(chan struct{})
	f := startForwarding(t, []string{"2025-06-18"}, func(ctx context.Context, _ *mcp.Message) (json.RawMessage, error) {
		<-ctx.Done()
		close(stopped)
		return nil, ctx.Err()
	})
	f.ups[0].onCall = func(context.Context, string) (string, error) {
		ctx, cancel := context.WithCancel(context.Background())
		go func() { <-f.peer.asked; cancel() }()
		_, err := f.ups[0].conn.CallContext(ctx, "sampling/createMessage", map[string]any{})
		if !errors.Is(err, context.Canceled) {
			return "", fmt.Errorf("sampling returned %v, want cancelled", err)
		}
		return "gave up", nil
	}
	if _, err := f.initialize(t, "2025-06-18"); err != nil {
		t.Fatal(err)
	}
	if _, err := f.agent.Call("tools/call", map[string]any{"name": "tool_0"}); err != nil {
		t.Fatal(err)
	}
	waitFor(t, stopped, "the agent's handler to be cancelled")
	f.stop(t)
}

// TestAgentLeavingFailsForwardedRequests pins that a forwarded request
// the agent will never answer cannot hold the proxy open: when the
// agent disconnects, the upstream's request fails and Run returns.
func TestAgentLeavingFailsForwardedRequests(t *testing.T) {
	f := startForwarding(t, []string{"2025-06-18"}, func(ctx context.Context, _ *mcp.Message) (json.RawMessage, error) {
		<-ctx.Done() // never answers
		return nil, ctx.Err()
	})
	upstreamErr := make(chan error, 1)
	f.ups[0].onCall = func(ctx context.Context, _ string) (string, error) {
		_, err := f.ups[0].conn.CallContext(ctx, "sampling/createMessage", map[string]any{})
		upstreamErr <- err
		return "", err
	}
	if _, err := f.initialize(t, "2025-06-18"); err != nil {
		t.Fatal(err)
	}
	go func() { _, _ = f.agent.Call("tools/call", map[string]any{"name": "tool_0"}) }() // abandoned on purpose
	waitFor(t, f.peer.asked, "the forwarded request")
	f.stop(t)
	if err := waitFor(t, upstreamErr, "the upstream's request to fail"); err == nil {
		t.Fatal("the upstream's request succeeded after the agent left")
	}
}

// TestOnlyClientFeaturesAreForwarded pins the allowlist: ping is
// answered by the proxy, and a request the agent never agreed to
// answer is refused without reaching it.
func TestOnlyClientFeaturesAreForwarded(t *testing.T) {
	f := startForwarding(t, []string{"2025-06-18"}, func(context.Context, *mcp.Message) (json.RawMessage, error) {
		return json.RawMessage(`{}`), nil
	})
	if _, err := f.initialize(t, "2025-06-18"); err != nil {
		t.Fatal(err)
	}
	if res, err := f.ups[0].conn.Call("ping", nil); err != nil || string(res) != `{}` {
		t.Fatalf("ping from upstream: %s, %v", res, err)
	}
	_, err := f.ups[0].conn.Call("tools/call", map[string]any{"name": "agent_tool"})
	var rpcErr *mcp.Error
	if !errors.As(err, &rpcErr) || rpcErr.Code != mcp.CodeMethodNotFound {
		t.Fatalf("got %v, want method not found", err)
	}
	select {
	case m := <-f.peer.asked:
		t.Fatalf("agent was asked %s", m.Method)
	default:
	}
	f.stop(t)
}

// TestToolsListChangedRefreshesRoutes pins that a tool an upstream adds
// is routable by the time the agent hears it changed.
func TestToolsListChangedRefreshesRoutes(t *testing.T) {
	f := startForwarding(t, []string{"2025-06-18"}, nil)
	if _, err := f.initialize(t, "2025-06-18"); err != nil {
		t.Fatal(err)
	}
	f.ups[0].mu.Lock()
	f.ups[0].tools = append(f.ups[0].tools, "new_tool")
	f.ups[0].mu.Unlock()
	if err := f.ups[0].conn.Notify("notifications/tools/list_changed", nil); err != nil {
		t.Fatal(err)
	}
	if n := waitFor(t, f.peer.notes, "list_changed at the agent"); n.Method != "notifications/tools/list_changed" {
		t.Fatalf("agent got %s", n.Method)
	}
	if _, err := f.agent.Call("tools/call", map[string]any{"name": "new_tool"}); err != nil {
		t.Fatalf("calling the new tool: %v", err)
	}
	f.stop(t)
}

// TestRootsListChangedReachesUpstreams pins the agent-to-upstream
// direction of client notifications.
func TestRootsListChangedReachesUpstreams(t *testing.T) {
	f := startForwarding(t, []string{"2025-06-18", "2025-06-18"}, nil)
	if _, err := f.initialize(t, "2025-06-18"); err != nil {
		t.Fatal(err)
	}
	if err := f.agent.Notify("notifications/roots/list_changed", nil); err != nil {
		t.Fatal(err)
	}
	for i, up := range f.ups {
		if n := waitFor(t, up.notes, "roots/list_changed upstream"); n.Method != "notifications/roots/list_changed" {
			t.Fatalf("upstream %d got %s", i, n.Method)
		}
	}
	f.stop(t)
}

// TestProtocolVersionNegotiation pins that the proxy answers with the
// version its upstreams chose, and refuses to guess when they disagree
// or chose one it cannot relay.
func TestProtocolVersionNegotiation(t *testing.T) {
	cases := []struct {
		name      string
		upstreams []string
		requested string
		want      string
		wantErr   string
	}{
		{"agreeing upstreams", []string{"2025-06-18", "2025-06-18"}, "2025-06-18", "2025-06-18", ""},
		{"upstream downgrades", []string{"2025-03-26"}, "2025-06-18", "2025-03-26", ""},
		{"upstreams disagree", []string{"2025-06-18", "2025-03-26"}, "2025-06-18", "", "different protocol versions"},
		{"unsupported version", []string{"1999-01-01"}, "2025-06-18", "", "not supported"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			f := startForwarding(t, tc.upstreams, nil)
			got, err := f.initialize(t, tc.requested)
			if tc.wantErr != "" {
				if err == nil || !strings.Contains(err.Error(), tc.wantErr) {
					t.Fatalf("got %q, %v; want error %q", got, err, tc.wantErr)
				}
			} else if err != nil || got != tc.want {
				t.Fatalf("got %q, %v; want %q", got, err, tc.want)
			}
			f.stop(t)
		})
	}
}
