package mcp

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
)

// echoServer serves an HTTPServer the way the proxy does: it answers
// each request with its method and params, sends a progress
// notification first when the request carries a progressToken, and
// sends one unprompted notification after initialize. It stops when
// the session ends.
func echoServer(t *testing.T, s *HTTPServer) {
	t.Helper()
	go func() {
		for {
			m, err := s.Read()
			if err != nil {
				return
			}
			if m.Method == "" || len(m.ID) == 0 {
				continue
			}
			if tok := progressToken(m.Params); tok != "" {
				_ = s.Write(&Message{Method: "notifications/progress", Params: json.RawMessage(`{"progressToken":` + tok + `,"progress":1}`)})
			}
			res := json.RawMessage(`{"method":"` + m.Method + `"}`)
			if m.Method == "initialize" {
				res = json.RawMessage(`{"protocolVersion":"2025-06-18","capabilities":{}}`)
			}
			_ = s.Write(&Message{ID: m.ID, Result: res})
		}
	}()
}

type noteSink struct{ notes chan *Message }

func (n noteSink) HandleNotification(m *Message) { n.notes <- m }
func (n noteSink) HandleRequest(context.Context, *Message) (json.RawMessage, error) {
	return json.RawMessage(`{"answered":true}`), nil
}

func startHTTP(t *testing.T) (*HTTPServer, *httptest.Server, *Client, *HTTPClient, chan *Message) {
	t.Helper()
	srv := NewHTTPServer(t.Logf)
	hs := httptest.NewServer(srv)
	t.Cleanup(hs.Close)
	hc := NewHTTPClient(hs.URL, nil, t.Logf)
	t.Cleanup(func() { hc.Close() })
	c := NewClient(hc)
	c.Logf = t.Logf
	notes := make(chan *Message, 16)
	c.Handle(noteSink{notes})
	return srv, hs, c, hc, notes
}

