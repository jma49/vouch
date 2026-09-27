package mcp

import (
	"bytes"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"net"
	"net/http"
	"net/url"
	"strings"
	"sync"
)

// HTTPServer is the server side of MCP's Streamable HTTP transport, as
// a Transport for the proxy: the agent POSTs messages, and Read returns
// them; Write sends each outgoing message on the HTTP stream it belongs
// to. It serves exactly one session, because one proxy process is one
// receipted session (its log, its turns, its seal); DELETE ends it, and
// Read then returns io.EOF, so the proxy seals the log as it does when
// stdin closes.
//
// Routing of what the proxy writes:
//   - a response goes to the POST that carried its request, as an
//     event, and ends that POST's stream;
//   - a progress notification goes to the stream of the request whose
//     progressToken it names;
//   - anything else (log messages, list_changed, requests to the agent)
//     goes to the GET stream if the agent holds one open, else to the
//     most recent open POST stream, since MCP lets a server send
//     messages ahead of a response; with no stream open, a notification
//     is dropped and a request fails.
type HTTPServer struct {
	logf        func(format string, args ...any)
	allowOrigin func(origin string) bool

	in        chan *Message
	done      chan struct{}
	closeOnce sync.Once

	mu        sync.Mutex
	sessionID string
	version   string                  // negotiated, from the initialize response
	posts     map[string]*eventStream // by request id
	order     []string                // open POST streams, oldest first
	tokens    map[string]string       // progressToken -> request id
	get       *eventStream
}

// eventStream is one open text/event-stream response.
type eventStream struct {
	out  chan []byte
	gone chan struct{} // closed when the HTTP handler returns
}

// NewHTTPServer returns a transport that accepts browser origins only
// from loopback hosts (MCP requires Origin validation against DNS
// rebinding); requests without an Origin, from non-browser agents, are
// accepted. logf may be nil.
func NewHTTPServer(logf func(format string, args ...any)) *HTTPServer {
	if logf == nil {
		logf = log.Printf
	}
	return &HTTPServer{
		logf: logf, allowOrigin: loopbackOrigin,
		in: make(chan *Message, 64), done: make(chan struct{}),
		posts: make(map[string]*eventStream), tokens: make(map[string]string),
	}
}

func loopbackOrigin(origin string) bool {
	u, err := url.Parse(origin)
	if err != nil {
		return false
	}
	host := u.Hostname()
	if host == "localhost" {
		return true
	}
	ip := net.ParseIP(host)
	return ip != nil && ip.IsLoopback()
}

// Read returns the next message the agent sent, or io.EOF after the
// session ended.
func (s *HTTPServer) Read() (*Message, error) {
	select {
	case m := <-s.in:
		return m, nil
	case <-s.done:
		return nil, io.EOF
	}
}

// Close ends the session.
func (s *HTTPServer) Close() error {
	s.closeOnce.Do(func() { close(s.done) })
	return nil
}

// Write routes one outgoing message; see HTTPServer.
func (s *HTTPServer) Write(m *Message) error {
	if m.JSONRPC == "" {
		m.JSONRPC = "2.0"
	}
	raw, err := json.Marshal(m)
	if err != nil {
		return fmt.Errorf("mcp: marshal frame: %w", err)
	}
	s.mu.Lock()
	if m.Method == "" && m.Error == nil && s.version == "" {
		var r struct {
			ProtocolVersion string `json:"protocolVersion"`
		}
		if json.Unmarshal(m.Result, &r) == nil && r.ProtocolVersion != "" {
			s.version = r.ProtocolVersion
		}
	}
	stream := s.route(m)
	s.mu.Unlock()
	if stream == nil {
		if m.Method != "" && len(m.ID) > 0 {
			return fmt.Errorf("mcp: no open stream to send %s to the agent", m.Method)
		}
		s.logf("mcp: http: no open stream for %s; dropped", describe(m))
		return nil
	}
	select {
	case stream.out <- raw:
		return nil
	case <-stream.gone:
		s.logf("mcp: http: agent left before %s was sent", describe(m))
		return nil
	}
}

