// Package proxy implements the federating MCP server (docs/design.md
// section 2): the agent connects here instead of to its upstreams; the
// proxy forwards every call, records a signed receipt of the
// request/response pair, and returns the result unchanged.
//
// Federation surface: initialize (with protocol version negotiation),
// notifications/initialized, ping, tools/list (merged across
// upstreams), tools/call (routed by tool name), and cancellation.
// Requests are served concurrently after initialize (#67). From
// upstreams, progress, log, and tools/list_changed notifications reach
// the agent, and server-to-client requests (sampling, roots,
// elicitation) are forwarded to it and answered back (#68). Resources
// and prompts are out of scope and answered with method-not-found.
package proxy

import (
	"bytes"
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/jma49/vouch/proxy/internal/clock"
	"github.com/jma49/vouch/proxy/internal/extract"
	"github.com/jma49/vouch/proxy/internal/mcp"
	"github.com/jma49/vouch/proxy/internal/receipt"
	"github.com/jma49/vouch/proxy/internal/store"
)

// Caller is the upstream call surface. *mcp.Client is the live
// implementation; the fixture recorder and replayer wrap or replace it
// (docs/design.md section 8.1).
type Caller interface {
	CallContext(ctx context.Context, method string, params any) (json.RawMessage, error)
	Notify(method string, params any) error
}

// Upstream is one federated MCP server.
type Upstream struct {
	Name   string
	Client Caller
	Close  func() error
}

// Server federates upstreams behind a single MCP endpoint and records
// one receipt per tools/call.
type Server struct {
	Down      mcp.Transport
	Upstreams []*Upstream
	Schemas   map[string]*extract.Schema
	Log       *store.Log
	SessionID string
	Clock     clock.Clock
	Logf      func(format string, args ...any)
	// Cite adds a block to each receipted result naming the receipt and
	// its facts, so the model can cite them (design section 5, P-044).
	Cite bool

	mu       sync.RWMutex
	catalog  *catalog
	inflight map[string]context.CancelFunc // downstream request id -> cancel
	writeErr error                         // first failed write to the agent
	wg       sync.WaitGroup                // requests being served

	// Requests the proxy sends the agent on an upstream's behalf, by the
	// proxy's own id; downClosed is set when the agent has gone away.
	downPending map[string]chan *mcp.Message
	downNext    int64
	downClosed  bool

	// Notifications from upstreams wait here for one writer, so an
	// upstream's reader never blocks on a slow agent (#100) and their
	// order is kept.
	notes     chan *mcp.Message
	notesDone chan struct{}
}

// noteQueue bounds notifications waiting for a slow agent; past it,
// they are dropped (they are progress and logs, and the agent can ask
// for tools/list again).
const noteQueue = 256

// SupportedVersions are the MCP protocol versions the proxy can speak,
// newest first. The proxy forwards messages it does not interpret, so
// it can relay any version whose framing and federation methods match.
var SupportedVersions = []string{"2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05"}

func (s *Server) logf(format string, args ...any) {
	if s.Logf != nil {
		s.Logf(format, args...)
	} else {
		log.Printf(format, args...)
	}
}

// Run serves the downstream connection until EOF. Only EOF or an I/O
// error ends the session; a malformed frame is answered and skipped.
// initialize is served before the next message is read, so routes exist
// before any call; every other request runs on its own goroutine, and
// Run returns only after they finish, so a session is never sealed
// while a receipt is still being written.
func (s *Server) Run() error {
	s.inflight = make(map[string]context.CancelFunc)
	s.downPending = make(map[string]chan *mcp.Message)
	s.notes, s.notesDone = make(chan *mcp.Message, noteQueue), make(chan struct{})
	go s.pumpNotes(s.notes, s.notesDone)
	for _, u := range s.Upstreams {
		if l, ok := u.Client.(interface{ Handle(mcp.Handler) }); ok {
			l.Handle(&upstreamHandler{s: s, u: u})
		}
	}
	err := s.serve()
	// The agent is gone: requests forwarded to it will never be
	// answered. Fail them first, or a tools/call waiting on one would
	// hold Wait forever.
	s.closeDown()
	s.wg.Wait()
	s.mu.Lock()
	close(s.notes)
	s.notes = nil
	s.mu.Unlock()
	<-s.notesDone
	if err == nil {
		s.mu.Lock()
		err = s.writeErr
		s.mu.Unlock()
	}
	return err
}

