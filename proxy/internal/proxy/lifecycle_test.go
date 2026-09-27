package proxy

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/jma49/vouch/proxy/internal/clock"
	"github.com/jma49/vouch/proxy/internal/mcp"
	"github.com/jma49/vouch/proxy/internal/store"
)

// pagedUpstream serves its tools two to a page, and never answers
// initialize when hang is set.
type pagedUpstream struct {
	tools []string
	hang  bool
}

func (p *pagedUpstream) HandleNotification(*mcp.Message) {}

func (p *pagedUpstream) HandleRequest(ctx context.Context, m *mcp.Message) (json.RawMessage, error) {
	switch m.Method {
	case "initialize":
		if p.hang {
			<-ctx.Done()
			return nil, ctx.Err()
		}
		return json.RawMessage(`{"protocolVersion":"2025-06-18","capabilities":{"tools":{}}}`), nil
	case "tools/list":
		var params struct {
			Cursor string `json:"cursor"`
		}
		_ = json.Unmarshal(m.Params, &params)
		start := 0
		fmt.Sscanf(params.Cursor, "page-%d", &start)
		end := min(start+2, len(p.tools))
		var list []map[string]any
		for _, name := range p.tools[start:end] {
			list = append(list, map[string]any{"name": name, "inputSchema": map[string]any{"type": "object"}})
		}
		res := map[string]any{"tools": list}
		if end < len(p.tools) {
			res["nextCursor"] = fmt.Sprintf("page-%d", end)
		}
		return json.Marshal(res)
	case "tools/call":
		return json.RawMessage(`{"content":[{"type":"text","text":"ok"}]}`), nil
	}
	return nil, &mcp.Error{Code: mcp.CodeMethodNotFound, Message: m.Method}
}

func startPaged(t *testing.T, up *pagedUpstream) (*mcp.Client, func() error) {
	t.Helper()
	upIn, proxyToUp := io.Pipe()
	upOut, upToProxy := io.Pipe()
	t.Cleanup(func() { proxyToUp.Close(); upToProxy.Close() })
	server := mcp.NewClient(mcp.NewConn(upIn, upToProxy))
	server.Handle(up)
	server.Start()
	downIn, agentOut := io.Pipe()
	downOut, proxyOut := io.Pipe()
	rlog, err := store.Open(filepath.Join(t.TempDir(), "receipts.jsonl"), signer)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { rlog.Close() })
	srv := &Server{
		Down: mcp.NewConn(downIn, proxyOut), Log: rlog, SessionID: "s-paged", Clock: &clock.Wall{}, Logf: t.Logf,
		Upstreams: []*Upstream{{Name: "paged", Client: mcp.NewClient(mcp.NewConn(upOut, proxyToUp))}},
	}
	done := make(chan error, 1)
	go func() { done <- srv.Run() }()
	return mcp.NewClient(mcp.NewConn(downOut, agentOut)), func() error {
		agentOut.Close()
		select {
		case err := <-done:
			return err
		case <-time.After(5 * time.Second):
			return errors.New("Run did not return")
		}
	}
}

// TestToolsListFollowsEveryPage pins #100: tools on later pages are
// listed and routable, and the agent gets them in one page.
func TestToolsListFollowsEveryPage(t *testing.T) {
	agent, stop := startPaged(t, &pagedUpstream{tools: []string{"a", "b", "c", "d", "e"}})
	if _, err := agent.Call("initialize", map[string]any{"protocolVersion": "2025-06-18"}); err != nil {
		t.Fatal(err)
	}
	res, err := agent.Call("tools/list", nil)
	if err != nil {
		t.Fatal(err)
	}
	var list struct {
		Tools      []struct{ Name string } `json:"tools"`
		NextCursor string                  `json:"nextCursor"`
	}
	if err := json.Unmarshal(res, &list); err != nil || len(list.Tools) != 5 || list.NextCursor != "" {
		t.Fatalf("tools/list: %s", res)
	}
	if _, err := agent.Call("tools/call", map[string]any{"name": "e"}); err != nil {
		t.Fatalf("tool on the last page: %v", err)
	}
	_, err = agent.Call("tools/list", map[string]any{"cursor": "page-2"})
	var rpcErr *mcp.Error
	if !errors.As(err, &rpcErr) || rpcErr.Code != mcp.CodeInvalidParams {
		t.Fatalf("agent cursor: %v, want invalid params", err)
	}
	if err := stop(); err != nil {
		t.Fatal(err)
	}
}

// TestHungInitializeTimesOut pins #100: an upstream that never answers
// initialize gets a bounded wait, and the session still ends.
func TestHungInitializeTimesOut(t *testing.T) {
	old := upstreamTimeout
	upstreamTimeout = 200 * time.Millisecond
	t.Cleanup(func() { upstreamTimeout = old })
	agent, stop := startPaged(t, &pagedUpstream{hang: true})
	_, err := agent.Call("initialize", map[string]any{"protocolVersion": "2025-06-18"})
	if err == nil || !strings.Contains(err.Error(), "deadline") {
		t.Fatalf("initialize: %v, want a timeout", err)
	}
	if err := stop(); err != nil {
		t.Fatal(err)
	}
}