// TestHTTPRoundTrip pins the Streamable HTTP pair end to end: a session
// id from initialize rides on later requests, responses come back over
// event streams, a progress notification reaches the caller ahead of
// its response, and ending the session ends the server's Read.
func TestHTTPRoundTrip(t *testing.T) {
	srv, _, c, hc, notes := startHTTP(t)
	echoServer(t, srv)

	if _, err := c.Call("initialize", map[string]any{"protocolVersion": "2025-06-18"}); err != nil {
		t.Fatal(err)
	}
	hc.mu.Lock()
	session, version := hc.sessionID, hc.version
	hc.mu.Unlock()
	if session == "" || session != srv.sessionID || version != "2025-06-18" {
		t.Fatalf("client session %q version %q; server session %q", session, version, srv.sessionID)
	}
	res, err := c.Call("tools/call", map[string]any{"name": "x", "_meta": map[string]any{"progressToken": "p1"}})
	if err != nil || string(res) != `{"method":"tools/call"}` {
		t.Fatalf("tools/call: %s, %v", res, err)
	}
	select {
	case n := <-notes:
		if n.Method != "notifications/progress" || !strings.Contains(string(n.Params), `"p1"`) {
			t.Fatalf("got %s %s", n.Method, n.Params)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("no progress notification")
	}
	if err := c.Notify("notifications/initialized", nil); err != nil {
		t.Fatalf("notification: %v", err)
	}
	if err := hc.Close(); err != nil {
		t.Fatal(err)
	}
	select {
	case <-srv.done:
	case <-time.After(5 * time.Second):
		t.Fatal("DELETE did not end the session")
	}
	if _, err := srv.Read(); !errors.Is(err, io.EOF) {
		t.Fatalf("Read after the session ended: %v", err)
	}
}

// TestHTTPGetStreamCarriesUnpromptedMessages pins the GET stream: a
// message the server sends on its own reaches the client, and a request
// sent to the client there is answered by POST.
func TestHTTPGetStreamCarriesUnpromptedMessages(t *testing.T) {
	srv, _, c, _, notes := startHTTP(t)
	echoServer(t, srv)
	if _, err := c.Call("initialize", map[string]any{"protocolVersion": "2025-06-18"}); err != nil {
		t.Fatal(err)
	}
	deadline := time.Now().Add(5 * time.Second)
	for {
		srv.mu.Lock()
		open := srv.get != nil
		srv.mu.Unlock()
		if open {
			break
		}
		if time.Now().After(deadline) {
			t.Fatal("the client never opened a GET stream")
		}
		time.Sleep(10 * time.Millisecond)
	}
	if err := srv.Write(&Message{Method: "notifications/tools/list_changed"}); err != nil {
		t.Fatal(err)
	}
	select {
	case n := <-notes:
		if n.Method != "notifications/tools/list_changed" {
			t.Fatalf("got %s", n.Method)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("unprompted notification never arrived")
	}
}

func do(t *testing.T, hs *httptest.Server, method, body string, header map[string]string) *http.Response {
	t.Helper()
	req, err := http.NewRequest(method, hs.URL, strings.NewReader(body))
	if err != nil {
		t.Fatal(err)
	}
	req.Header.Set("Accept", "application/json, text/event-stream")
	for k, v := range header {
		req.Header.Set(k, v)
	}
	resp, err := hs.Client().Do(req)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { resp.Body.Close() })
	return resp
}

// TestHTTPServerRefuses pins the transport's guard rails: Origin
// validation, session and version headers, one session per proxy, and
// malformed bodies answered as JSON-RPC errors.
func TestHTTPServerRefuses(t *testing.T) {
	srv, hs, c, hc, _ := startHTTP(t)
	echoServer(t, srv)
	if _, err := c.Call("initialize", map[string]any{"protocolVersion": "2025-06-18"}); err != nil {
		t.Fatal(err)
	}
	hc.mu.Lock()
	session := hc.sessionID
	hc.mu.Unlock()
	ping := `{"jsonrpc":"2.0","id":9,"method":"ping"}`
	ok := map[string]string{"Mcp-Session-Id": session}
	cases := []struct {
		name   string
		method string
		body   string
		header map[string]string
		status int
	}{
		{"foreign origin", "POST", ping, map[string]string{"Mcp-Session-Id": session, "Origin": "https://evil.example"}, 403},
		{"missing session", "POST", ping, nil, 400},
		{"unknown session", "POST", ping, map[string]string{"Mcp-Session-Id": "nope"}, 404},
		{"second initialize", "POST", `{"jsonrpc":"2.0","id":8,"method":"initialize","params":{}}`, ok, 400},
		{"version mismatch", "POST", ping, map[string]string{"Mcp-Session-Id": session, "MCP-Protocol-Version": "2024-11-05"}, 400},
		{"not json", "POST", "{", ok, 400},
		{"batch", "POST", "[" + ping + "]", ok, 400},
		{"GET without event-stream", "GET", "", map[string]string{"Mcp-Session-Id": session, "Accept": "application/json"}, 406},
		{"PUT", "PUT", "", ok, 405},
		{"loopback origin is fine", "POST", ping, map[string]string{"Mcp-Session-Id": session, "Origin": "http://localhost:3000"}, 200},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if resp := do(t, hs, tc.method, tc.body, tc.header); resp.StatusCode != tc.status {
				body, _ := io.ReadAll(resp.Body)
				t.Fatalf("got %d %s, want %d", resp.StatusCode, body, tc.status)
			}
		})
	}
}

// TestHTTPClientHandlesPlainJSONAndFailures pins the client against
// servers that answer with application/json, fail, or end a stream
// early: each call gets an answer or an error, never a hang.
func TestHTTPClientHandlesPlainJSONAndFailures(t *testing.T) {
	hs := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var m Message
		_ = json.NewDecoder(r.Body).Decode(&m)
		switch m.Method {
		case "json":
			w.Header().Set("Content-Type", "application/json")
			_ = json.NewEncoder(w).Encode(&Message{JSONRPC: "2.0", ID: m.ID, Result: json.RawMessage(`{"plain":true}`)})
		case "boom":
			http.Error(w, "upstream exploded", http.StatusInternalServerError)
		case "short":
			w.Header().Set("Content-Type", "text/event-stream")
			_, _ = io.WriteString(w, ": keep-alive\n\nevent: message\ndata: {\"jsonrpc\":\"2.0\",\"method\":\"notifications/message\"}\n\n")
		default:
			w.WriteHeader(http.StatusAccepted)
		}
	}))
	defer hs.Close()
	hc := NewHTTPClient(hs.URL, http.Header{"Authorization": {"Bearer t"}}, t.Logf)
	defer hc.Close()
	c := NewClient(hc)
	c.Logf = t.Logf

	if res, err := c.Call("json", nil); err != nil || string(res) != `{"plain":true}` {
		t.Fatalf("json: %s, %v", res, err)
	}
	if _, err := c.Call("boom", nil); err == nil || !strings.Contains(err.Error(), "500") {
		t.Fatalf("boom: %v, want the HTTP status", err)
	}
	if _, err := c.Call("short", nil); err == nil || !strings.Contains(err.Error(), "without a response") {
		t.Fatalf("short: %v, want a stream-ended error", err)
	}
	if err := c.Notify("notifications/initialized", nil); err != nil {
		t.Fatalf("notification: %v", err)
	}
}