// route picks the stream for m. Callers hold s.mu.
func (s *HTTPServer) route(m *Message) *eventStream {
	if m.Method == "" {
		return s.posts[string(bytes.TrimSpace(m.ID))]
	}
	if m.Method == "notifications/progress" {
		var p struct {
			ProgressToken json.RawMessage `json:"progressToken"`
		}
		if json.Unmarshal(m.Params, &p) == nil {
			if st, ok := s.posts[s.tokens[string(bytes.TrimSpace(p.ProgressToken))]]; ok {
				return st
			}
		}
	}
	if s.get != nil {
		return s.get
	}
	if n := len(s.order); n > 0 {
		return s.posts[s.order[n-1]]
	}
	return nil
}

func describe(m *Message) string {
	if m.Method != "" {
		return m.Method
	}
	return "response " + string(m.ID)
}

// ServeHTTP serves the MCP endpoint.
func (s *HTTPServer) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	if origin := r.Header.Get("Origin"); origin != "" && !s.allowOrigin(origin) {
		http.Error(w, "origin not allowed", http.StatusForbidden)
		return
	}
	select {
	case <-s.done:
		http.Error(w, "session ended", http.StatusNotFound)
		return
	default:
	}
	switch r.Method {
	case http.MethodPost:
		s.post(w, r)
	case http.MethodGet:
		s.listen(w, r)
	case http.MethodDelete:
		if s.checkSession(w, r, false) {
			s.Close()
			w.WriteHeader(http.StatusOK)
		}
	default:
		w.Header().Set("Allow", "GET, POST, DELETE")
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
	}
}

// checkSession enforces the session id and protocol version headers on
// every request after initialize. It writes the error response itself.
func (s *HTTPServer) checkSession(w http.ResponseWriter, r *http.Request, initializing bool) bool {
	s.mu.Lock()
	session, version := s.sessionID, s.version
	s.mu.Unlock()
	got := r.Header.Get("Mcp-Session-Id")
	switch {
	case initializing && session != "":
		writeRPCError(w, http.StatusBadRequest, "this proxy serves one session, already initialized")
		return false
	case initializing:
		return true
	case session == "":
		writeRPCError(w, http.StatusBadRequest, "no session: initialize first")
		return false
	case got == "":
		writeRPCError(w, http.StatusBadRequest, "missing Mcp-Session-Id header")
		return false
	case got != session:
		writeRPCError(w, http.StatusNotFound, "unknown session")
		return false
	}
	if v := r.Header.Get("MCP-Protocol-Version"); v != "" && version != "" && v != version {
		writeRPCError(w, http.StatusBadRequest, fmt.Sprintf("MCP-Protocol-Version %q does not match the session's %q", v, version))
		return false
	}
	return true
}

