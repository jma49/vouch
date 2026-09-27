package mcp

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"io"
	"strings"
	"testing"
	"time"
)

// TestConnReadRecoversFromBadFrames pins that a malformed line is a
// per-frame error, not a dead connection: the next well-formed line
// must still be readable.
func TestConnReadRecoversFromBadFrames(t *testing.T) {
	cases := []struct {
		name     string
		line     string
		wantCode int
		tooLarge bool
	}{
		{"not json", "not json", CodeParseError, false},
		{"truncated object", `{"jsonrpc":"2.0","id":1`, CodeParseError, false},
		{"batch", `[{"jsonrpc":"2.0","id":2,"method":"ping"}]`, CodeInvalidRequest, false},
		{"scalar", `42`, CodeInvalidRequest, false},
		{"wrong member type", `{"jsonrpc":"2.0","id":3,"method":7}`, CodeInvalidRequest, false},
		{"oversized", `{"pad":"` + strings.Repeat("x", 200) + `"}`, CodeInvalidRequest, true},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			in := tc.line + "\n" + `{"jsonrpc":"2.0","id":9,"method":"ping"}` + "\n"
			c := NewConn(strings.NewReader(in), io.Discard)
			c.maxFrame = 128

			_, err := c.Read()
			var fe *FrameError
			if !errors.As(err, &fe) {
				t.Fatalf("got %v, want *FrameError", err)
			}
			if fe.Code != tc.wantCode {
				t.Fatalf("code %d, want %d (%v)", fe.Code, tc.wantCode, err)
			}
			if got := errors.Is(err, ErrFrameTooLarge); got != tc.tooLarge {
				t.Fatalf("errors.Is(ErrFrameTooLarge) = %v, want %v", got, tc.tooLarge)
			}

			m, err := c.Read()
			if err != nil {
				t.Fatalf("read after bad frame: %v", err)
			}
			if m.Method != "ping" || string(m.ID) != "9" {
				t.Fatalf("read after bad frame: %+v", m)
			}
			if _, err := c.Read(); !errors.Is(err, io.EOF) {
				t.Fatalf("got %v, want EOF", err)
			}
		})
	}
}

func TestConnReadUnterminatedFinalLine(t *testing.T) {
	c := NewConn(strings.NewReader(`{"jsonrpc":"2.0","id":1,"method":"ping"}`), io.Discard)
	m, err := c.Read()
	if err != nil || m.Method != "ping" {
		t.Fatalf("got %+v, %v", m, err)
	}
	if _, err := c.Read(); !errors.Is(err, io.EOF) {
		t.Fatalf("got %v, want EOF", err)
	}
}

// scriptedUpstream answers every request with the raw lines noise
// returns for its id, then the real response {"n": id}. Output goes
// through a buffered queue because io.Pipe, unlike an OS pipe, has no
// buffer: a response the client has stopped waiting for would
// otherwise block the upstream from reading the next request.
func scriptedUpstream(r io.Reader, w io.Writer, noise func(id string) []string) {
	out := make(chan string, 64)
	defer close(out)
	go func() {
		for line := range out {
			if _, err := io.WriteString(w, line+"\n"); err != nil {
				return
			}
		}
	}()
	conn := NewConn(r, io.Discard)
	for {
		m, err := conn.Read()
		if err != nil {
			return
		}
		if m.IsNotification() {
			continue
		}
		for _, line := range noise(string(m.ID)) {
			out <- line
		}
		out <- `{"jsonrpc":"2.0","id":` + string(m.ID) + `,"result":{"n":` + string(m.ID) + `}}`
	}
}

func newScriptedClient(t *testing.T, noise func(id string) []string) *Client {
	t.Helper()
	upIn, clientToUp := io.Pipe()
	clientIn, upToClient := io.Pipe()
	t.Cleanup(func() { clientToUp.Close(); upToClient.Close() })
	go scriptedUpstream(upIn, upToClient, noise)
	c := NewClient(NewConn(clientIn, clientToUp))
	c.Logf = t.Logf
	return c
}