func (s *Server) serve() error {
	for {
		m, err := s.Down.Read()
		var fe *mcp.FrameError
		if errors.As(err, &fe) {
			// JSON-RPC 2.0 section 5.1: when the id cannot be read, the
			// error response carries id null.
			s.logf("proxy: %v", err)
			if err := s.Down.Write(&mcp.Message{
				ID:    json.RawMessage("null"),
				Error: &mcp.Error{Code: fe.Code, Message: fe.Err.Error()},
			}); err != nil {
				return err
			}
			continue
		}
		if err != nil {
			if errors.Is(err, io.EOF) {
				return nil
			}
			return err
		}
		if err := s.dispatch(m); err != nil {
			return err
		}
	}
}

func (s *Server) dispatch(m *mcp.Message) error {
	// JSON-RPC 2.0 section 4.1: a notification is never answered. A
	// request method sent as a notification is dropped, not executed: a
	// tools/call nobody can receive the result of would still produce a
	// receipt attesting to data no agent saw. A message without a method
	// is a response, and the proxy sends no requests downstream.
	switch {
	case m.Method == "":
		s.deliver(m)
		return nil
	case m.Method == "notifications/cancelled" && m.IsNotification():
		s.cancel(m)
		return nil
	case m.Method == "notifications/roots/list_changed" && m.IsNotification():
		for _, u := range s.Upstreams {
			if err := u.Client.Notify(m.Method, m.Params); err != nil {
				s.logf("proxy: forward %s to %s: %v", m.Method, u.Name, err)
			}
		}
		return nil
	case m.IsNotification() && m.Method != "notifications/initialized":
		s.logf("proxy: dropping notification %s", m.Method)
		return nil
	}

	switch m.Method {
	case "initialize":
		return s.handleInitialize(m)
	case "notifications/initialized":
		if !m.IsNotification() {
			return s.replyError(m, mcp.CodeInvalidRequest, "notifications/initialized must be a notification")
		}
		for _, u := range s.Upstreams {
			if err := u.Client.Notify(m.Method, m.Params); err != nil {
				s.logf("proxy: forward initialized to %s: %v", u.Name, err)
			}
		}
		return nil
	}

	ctx, cancel := context.WithCancel(context.Background())
	key := string(bytes.TrimSpace(m.ID))
	s.mu.Lock()
	if _, dup := s.inflight[key]; dup {
		s.mu.Unlock()
		cancel()
		return s.replyError(m, mcp.CodeInvalidRequest, fmt.Sprintf("request id %s is already in use", m.ID))
	}
	s.inflight[key] = cancel
	s.mu.Unlock()
	s.wg.Add(1)
	go func() {
		defer s.wg.Done()
		defer func() {
			s.mu.Lock()
			delete(s.inflight, key)
			s.mu.Unlock()
			cancel()
		}()
		if err := s.handle(ctx, m); err != nil {
			s.mu.Lock()
			if s.writeErr == nil {
				s.writeErr = err
			}
			s.mu.Unlock()
		}
	}()
	return nil
}

func (s *Server) handle(ctx context.Context, m *mcp.Message) error {
	switch m.Method {
	case "ping":
		return s.reply(m, json.RawMessage(`{}`))
	case "tools/list":
		return s.handleToolsList(ctx, m)
	case "tools/call":
		return s.handleToolsCall(ctx, m)
	default:
		return s.replyError(m, mcp.CodeMethodNotFound, fmt.Sprintf("method %q not federated by vouch proxy", m.Method))
	}
}

// cancel handles notifications/cancelled from the agent: the request's
// context ends, which cancels its upstream call in turn (mcp.Client
// sends the upstream its own notifications/cancelled). A cancelled
// request gets no response and, unless its result had already come
// back, no receipt: the agent never saw a result.
func (s *Server) cancel(m *mcp.Message) {
	var p struct {
		RequestID json.RawMessage `json:"requestId"`
	}
	if err := json.Unmarshal(m.Params, &p); err != nil {
		s.logf("proxy: notifications/cancelled: %v", err)
		return
	}
	s.mu.Lock()
	cancel, ok := s.inflight[string(bytes.TrimSpace(p.RequestID))]
	s.mu.Unlock()
	if ok {
		cancel()
	}
}

