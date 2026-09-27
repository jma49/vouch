package proxy

import (
	"encoding/json"
	"errors"
	"io"
	"strings"
	"testing"

	"github.com/jma49/vouch/proxy/internal/mcp"
	"github.com/jma49/vouch/proxy/internal/store"
)

// TestBadFramesAreAnsweredAndServingContinues pins JSON-RPC 2.0
// section 5.1: a frame the proxy cannot parse gets an error with id
// null, and the session keeps going.
func TestBadFramesAreAnsweredAndServingContinues(t *testing.T) {
	cases := []struct {
		name string
		line string
		code int
	}{
		{"not json", "not json", mcp.CodeParseError},
		{"batch", `[{"jsonrpc":"2.0","id":2,"method":"ping"}]`, mcp.CodeInvalidRequest},
		{"not an object", `"ping"`, mcp.CodeInvalidRequest},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			s := startSession(t)
			defer s.shutdown()

			if _, err := io.WriteString(s.send, tc.line+"\n"); err != nil {
				t.Fatal(err)
			}
			m, err := s.raw.Read()
			if err != nil {
				t.Fatalf("read error reply: %v", err)
			}
			if string(m.ID) != "null" || m.Error == nil || m.Error.Code != tc.code {
				t.Fatalf("reply: id=%s error=%+v, want id null code %d", m.ID, m.Error, tc.code)
			}
			if _, err := s.agent.Call("ping", nil); err != nil {
				t.Fatalf("ping after bad frame: %v", err)
			}
		})
	}
}

// TestNoReplyWithoutRequest pins JSON-RPC 2.0 section 4.1: a
// notification is never answered, and a tools/call notification is not
// executed (the agent could never see its result, so a receipt for it
// would attest to data nobody received). A message without a method is
// not a request and gets no reply either.
func TestNoReplyWithoutRequest(t *testing.T) {
	cases := []struct {
		name string
		line string
	}{
		{"tools/call notification", `{"jsonrpc":"2.0","method":"tools/call","params":{"name":"get_indicators","arguments":{"symbol":"NVDA"}}}`},
		{"ping notification", `{"jsonrpc":"2.0","method":"ping"}`},
		{"initialize notification", `{"jsonrpc":"2.0","method":"initialize","params":{}}`},
		{"tools/list notification", `{"jsonrpc":"2.0","method":"tools/list"}`},
		{"unknown notification", `{"jsonrpc":"2.0","method":"notifications/cancelled","params":{}}`},
		{"response from agent", `{"jsonrpc":"2.0","id":5,"result":{}}`},
		{"error response from agent", `{"jsonrpc":"2.0","id":6,"error":{"code":-1,"message":"no"}}`},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			s := startSession(t)
			if _, err := s.agent.Call("initialize", map[string]any{"protocolVersion": "2025-06-18"}); err != nil {
				t.Fatal(err)
			}
			if _, err := io.WriteString(s.send, tc.line+"\n"+`{"jsonrpc":"2.0","id":"after","method":"ping"}`+"\n"); err != nil {
				t.Fatal(err)
			}
			m, err := s.raw.Read()
			if err != nil {
				t.Fatal(err)
			}
			if string(m.ID) != `"after"` {
				t.Fatalf("first reply is %+v (id %s), want the ping reply", m, m.ID)
			}
			s.shutdown()

			receipts, err := store.Scan(s.logPath)
			if err != nil {
				t.Fatal(err)
			}
			if len(receipts) != 0 {
				t.Fatalf("got %d receipts, want none", len(receipts))
			}
		})
	}
}

// TestAmbiguousToolsCallParamsRejected pins that params the proxy and
// the upstream could read differently are refused before the upstream
// is called: with a duplicate "name", Go routes and receipts the last
// value while the upstream may execute the first.
func TestAmbiguousToolsCallParamsRejected(t *testing.T) {
	cases := []struct {
		name   string
		params string
	}{
		{"duplicate tool name", `{"name":"nope","name":"get_indicators","arguments":{"symbol":"NVDA"}}`},
		{"duplicate argument", `{"name":"get_indicators","arguments":{"symbol":"AMD","symbol":"NVDA"}}`},
		{"lone surrogate argument", `{"name":"get_indicators","arguments":{"symbol":"\ud800"}}`},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			s := startSession(t)
			if _, err := s.agent.Call("initialize", map[string]any{"protocolVersion": "2025-06-18"}); err != nil {
				t.Fatal(err)
			}
			_, err := s.agent.Call("tools/call", json.RawMessage(tc.params))
			var rpcErr *mcp.Error
			if !errors.As(err, &rpcErr) || rpcErr.Code != mcp.CodeInvalidParams {
				t.Fatalf("got %v, want invalid params", err)
			}
			s.shutdown()
			if receipts, err := store.Scan(s.logPath); err != nil || len(receipts) != 0 {
				t.Fatalf("receipts: %d, %v; want none", len(receipts), err)
			}
		})
	}
}

// TestAmbiguousResultFailsCall pins invariant 2 for results: a payload
// that cannot be canonicalized without changing its meaning yields no
// receipt, so the call fails.
func TestAmbiguousResultFailsCall(t *testing.T) {
	s, _ := recordingServer(t)
	result := json.RawMessage(`{"content":[{"type":"text","text":"{\"rsi_14\":99,\"rsi_14\":10}"}]}`)
	err := s.record("get_indicators", json.RawMessage(`{"symbol":"NVDA"}`), result, 0)
	if err == nil || !strings.Contains(err.Error(), "duplicate key") {
		t.Fatalf("got %v, want duplicate key error", err)
	}
}

func TestOversizedDownstreamFrameIsAnswered(t *testing.T) {
	s := startSession(t)
	defer s.shutdown()

	errc := make(chan error, 1)
	go func() {
		_, err := io.WriteString(s.send, `{"pad":"`+strings.Repeat("x", mcp.MaxFrame)+`"}`+"\n")
		errc <- err
	}()
	m, err := s.raw.Read()
	if err != nil {
		t.Fatalf("read error reply: %v", err)
	}
	if err := <-errc; err != nil {
		t.Fatal(err)
	}
	if string(m.ID) != "null" || m.Error == nil || m.Error.Code != mcp.CodeInvalidRequest {
		t.Fatalf("reply: id=%s error=%+v", m.ID, m.Error)
	}
	if _, err := s.agent.Call("ping", nil); err != nil {
		t.Fatalf("ping after oversized frame: %v", err)
	}
}