// TestClientCallSurvivesNoise pins that stray output from an upstream
// costs nothing: every call still gets its own response.
func TestClientCallSurvivesNoise(t *testing.T) {
	cases := []struct {
		name  string
		noise func(id string) []string
	}{
		{"log line before first response", func(id string) []string {
			if id == "1" {
				return []string{"INFO: warming cache"}
			}
			return nil
		}},
		{"garbage before every response", func(string) []string {
			return []string{"WARN: slow disk", `{"not":"jsonrpc"`, `[1,2]`}
		}},
		{"stale response replayed", func(id string) []string {
			if id == "1" {
				return nil
			}
			return []string{`{"jsonrpc":"2.0","id":1,"result":{"n":1}}`}
		}},
		{"unknown id", func(string) []string {
			return []string{`{"jsonrpc":"2.0","id":"x","result":{}}`}
		}},
		{"notification", func(string) []string {
			return []string{`{"jsonrpc":"2.0","method":"notifications/message","params":{}}`}
		}},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			c := newScriptedClient(t, tc.noise)
			for want := 1; want <= 3; want++ {
				res, err := c.Call("tools/call", nil)
				if err != nil {
					t.Fatalf("call %d: %v", want, err)
				}
				var got struct{ N int }
				if err := json.Unmarshal(res, &got); err != nil || got.N != want {
					t.Fatalf("call %d: got result %s", want, res)
				}
			}
		})
	}
}

// TestClientCallNullIDErrorFailsCall pins that an upstream's answer to
// a request it could not parse ends the pending call instead of
// leaving it waiting for a response that will never come.
func TestClientCallNullIDErrorFailsCall(t *testing.T) {
	c := newScriptedClient(t, func(id string) []string {
		if id == "1" {
			return []string{`{"jsonrpc":"2.0","id":null,"error":{"code":-32700,"message":"parse error"}}`}
		}
		return nil
	})
	_, err := c.Call("tools/call", nil)
	var rpcErr *Error
	if !errors.As(err, &rpcErr) || rpcErr.Code != CodeParseError {
		t.Fatalf("got %v, want the upstream's parse error", err)
	}
	// The real response to id 1 is now stale and must not leak into the
	// next call.
	res, err := c.Call("tools/call", nil)
	if err != nil || string(res) != `{"n":2}` {
		t.Fatalf("second call: %s, %v", res, err)
	}
}

// peer is the far end of a Client under test, driven by hand.
type peer struct {
	conn   *Conn
	client *Client
}

func newPeer(t *testing.T) *peer {
	t.Helper()
	peerIn, clientToPeer := io.Pipe()
	clientIn, peerToClient := io.Pipe()
	t.Cleanup(func() { clientToPeer.Close(); peerToClient.Close() })
	c := NewClient(NewConn(clientIn, clientToPeer))
	c.Logf = t.Logf
	return &peer{conn: NewConn(peerIn, peerToClient), client: c}
}

func (p *peer) read(t *testing.T) *Message {
	t.Helper()
	m, err := p.conn.Read()
	if err != nil {
		t.Fatalf("peer read: %v", err)
	}
	return m
}

type result struct {
	res json.RawMessage
	err error
}

func (p *peer) call(ctx context.Context, method string) <-chan result {
	out := make(chan result, 1)
	go func() {
		res, err := p.client.CallContext(ctx, method, nil)
		out <- result{res, err}
	}()
	return out
}

// TestClientMultiplexesCalls pins #67 at the transport: calls overlap,
// and each gets its own response whatever order the peer answers in.
func TestClientMultiplexesCalls(t *testing.T) {
	p := newPeer(t)
	first := p.call(context.Background(), "slow")
	req1 := p.read(t)
	second := p.call(context.Background(), "fast")
	req2 := p.read(t)
	p.conn.Write(&Message{ID: req2.ID, Result: json.RawMessage(`"fast"`)})
	if r := <-second; r.err != nil || string(r.res) != `"fast"` {
		t.Fatalf("second call: %s, %v", r.res, r.err)
	}
	p.conn.Write(&Message{ID: req1.ID, Result: json.RawMessage(`"slow"`)})
	if r := <-first; r.err != nil || string(r.res) != `"slow"` {
		t.Fatalf("first call: %s, %v", r.res, r.err)
	}
}