// upstreamHandler receives what one upstream sends besides responses.
type upstreamHandler struct {
	s *Server
	u *Upstream
}

// HandleNotification forwards progress and log messages to the agent
// unchanged: a progressToken is the agent's own (it rides in the
// forwarded params), so the agent can match it without translation.
// tools/list_changed rebuilds the routes before the agent hears of it,
// so a call to a new tool made in response is routable. It runs on its
// own goroutine: refreshing calls tools/list on this upstream, whose
// response the calling goroutine (the upstream's reader) would have to
// read itself.
func (h *upstreamHandler) HandleNotification(m *mcp.Message) {
	switch m.Method {
	case "notifications/progress", "notifications/message":
		h.s.forwardNote(m, h.u)
	case "notifications/tools/list_changed":
		go func() {
			if err := h.s.refreshRoutes(); err != nil {
				h.s.logf("proxy: %s from %s: keeping the old routes: %v", m.Method, h.u.Name, err)
			}
			h.s.forwardNote(m, h.u)
		}()
	default:
		h.s.logf("proxy: dropping %s from %s", m.Method, h.u.Name)
	}
}

// forwardNote queues a notification for the agent without blocking.
func (s *Server) forwardNote(m *mcp.Message, u *Upstream) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.notes == nil {
		return // the session is over
	}
	select {
	case s.notes <- &mcp.Message{Method: m.Method, Params: m.Params}:
	default:
		s.logf("proxy: agent is not reading; dropped %s from %s", m.Method, u.Name)
	}
}

func (s *Server) pumpNotes(notes <-chan *mcp.Message, done chan<- struct{}) {
	defer close(done)
	for m := range notes {
		if err := s.Down.Write(m); err != nil {
			s.logf("proxy: forward %s: %v", m.Method, err)
		}
	}
}

// forwardedRequests are the server-to-client requests an upstream may
// send through the proxy. Everything else is refused: the agent never
// agreed to answer it.
var forwardedRequests = map[string]bool{
	"sampling/createMessage": true,
	"roots/list":             true,
	"elicitation/create":     true,
}

// HandleRequest relays a server-to-client request to the agent under a
// proxy-assigned id and returns the agent's answer, which mcp.Client
// sends back under the upstream's id. ping is answered by the proxy
// itself. If the upstream cancels, the agent is told to cancel too.
// The upstream saw the agent's own capabilities in the forwarded
// initialize, so it asks only for what the agent offered.
func (h *upstreamHandler) HandleRequest(ctx context.Context, m *mcp.Message) (json.RawMessage, error) {
	if m.Method == "ping" {
		return json.RawMessage(`{}`), nil
	}
	if !forwardedRequests[m.Method] {
		return nil, &mcp.Error{Code: mcp.CodeMethodNotFound, Message: fmt.Sprintf("vouch proxy does not forward %s", m.Method)}
	}
	return h.s.askAgent(ctx, m.Method, m.Params)
}

// askAgent sends the agent a request and waits for its response.
func (s *Server) askAgent(ctx context.Context, method string, params json.RawMessage) (json.RawMessage, error) {
	reply := make(chan *mcp.Message, 1)
	s.mu.Lock()
	if s.downClosed {
		s.mu.Unlock()
		return nil, fmt.Errorf("agent disconnected")
	}
	s.downNext++
	id := json.RawMessage(fmt.Sprintf(`"vouch-%d"`, s.downNext))
	s.downPending[string(id)] = reply
	s.mu.Unlock()
	forget := func() {
		s.mu.Lock()
		delete(s.downPending, string(id))
		s.mu.Unlock()
	}

	if err := s.Down.Write(&mcp.Message{ID: id, Method: method, Params: params}); err != nil {
		forget()
		return nil, err
	}
	select {
	case m, ok := <-reply:
		if !ok {
			return nil, fmt.Errorf("agent disconnected before answering %s", method)
		}
		if m.Error != nil {
			return nil, m.Error
		}
		return m.Result, nil
	case <-ctx.Done():
		forget()
		note, _ := json.Marshal(map[string]any{"requestId": id, "reason": "cancelled by the upstream"})
		if err := s.Down.Write(&mcp.Message{Method: "notifications/cancelled", Params: note}); err != nil {
			s.logf("proxy: cancel %s: %v", method, err)
		}
		return nil, ctx.Err()
	}
}