func (s *HTTPServer) post(w http.ResponseWriter, r *http.Request) {
	raw, err := io.ReadAll(io.LimitReader(r.Body, MaxFrame+1))
	if err != nil {
		writeRPCError(w, http.StatusBadRequest, "read body: "+err.Error())
		return
	}
	if len(raw) > MaxFrame {
		writeRPCError(w, http.StatusRequestEntityTooLarge, ErrFrameTooLarge.Error())
		return
	}
	m, err := decodeFrame(bytes.TrimSpace(raw))
	if err != nil {
		var fe *FrameError
		code := CodeParseError
		if errors.As(err, &fe) {
			code = fe.Code
		}
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusBadRequest)
		_ = json.NewEncoder(w).Encode(&Message{JSONRPC: "2.0", ID: nullID, Error: &Error{Code: code, Message: err.Error()}})
		return
	}
	initializing := m.Method == "initialize"
	if !s.checkSession(w, r, initializing) {
		return
	}
	if m.Method == "" || len(m.ID) == 0 { // a notification or a response
		if !s.enqueue(m) {
			http.Error(w, "session ended", http.StatusNotFound)
			return
		}
		w.WriteHeader(http.StatusAccepted)
		return
	}
	if !strings.Contains(r.Header.Get("Accept"), "text/event-stream") {
		// The agent cannot take a stream, so only the response can be
		// sent: MCP clients must accept both, and vouch always streams.
		writeRPCError(w, http.StatusNotAcceptable, "Accept must include text/event-stream")
		return
	}

	key := string(bytes.TrimSpace(m.ID))
	stream := &eventStream{out: make(chan []byte, 64), gone: make(chan struct{})}
	defer close(stream.gone)
	s.mu.Lock()
	if _, dup := s.posts[key]; dup {
		s.mu.Unlock()
		writeRPCError(w, http.StatusBadRequest, fmt.Sprintf("request id %s is already in flight", m.ID))
		return
	}
	if initializing {
		s.sessionID = newSessionID()
		w.Header().Set("Mcp-Session-Id", s.sessionID)
	}
	s.posts[key] = stream
	s.order = append(s.order, key)
	token := progressToken(m.Params)
	if token != "" {
		s.tokens[token] = key
	}
	s.mu.Unlock()
	defer func() {
		s.mu.Lock()
		delete(s.posts, key)
		delete(s.tokens, token)
		for i, k := range s.order {
			if k == key {
				s.order = append(s.order[:i], s.order[i+1:]...)
				break
			}
		}
		s.mu.Unlock()
	}()

	if !s.enqueue(m) {
		http.Error(w, "session ended", http.StatusNotFound)
		return
	}
	w.Header().Set("Content-Type", "text/event-stream")
	w.Header().Set("Cache-Control", "no-cache")
	w.WriteHeader(http.StatusOK)
	flush(w)
	for {
		select {
		case raw := <-stream.out:
			if err := writeSSE(w, raw); err != nil {
				return
			}
			flush(w)
			if isResponseTo(raw, key) {
				return // the response ends the stream
			}
		case <-r.Context().Done():
			// MCP: a disconnect is not a cancellation. The request runs
			// on and is receipted; its response has nowhere to go.
			s.logf("mcp: http: agent disconnected while waiting for %s", m.Method)
			return
		case <-s.done:
			return
		}
	}
}

// listen serves the GET stream for messages not tied to a request.
func (s *HTTPServer) listen(w http.ResponseWriter, r *http.Request) {
	if !strings.Contains(r.Header.Get("Accept"), "text/event-stream") {
		http.Error(w, "Accept must include text/event-stream", http.StatusNotAcceptable)
		return
	}
	if !s.checkSession(w, r, false) {
		return
	}
	stream := &eventStream{out: make(chan []byte, 64), gone: make(chan struct{})}
	defer close(stream.gone)
	s.mu.Lock()
	s.get = stream // a new GET replaces an older one
	s.mu.Unlock()
	defer func() {
		s.mu.Lock()
		if s.get == stream {
			s.get = nil
		}
		s.mu.Unlock()
	}()
	w.Header().Set("Content-Type", "text/event-stream")
	w.Header().Set("Cache-Control", "no-cache")
	w.WriteHeader(http.StatusOK)
	flush(w)
	for {
		select {
		case raw := <-stream.out:
			if err := writeSSE(w, raw); err != nil {
				return
			}
			flush(w)
		case <-r.Context().Done():
			return
		case <-s.done:
			return
		}
	}
}

func (s *HTTPServer) enqueue(m *Message) bool {
	select {
	case s.in <- m:
		return true
	case <-s.done:
		return false
	}
}

func progressToken(params json.RawMessage) string {
	var p struct {
		Meta struct {
			ProgressToken json.RawMessage `json:"progressToken"`
		} `json:"_meta"`
	}
	if json.Unmarshal(params, &p) != nil {
		return ""
	}
	return string(bytes.TrimSpace(p.Meta.ProgressToken))
}

func isResponseTo(raw []byte, key string) bool {
	var m Message
	return json.Unmarshal(raw, &m) == nil && m.Method == "" && string(bytes.TrimSpace(m.ID)) == key
}

func flush(w http.ResponseWriter) {
	if f, ok := w.(http.Flusher); ok {
		f.Flush()
	}
}

func writeRPCError(w http.ResponseWriter, status int, msg string) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(&Message{JSONRPC: "2.0", ID: nullID, Error: &Error{Code: CodeInvalidRequest, Message: msg}})
}

func newSessionID() string {
	var b [16]byte
	if _, err := rand.Read(b[:]); err != nil {
		panic(err) // crypto/rand failure is not recoverable
	}
	return hex.EncodeToString(b[:])
}