// TestClientCancelSendsCancelled pins MCP cancellation: the peer is told
// which request to abandon, and its late answer goes nowhere.
func TestClientCancelSendsCancelled(t *testing.T) {
	p := newPeer(t)
	ctx, cancel := context.WithCancel(context.Background())
	done := p.call(ctx, "tools/call")
	req := p.read(t)
	cancel()
	// io.Pipe is unbuffered: read the cancellation before waiting for
	// the call, which returns only once it is written.
	note := p.read(t)
	if r := <-done; !errors.Is(r.err, context.Canceled) {
		t.Fatalf("cancelled call returned %s, %v", r.res, r.err)
	}
	var params struct {
		RequestID json.RawMessage `json:"requestId"`
	}
	if note.Method != "notifications/cancelled" || json.Unmarshal(note.Params, &params) != nil ||
		string(params.RequestID) != string(req.ID) {
		t.Fatalf("got %s %s, want notifications/cancelled for id %s", note.Method, note.Params, req.ID)
	}
	// The late response is discarded; the next call is unaffected.
	p.conn.Write(&Message{ID: req.ID, Result: json.RawMessage(`"late"`)})
	next := p.call(context.Background(), "ping")
	p.conn.Write(&Message{ID: p.read(t).ID, Result: json.RawMessage(`{}`)})
	if r := <-next; r.err != nil || string(r.res) != `{}` {
		t.Fatalf("next call: %s, %v", r.res, r.err)
	}
}

type recordingHandler struct {
	notes    chan *Message
	requests chan *Message
	answer   func(ctx context.Context, m *Message) (json.RawMessage, error)
}

func (h *recordingHandler) HandleNotification(m *Message) { h.notes <- m }
func (h *recordingHandler) HandleRequest(ctx context.Context, m *Message) (json.RawMessage, error) {
	h.requests <- m
	return h.answer(ctx, m)
}

// TestClientRoutesPeerMessages pins that notifications reach the
// handler in order while a call is pending, and that a request from the
// peer is answered under its own id without disturbing the call.
func TestClientRoutesPeerMessages(t *testing.T) {
	p := newPeer(t)
	h := &recordingHandler{
		notes: make(chan *Message, 8), requests: make(chan *Message, 8),
		answer: func(context.Context, *Message) (json.RawMessage, error) {
			return json.RawMessage(`{"model":"m"}`), nil
		},
	}
	p.client.Handle(h)
	done := p.call(context.Background(), "tools/call")
	req := p.read(t)
	for i := 1; i <= 3; i++ {
		p.conn.Write(&Message{Method: "notifications/progress", Params: json.RawMessage(`{"progress":` + string(rune('0'+i)) + `}`)})
	}
	p.conn.Write(&Message{ID: json.RawMessage(`"s1"`), Method: "sampling/createMessage", Params: json.RawMessage(`{}`)})
	for i := 1; i <= 3; i++ {
		if n := <-h.notes; string(n.Params) != `{"progress":`+string(rune('0'+i))+`}` {
			t.Fatalf("notification %d out of order: %s", i, n.Params)
		}
	}
	if got := <-h.requests; got.Method != "sampling/createMessage" {
		t.Fatalf("handler got %s", got.Method)
	}
	if ans := p.read(t); string(ans.ID) != `"s1"` || string(ans.Result) != `{"model":"m"}` {
		t.Fatalf("answer to peer request: id %s result %s", ans.ID, ans.Result)
	}
	p.conn.Write(&Message{ID: req.ID, Result: json.RawMessage(`"ok"`)})
	if r := <-done; r.err != nil || string(r.res) != `"ok"` {
		t.Fatalf("call: %s, %v", r.res, r.err)
	}
}

// TestClientPeerCancelsItsRequest pins the other direction: when the
// peer cancels a request it sent, the handler's context ends and no
// response is written.
func TestClientPeerCancelsItsRequest(t *testing.T) {
	p := newPeer(t)
	h := &recordingHandler{
		notes: make(chan *Message, 8), requests: make(chan *Message, 8),
		answer: func(ctx context.Context, _ *Message) (json.RawMessage, error) {
			<-ctx.Done()
			return nil, ctx.Err()
		},
	}
	p.client.Handle(h)
	p.call(context.Background(), "tools/call") // starts the reader
	call := p.read(t)
	p.conn.Write(&Message{ID: json.RawMessage(`7`), Method: "roots/list"})
	<-h.requests
	p.conn.Write(&Message{Method: "notifications/cancelled", Params: json.RawMessage(`{"requestId":7}`)})
	// The only thing the peer should see next is nothing for id 7; prove
	// it by answering the call and checking the handler saw no note.
	p.conn.Write(&Message{ID: call.ID, Result: json.RawMessage(`{}`)})
	select {
	case n := <-h.notes:
		t.Fatalf("cancellation of the peer's own request leaked to the handler: %s", n.Method)
	case <-time.After(100 * time.Millisecond):
	}
	next := p.call(context.Background(), "ping")
	if m := p.read(t); m.Method != "ping" {
		t.Fatalf("peer got %s %s after cancelling; want no answer to id 7", m.Method, m.ID)
	} else {
		p.conn.Write(&Message{ID: m.ID, Result: json.RawMessage(`{}`)})
	}
	<-next
}