// deliver routes a response from the agent to the request it answers.
func (s *Server) deliver(m *mcp.Message) {
	s.mu.Lock()
	reply, ok := s.downPending[string(bytes.TrimSpace(m.ID))]
	delete(s.downPending, string(bytes.TrimSpace(m.ID)))
	s.mu.Unlock()
	if !ok {
		s.logf("proxy: ignoring response with unknown id %s", m.ID)
		return
	}
	reply <- m
}

func (s *Server) closeDown() {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.downClosed = true
	for id, reply := range s.downPending {
		delete(s.downPending, id)
		close(reply)
	}
}

// cancelled reports whether err means the agent cancelled the request,
// which is answered with silence (MCP cancellation).
func cancelled(ctx context.Context, err error) bool {
	return ctx.Err() != nil && errors.Is(err, ctx.Err())
}

// handleInitialize forwards the agent's initialize to every upstream,
// so each sees the agent's own requested version and capabilities, and
// answers with the version they all chose. MCP lets a server answer
// with a different version than requested; the proxy relays messages
// without translating them, so it can serve only one version per
// session. If upstreams disagree, or chose one the proxy does not
// support, initialize fails and says why rather than guessing (#68).
func (s *Server) handleInitialize(m *mcp.Message) error {
	versions := make([]string, len(s.Upstreams))
	// Bounded: initialize is served before the next agent message is
	// read, so a hung upstream would otherwise hold the session (#100).
	ctx, cancel := context.WithTimeout(context.Background(), upstreamTimeout)
	defer cancel()
	for i, u := range s.Upstreams {
		raw, err := u.Client.CallContext(ctx, "initialize", json.RawMessage(m.Params))
		if err != nil {
			return s.replyError(m, mcp.CodeInternalError, fmt.Sprintf("upstream %s initialize: %v", u.Name, err))
		}
		var res struct {
			ProtocolVersion string `json:"protocolVersion"`
		}
		_ = json.Unmarshal(raw, &res)
		versions[i] = res.ProtocolVersion
	}
	version, err := s.negotiate(m.Params, versions)
	if err != nil {
		return s.replyError(m, mcp.CodeInternalError, err.Error())
	}
	result := map[string]any{
		"protocolVersion": version,
		"capabilities":    map[string]any{"tools": map[string]any{"listChanged": true}},
		"serverInfo":      map[string]any{"name": "vouch-proxy", "version": "0.0.1-dev"},
	}
	raw, err := json.Marshal(result)
	if err != nil {
		return err
	}
	return s.reply(m, raw)
}

// negotiate picks the session's protocol version from what each
// upstream answered. With no upstreams it is the agent's request.
func (s *Server) negotiate(params json.RawMessage, versions []string) (string, error) {
	var req struct {
		ProtocolVersion string `json:"protocolVersion"`
	}
	_ = json.Unmarshal(params, &req)
	chosen := req.ProtocolVersion
	for i, v := range versions {
		if i == 0 {
			chosen = v
		} else if v != chosen {
			var parts []string
			for j, u := range s.Upstreams {
				parts = append(parts, fmt.Sprintf("%s=%q", u.Name, versions[j]))
			}
			return "", fmt.Errorf("upstreams chose different protocol versions (%s); vouch proxy relays one version per session",
				strings.Join(parts, ", "))
		}
	}
	for _, v := range SupportedVersions {
		if v == chosen {
			return chosen, nil
		}
	}
	return "", fmt.Errorf("protocol version %q is not supported by vouch proxy (supported: %s)",
		chosen, strings.Join(SupportedVersions, ", "))
}

// catalog is the merged tool list the agent sees and the routing table
// built from it, rebuilt on demand and on tools/list_changed.
type catalog struct {
	tools  []json.RawMessage
	routes map[string]*Upstream
}

// maxToolPages bounds how many pages one upstream's tools/list may take.
const maxToolPages = 100

// upstreamTimeout bounds a call the proxy makes on its own behalf
// (initialize, tools/list, notifications): an upstream that never
// answers must not hold the session open (#100). A variable so tests
// can shorten it.
var upstreamTimeout = 30 * time.Second

