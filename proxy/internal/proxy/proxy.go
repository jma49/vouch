// Package proxy implements the federating MCP server (docs/design.md
// section 2): the agent connects here instead of to its upstreams; the
// proxy forwards every call, records a signed receipt of the
// request/response pair, and returns the result unchanged.
//
// Federation surface: initialize, notifications/initialized, ping,
// tools/list (merged across upstreams), tools/call (routed by tool
// name), and cancellation. Requests are served concurrently after
// initialize (#67). Upstream progress and log notifications are
// forwarded to the agent. Resources and prompts are out of scope and
// answered with method-not-found.
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
	"sync"

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
	Down      *mcp.Conn
	Upstreams []*Upstream
	Schemas   map[string]*extract.Schema
	Log       *store.Log
	SessionID string
	Clock     clock.Clock
	Logf      func(format string, args ...any)

	mu       sync.RWMutex
	routes   map[string]*Upstream
	inflight map[string]context.CancelFunc // downstream request id -> cancel
	writeErr error                         // first failed write to the agent
	wg       sync.WaitGroup                // requests being served
}

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
	for _, u := range s.Upstreams {
		if l, ok := u.Client.(interface{ Handle(mcp.Handler) }); ok {
			l.Handle(&upstreamHandler{s: s, u: u})
		}
	}
	err := s.serve()
	s.wg.Wait()
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
		s.logf("proxy: ignoring message without a method (id %s)", m.ID)
		return nil
	case m.Method == "notifications/cancelled" && m.IsNotification():
		s.cancel(m)
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
func (h *upstreamHandler) HandleNotification(m *mcp.Message) {
	switch m.Method {
	case "notifications/progress", "notifications/message":
		if err := h.s.Down.Write(&mcp.Message{Method: m.Method, Params: m.Params}); err != nil {
			h.s.logf("proxy: forward %s from %s: %v", m.Method, h.u.Name, err)
		}
	default:
		h.s.logf("proxy: dropping %s from %s", m.Method, h.u.Name)
	}
}

// HandleRequest refuses server-to-client requests for now; forwarding
// them to the agent is #68. Answering beats silence: the upstream stops
// waiting.
func (h *upstreamHandler) HandleRequest(_ context.Context, m *mcp.Message) (json.RawMessage, error) {
	h.s.logf("proxy: refusing %s from %s", m.Method, h.u.Name)
	return nil, &mcp.Error{Code: mcp.CodeMethodNotFound, Message: fmt.Sprintf("vouch proxy does not forward %s yet", m.Method)}
}

// cancelled reports whether err means the agent cancelled the request,
// which is answered with silence (MCP cancellation).
func cancelled(ctx context.Context, err error) bool {
	return ctx.Err() != nil && errors.Is(err, ctx.Err())
}

func (s *Server) handleInitialize(m *mcp.Message) error {
	var params struct {
		ProtocolVersion string `json:"protocolVersion"`
	}
	_ = json.Unmarshal(m.Params, &params)
	for _, u := range s.Upstreams {
		if _, err := u.Client.CallContext(context.Background(), "initialize", json.RawMessage(m.Params)); err != nil {
			return s.replyError(m, mcp.CodeInternalError, fmt.Sprintf("upstream %s initialize: %v", u.Name, err))
		}
	}
	if err := s.refreshRoutes(); err != nil {
		return s.replyError(m, mcp.CodeInternalError, err.Error())
	}
	result := map[string]any{
		"protocolVersion": params.ProtocolVersion,
		"capabilities":    map[string]any{"tools": map[string]any{}},
		"serverInfo":      map[string]any{"name": "vouch-proxy", "version": "0.0.1-dev"},
	}
	raw, err := json.Marshal(result)
	if err != nil {
		return err
	}
	return s.reply(m, raw)
}

// refreshRoutes rebuilds the tool -> upstream routing table from every
// upstream's tools/list. Name collisions are an error: silently picking
// a winner would attribute receipts to the wrong upstream.
func (s *Server) refreshRoutes() error {
	routes := make(map[string]*Upstream)
	for _, u := range s.Upstreams {
		raw, err := u.Client.CallContext(context.Background(), "tools/list", nil)
		if err != nil {
			return fmt.Errorf("upstream %s tools/list: %w", u.Name, err)
		}
		var res struct {
			Tools []struct {
				Name string `json:"name"`
			} `json:"tools"`
		}
		if err := json.Unmarshal(raw, &res); err != nil {
			return fmt.Errorf("upstream %s tools/list: %w", u.Name, err)
		}
		for _, tool := range res.Tools {
			if prev, dup := routes[tool.Name]; dup {
				return fmt.Errorf("tool %q served by both %s and %s", tool.Name, prev.Name, u.Name)
			}
			routes[tool.Name] = u
		}
	}
	s.mu.Lock()
	s.routes = routes
	s.mu.Unlock()
	return nil
}