// TestClientCloseFailsPendingCalls pins that a peer that goes away ends
// every waiting call instead of hanging it.
func TestClientCloseFailsPendingCalls(t *testing.T) {
	peerIn, clientToPeer := io.Pipe()
	clientIn, peerToClient := io.Pipe()
	c := NewClient(NewConn(clientIn, clientToPeer))
	go io.Copy(io.Discard, peerIn)
	var results []<-chan result
	for range 3 {
		out := make(chan result, 1)
		go func() {
			res, err := c.Call("tools/call", nil)
			out <- result{res, err}
		}()
		results = append(results, out)
	}
	time.Sleep(50 * time.Millisecond)
	peerToClient.Close()
	for i, r := range results {
		if got := <-r; got.err == nil {
			t.Fatalf("call %d succeeded after the peer closed", i)
		}
	}
	if _, err := c.Call("ping", nil); err == nil {
		t.Fatal("call on a closed client succeeded")
	}
	clientToPeer.Close()
}

// TestAbsentParamsAreOmittedNotNull pins #74: forwarding a message that
// had no params (a nil json.RawMessage) must not put "params":null on
// the wire, which the official SDK rejects.
func TestAbsentParamsAreOmittedNotNull(t *testing.T) {
	var buf bytes.Buffer
	c := NewClient(NewConn(strings.NewReader(""), &buf))
	var absent json.RawMessage
	for _, params := range []any{nil, absent, json.RawMessage("null"), json.RawMessage(" null ")} {
		buf.Reset()
		if err := c.Notify("notifications/initialized", params); err != nil {
			t.Fatal(err)
		}
		if strings.Contains(buf.String(), "params") {
			t.Fatalf("params %#v written as %s", params, buf.String())
		}
	}
	buf.Reset()
	if err := c.Notify("notifications/progress", map[string]any{"progress": 1}); err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(buf.String(), `"params":{"progress":1}`) {
		t.Fatalf("real params lost: %s", buf.String())
	}
}

// TestClientSeesACancelRightBehindItsRequest pins #100: a cancellation
// that arrives with its request is not lost to the handler goroutine
// starting late.
func TestClientSeesACancelRightBehindItsRequest(t *testing.T) {
	for i := 0; i < 20; i++ {
		peerIn, clientToPeer := io.Pipe()
		clientIn, peerToClient := io.Pipe()
		c := NewClient(NewConn(clientIn, clientToPeer))
		cancelled := make(chan struct{})
		c.Handle(&recordingHandler{notes: make(chan *Message, 8), requests: make(chan *Message, 8),
			answer: func(ctx context.Context, _ *Message) (json.RawMessage, error) {
				<-ctx.Done()
				close(cancelled)
				return nil, ctx.Err()
			}})
		c.Start()
		go io.Copy(io.Discard, peerIn)
		_, _ = io.WriteString(peerToClient, `{"jsonrpc":"2.0","id":7,"method":"roots/list"}`+"\n"+
			`{"jsonrpc":"2.0","method":"notifications/cancelled","params":{"requestId":7}}`+"\n")
		select {
		case <-cancelled:
		case <-time.After(2 * time.Second):
			t.Fatalf("run %d: the cancellation was lost", i)
		}
		peerToClient.Close()
		clientToPeer.Close()
	}
}

func TestSSEOversizedLineIsSkipped(t *testing.T) {
	huge := "data: " + strings.Repeat("x", MaxFrame+10) + "\n\n"
	r := newSSEReader(strings.NewReader(huge + "data: {\"jsonrpc\":\"2.0\",\"method\":\"ok\"}\n\n"))
	var fe *FrameError
	if _, err := r.Next(); !errors.As(err, &fe) || !errors.Is(err, ErrFrameTooLarge) {
		t.Fatalf("oversized line: %v", err)
	}
	if m, err := r.Next(); err != nil || m.Method != "ok" {
		t.Fatalf("after an oversized line: %+v, %v", m, err)
	}
}