// loadCatalog lists every upstream's tools, following nextCursor to the
// last page (#100), and builds the routes. Name collisions are an
// error: silently picking a winner would attribute receipts to the
// wrong upstream.
func (s *Server) loadCatalog(ctx context.Context) (*catalog, error) {
	c := &catalog{routes: make(map[string]*Upstream)}
	for _, u := range s.Upstreams {
		cursor := ""
		for page := 0; ; page++ {
			if page == maxToolPages {
				return nil, fmt.Errorf("upstream %s tools/list: more than %d pages", u.Name, maxToolPages)
			}
			var params any
			if cursor != "" {
				params = map[string]string{"cursor": cursor}
			}
			raw, err := u.Client.CallContext(ctx, "tools/list", params)
			if err != nil {
				return nil, fmt.Errorf("upstream %s tools/list: %w", u.Name, err)
			}
			var res struct {
				Tools      []json.RawMessage `json:"tools"`
				NextCursor string            `json:"nextCursor"`
			}
			if err := receipt.DecodeStrict(raw, &res); err != nil {
				return nil, fmt.Errorf("upstream %s tools/list: %w", u.Name, err)
			}
			for _, tool := range res.Tools {
				var t struct {
					Name string `json:"name"`
				}
				if err := receipt.DecodeStrict(tool, &t); err != nil || t.Name == "" {
					return nil, fmt.Errorf("upstream %s tools/list: a tool without a readable name", u.Name)
				}
				if prev, dup := c.routes[t.Name]; dup {
					return nil, fmt.Errorf("tool %q served by both %s and %s", t.Name, prev.Name, u.Name)
				}
				c.routes[t.Name] = u
				c.tools = append(c.tools, tool)
			}
			if res.NextCursor == "" {
				break
			}
			cursor = res.NextCursor
		}
	}
	return c, nil
}

// refreshRoutes reloads the catalog, as after tools/list_changed.
func (s *Server) refreshRoutes() error {
	ctx, cancel := context.WithTimeout(context.Background(), upstreamTimeout)
	defer cancel()
	c, err := s.loadCatalog(ctx)
	if err != nil {
		return err
	}
	s.mu.Lock()
	s.catalog = c
	s.mu.Unlock()
	return nil
}

// routes returns the current routing table, loading the catalog on first
// use. Routes are built lazily rather than during initialize: MCP
// expects no requests to an upstream before the agent's
// notifications/initialized has reached it (#100).
func (s *Server) route(ctx context.Context, tool string) (*Upstream, error) {
	s.mu.RLock()
	c := s.catalog
	s.mu.RUnlock()
	if c == nil {
		if err := s.refreshRoutes(); err != nil {
			return nil, err
		}
		s.mu.RLock()
		c = s.catalog
		s.mu.RUnlock()
	}
	return c.routes[tool], nil
}

// handleToolsList serves the merged tool list in one page. Upstream
// pages are the proxy's business: an agent's cursor would mean nothing
// across several upstreams, so none is accepted (#100).
func (s *Server) handleToolsList(ctx context.Context, m *mcp.Message) error {
	var params struct {
		Cursor string `json:"cursor"`
	}
	if len(m.Params) > 0 {
		if err := receipt.DecodeStrict(m.Params, &params); err != nil {
			return s.replyError(m, mcp.CodeInvalidParams, fmt.Sprintf("tools/list: params: %v", err))
		}
	}
	if params.Cursor != "" {
		return s.replyError(m, mcp.CodeInvalidParams, "tools/list: vouch proxy lists every tool in one page; no cursor")
	}
	c, err := s.loadCatalog(ctx)
	if cancelled(ctx, err) {
		return nil
	}
	if err != nil {
		return s.replyError(m, mcp.CodeInternalError, err.Error())
	}
	s.mu.Lock()
	s.catalog = c
	s.mu.Unlock()
	raw, err := json.Marshal(map[string]any{"tools": c.tools})
	if err != nil {
		return err
	}
	return s.reply(m, raw)
}