func (s *Server) handleToolsList(ctx context.Context, m *mcp.Message) error {
	var merged []json.RawMessage
	for _, u := range s.Upstreams {
		raw, err := u.Client.CallContext(ctx, "tools/list", json.RawMessage(m.Params))
		if cancelled(ctx, err) {
			return nil
		}
		if err != nil {
			return s.replyError(m, mcp.CodeInternalError, fmt.Sprintf("upstream %s tools/list: %v", u.Name, err))
		}
		var res struct {
			Tools []json.RawMessage `json:"tools"`
		}
		if err := json.Unmarshal(raw, &res); err != nil {
			return s.replyError(m, mcp.CodeInternalError, fmt.Sprintf("upstream %s tools/list: %v", u.Name, err))
		}
		merged = append(merged, res.Tools...)
	}
	raw, err := json.Marshal(map[string]any{"tools": merged})
	if err != nil {
		return err
	}
	return s.reply(m, raw)
}

func (s *Server) handleToolsCall(ctx context.Context, m *mcp.Message) error {
	var params struct {
		Name      string          `json:"name"`
		Arguments json.RawMessage `json:"arguments"`
	}
	// Params the proxy and the upstream could read differently are
	// refused before the upstream runs: with a duplicate "name", Go would
	// route and receipt the last value while the upstream may execute
	// the first. Canonicalize is the strict reader (receipt package).
	if _, err := receipt.Canonicalize(m.Params); len(m.Params) > 0 && err != nil {
		return s.replyError(m, mcp.CodeInvalidParams, fmt.Sprintf("tools/call: params: %v", err))
	}
	if err := json.Unmarshal(m.Params, &params); err != nil || params.Name == "" {
		return s.replyError(m, mcp.CodeInvalidParams, "tools/call: missing tool name")
	}
	s.mu.RLock()
	u, ok := s.routes[params.Name]
	s.mu.RUnlock()
	if !ok {
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
	if err := s.record(params.Name, params.Arguments, result, latency); err != nil {
		return s.replyError(m, mcp.CodeInternalError, fmt.Sprintf("receipt: %v", err))
	}
	return s.reply(m, result)
}

// record writes one signed receipt for a completed tools/call.
func (s *Server) record(tool string, args, result json.RawMessage, latencyMS int64) error {
	if len(args) == 0 {
		args = json.RawMessage(`{}`)
	}
	argsCanon, err := receipt.Canonicalize(args)
	if err != nil {
		return fmt.Errorf("canonicalize args: %w", err)
	}
	responseCanon, err := receipt.Canonicalize(result)
	if err != nil {
		return fmt.Errorf("canonicalize response: %w", err)
	}
	payload, source := resultPayload(result)
	resultCanon, err := receipt.Canonicalize(payload)
	if err != nil {
		return fmt.Errorf("canonicalize result: %w", err)
	}

	// A tool error is receipted, because the agent saw it, but it is not
	// evidence: numbers in an error payload describe the failure, not
	// data the tool returned, so it carries no facts.
	var facts []receipt.Fact
	var dataAsOf string
	if schema, ok := s.Schemas[tool]; ok && !isToolError(result) {
		facts, err = schema.Extract(resultCanon)
		if err != nil {
			return err
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
	return s.Log.AppendNextTurn(r)
}

// isToolError reports whether an MCP tools/call result is flagged as a
// tool-level error (isError: true).
func isToolError(result json.RawMessage) bool {
	var res struct {
		IsError bool `json:"isError"`
	}
	return json.Unmarshal(result, &res) == nil && res.IsError
}

// resultPayload picks the JSON document facts are extracted from:
// structuredContent when the upstream provides it, else the first text
// content block when it parses as JSON, else the whole MCP result.
func resultPayload(result json.RawMessage) (json.RawMessage, string) {
	var res struct {
		StructuredContent json.RawMessage `json:"structuredContent"`
		Content           []struct {
			Type string `json:"type"`
			Text string `json:"text"`
		} `json:"content"`
	}
	if err := json.Unmarshal(result, &res); err == nil {
		if sc := bytes.TrimSpace(res.StructuredContent); len(sc) > 0 && !bytes.Equal(sc, []byte("null")) {
			return res.StructuredContent, "structuredContent"
		}
		for i, c := range res.Content {
			// Only a JSON object or array is a document facts can come
			// from; a scalar such as "1" is valid JSON but would shadow
			// the real payload in a later block (#20).
			text := bytes.TrimSpace([]byte(c.Text))
			if c.Type == "text" && len(text) > 0 && (text[0] == '{' || text[0] == '[') && json.Valid(text) {
				return json.RawMessage(c.Text), fmt.Sprintf("content/%d/text", i)
			}
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