func TestSSEReader(t *testing.T) {
	stream := ": comment\r\n" +
		"event: message\r\nid: 1\r\ndata: {\"jsonrpc\":\"2.0\",\r\ndata: \"method\":\"a\"}\r\n\r\n" +
		"data: not json\n\n" +
		"data: {\"jsonrpc\":\"2.0\",\"method\":\"b\"}"
	r := newSSEReader(strings.NewReader(stream))
	if m, err := r.Next(); err != nil || m.Method != "a" {
		t.Fatalf("multi-line data: %+v, %v", m, err)
	}
	var fe *FrameError
	if _, err := r.Next(); !errors.As(err, &fe) {
		t.Fatalf("bad data: %v, want a FrameError", err)
	}
	if m, err := r.Next(); err != nil || m.Method != "b" {
		t.Fatalf("final event without a blank line: %+v, %v", m, err)
	}
	if _, err := r.Next(); !errors.Is(err, io.EOF) {
		t.Fatalf("end: %v", err)
	}
}

// TestHTTPSessionRaces pins #100: concurrent initialize POSTs get one
// session, and an initialize the proxy refuses leaves none behind.
func TestHTTPSessionRaces(t *testing.T) {
	srv := NewHTTPServer(t.Logf)
	hs := httptest.NewServer(srv)
	defer hs.Close()
	refuse := true
	go func() {
		for {
			m, err := srv.Read()
			if err != nil {
				return
			}
			if m.Method != "initialize" {
				continue
			}
			if refuse {
				refuse = false
				_ = srv.Write(&Message{ID: m.ID, Error: &Error{Code: CodeInternalError, Message: "no"}})
				continue
			}
			_ = srv.Write(&Message{ID: m.ID, Result: json.RawMessage(`{"protocolVersion":"2025-06-18"}`)})
		}
	}()
	init := `{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}`
	resp := do(t, hs, "POST", init, nil)
	body, _ := io.ReadAll(resp.Body)
	if !strings.Contains(string(body), `"error"`) {
		t.Fatalf("first initialize should be refused: %s", body)
	}
	// The refusal released the session: a retry, raced by a second
	// initialize, gets exactly one.
	codes := make(chan int, 2)
	for range 2 {
		go func() {
			req, _ := http.NewRequest("POST", hs.URL, strings.NewReader(init))
			req.Header.Set("Content-Type", "application/json")
			req.Header.Set("Accept", "application/json, text/event-stream")
			r, err := hs.Client().Do(req)
			if err != nil {
				codes <- 0
				return
			}
			io.Copy(io.Discard, r.Body)
			r.Body.Close()
			codes <- r.StatusCode
		}()
	}
	got := []int{<-codes, <-codes}
	if !(got[0] == 200) == !(got[1] == 200) {
		t.Fatalf("status codes %v, want exactly one 200", got)
	}
}

func TestHTTPServerLimits(t *testing.T) {
	srv := NewHTTPServer(t.Logf)
	srv.LoopbackHostsOnly()
	hs := httptest.NewServer(srv)
	defer hs.Close()
	req, _ := http.NewRequest("POST", hs.URL, strings.NewReader("{}"))
	req.Host = "evil.example"
	resp, err := hs.Client().Do(req)
	if err != nil || resp.StatusCode != http.StatusForbidden {
		t.Fatalf("foreign Host: %v %v", resp, err)
	}
	resp.Body.Close()
}

func TestHTTPClientDoesNotFollowRedirects(t *testing.T) {
	target := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		t.Errorf("redirect followed, carrying %q", r.Header.Get("X-Api-Key"))
	}))
	defer target.Close()
	hs := httptest.NewServer(http.RedirectHandler(target.URL, http.StatusTemporaryRedirect))
	defer hs.Close()
	hc := NewHTTPClient(hs.URL, http.Header{"X-Api-Key": {"secret"}}, t.Logf)
	defer hc.Close()
	if _, err := NewClient(hc).Call("ping", nil); err == nil || !strings.Contains(err.Error(), "307") {
		t.Fatalf("redirect: %v, want the 307 reported", err)
	}
}