func (s *Server) handleToolsCall(ctx context.Context, m *mcp.Message) error {
	var params struct {
		Name      string          `json:"name"`
		Arguments json.RawMessage `json:"arguments"`
		Meta      json.RawMessage `json:"_meta"` // read only so "_META" is refused
	}
	// Params the proxy and the upstream could read differently are
	// refused before the upstream runs: with a duplicate "name", or both
	// "arguments" and "Arguments", Go would route and receipt one value
	// while the upstream executes another (#98). DecodeStrict is the
	// reader every party agrees with.
	if len(m.Params) > 0 {
		if err := receipt.DecodeStrict(m.Params, &params); err != nil {
			return s.replyError(m, mcp.CodeInvalidParams, fmt.Sprintf("tools/call: params: %v", err))
		}
	}
	if params.Name == "" {
		return s.replyError(m, mcp.CodeInvalidParams, "tools/call: missing tool name")
	}
	u, err := s.route(ctx, params.Name)
	if err != nil {
		return s.replyError(m, mcp.CodeInternalError, err.Error())
	}
	if u == nil {
		return s.replyError(m, mcp.CodeInvalidParams, fmt.Sprintf("tools/call: unknown tool %q", params.Name))
	}

	start := s.Clock.Now()
	result, err := u.Client.CallContext(ctx, "tools/call", json.RawMessage(m.Params))
	latency := s.Clock.Now().Sub(start).Milliseconds()
	if cancelled(ctx, err) {
		return nil // no response, and no receipt: the agent saw nothing
	}
	if err != nil {
		var rpcErr *mcp.Error
		if errors.As(err, &rpcErr) {
			return s.Down.Write(&mcp.Message{ID: m.ID, Error: rpcErr})
		}
		return s.replyError(m, mcp.CodeInternalError, fmt.Sprintf("upstream %s: %v", u.Name, err))
	}

	// The receipt is the product: if it cannot be written, the call
	// fails rather than passing unverifiable data through.
	r, err := s.record(params.Name, params.Arguments, result, latency)
	if err != nil {
		return s.replyError(m, mcp.CodeInternalError, fmt.Sprintf("receipt: %v", err))
	}
	if s.Cite {
		result = withCitations(result, r)
	}
	return s.reply(m, result)
}

// citeIDLen is how much of a receipt id a citation carries. The
// verifier accepts a unique prefix, and a model copies twelve hex digits
// more reliably than thirty-two; a prefix shared by two receipts in one
// log makes the citation ambiguous and UNSUPPORTED, never wrong.
const citeIDLen = 12

// withCitations appends a text block to a tools/call result telling the
// model how to cite each fact the receipt holds (design section 5,
// P-044). Everything in the block comes from the signed receipt, so the
// agent learns nothing the receipt does not attest. The receipt itself
// covers the upstream's result without the block: it records what the
// tool returned, and the block is the proxy's, derived from it. A result
// with no facts, or one whose content cannot be extended, is returned
// unchanged.
func withCitations(result json.RawMessage, r *receipt.Receipt) json.RawMessage {
	if len(r.Facts) == 0 {
		return result
	}
	var res map[string]json.RawMessage
	if json.Unmarshal(result, &res) != nil {
		return result
	}
	var content []json.RawMessage
	if raw, ok := res["content"]; ok && json.Unmarshal(raw, &content) != nil {
		return result
	}
	id := r.ReceiptID[:min(citeIDLen, len(r.ReceiptID))]
	var b strings.Builder
	fmt.Fprintf(&b, "[vouch] These values are receipted. When you state one, cite it right after the number as [[r:%s#<pointer>]]:", id)
	for _, f := range r.Facts {
		fmt.Fprintf(&b, "\n%s = %s  -> [[r:%s#%s]]", f.Metric, strconv.FormatFloat(f.Value, 'g', -1, 64), id, f.JSONPtr)
	}
	block, err := json.Marshal(map[string]string{"type": "text", "text": b.String()})
	if err != nil {
		return result
	}
	res["content"], err = json.Marshal(append(content, block))
	if err != nil {
		return result
	}
	out, err := json.Marshal(res)
	if err != nil {
		return result
	}
	return out
}

// record writes one signed receipt for a completed tools/call.
func (s *Server) record(tool string, args, result json.RawMessage, latencyMS int64) (*receipt.Receipt, error) {
	if len(args) == 0 {
		args = json.RawMessage(`{}`)
	}
	argsCanon, err := receipt.Canonicalize(args)
	if err != nil {
		return nil, fmt.Errorf("canonicalize args: %w", err)
	}
	responseCanon, err := receipt.Canonicalize(result)
	if err != nil {
		return nil, fmt.Errorf("canonicalize response: %w", err)
	}
	var res toolResult
	if err := receipt.DecodeStrict(result, &res); err != nil {
		return nil, fmt.Errorf("result: %w", err)
	}
	payload, source := res.payload(result)
	resultCanon, err := receipt.Canonicalize(payload)
	if err != nil {
		return nil, fmt.Errorf("canonicalize result: %w", err)
	}

	// A tool error is receipted, because the agent saw it, but it is not
	// evidence: numbers in an error payload describe the failure, not
	// data the tool returned, so it carries no facts.
	var facts []receipt.Fact
	var dataAsOf string
	if schema, ok := s.Schemas[tool]; ok && !res.IsError {
		facts, err = schema.Extract(resultCanon)
		if err != nil {
			return nil, err
		}
		dataAsOf = schema.ResultAsOf(resultCanon)
	}

	r := &receipt.Receipt{
		ReceiptID:         newID(),
		SessionID:         s.SessionID,
		ToolName:          tool,
		ArgsCanonical:     argsCanon,
		ResultCanonical:   resultCanon,
		ResultDigest:      receipt.Digest(resultCanon),
		PayloadSource:     source,
		ResponseCanonical: responseCanon,
		ResponseDigest:    receipt.Digest(responseCanon),
		Facts:             facts,
		DataAsOf:          dataAsOf,
		WallTime:          s.Clock.Now(),
		LogicalTime:       s.Clock.Tick(),
		UpstreamLatencyMS: latencyMS,
	}
	// The log signs (the envelope's signature covers the exact bytes it
	// writes, package sign) and assigns the turn, continuing a session
	// already in the log (#69).
	if err := s.Log.AppendNextTurn(r); err != nil {
		return nil, err
	}
	return r, nil
}

// toolResult is what the proxy reads of an MCP tools/call result, read
// strictly: an upstream sending both "isError" and "IsError", or
// "structuredContent" and "StructuredContent", would otherwise decide
// which payload is receipted differently for the proxy and the agent's
// SDK (#98). isError flags a tool-level error.
type toolResult struct {
	IsError           bool            `json:"isError"`
	StructuredContent json.RawMessage `json:"structuredContent"`
	Content           []struct {
		Type string `json:"type"`
		Text string `json:"text"`
	} `json:"content"`
}

// payload picks the JSON document facts are extracted from:
// structuredContent when the upstream provides it, else the first text
// content block when it parses as JSON, else the whole MCP result.
func (res toolResult) payload(result json.RawMessage) (json.RawMessage, string) {
	if sc := bytes.TrimSpace(res.StructuredContent); len(sc) > 0 && !bytes.Equal(sc, []byte("null")) {
		return res.StructuredContent, "structuredContent"
	}
	for i, c := range res.Content {
		// Only a JSON object or array is a document facts can come
		// from; a scalar such as "1" is valid JSON but would shadow the
		// real payload in a later block (#20).
		text := bytes.TrimSpace([]byte(c.Text))
		if c.Type == "text" && len(text) > 0 && (text[0] == '{' || text[0] == '[') && json.Valid(text) {
			return json.RawMessage(c.Text), fmt.Sprintf("content/%d/text", i)
		}
	}
	return result, "result"
}

func (s *Server) reply(m *mcp.Message, result json.RawMessage) error {
	if m.IsNotification() {
		return nil // dispatch filters these; never answer one regardless
	}
	return s.Down.Write(&mcp.Message{ID: m.ID, Result: result})
}

func (s *Server) replyError(m *mcp.Message, code int, msg string) error {
	if m.IsNotification() {
		s.logf("proxy: %s", msg)
		return nil
	}
	return s.Down.Write(&mcp.Message{ID: m.ID, Error: &mcp.Error{Code: code, Message: msg}})
}

func newID() string {
	var b [16]byte
	if _, err := rand.Read(b[:]); err != nil {
		panic(err) // crypto/rand failure is not recoverable
	}
	return hex.EncodeToString(b[:])
}
