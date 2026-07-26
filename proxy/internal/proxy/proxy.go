// Package proxy implements the federating MCP server (docs/design.md
// section 2): the agent connects here instead of to its upstreams; the
// proxy forwards every call, records a signed receipt of the
// request/response pair, and returns the result unchanged.
//
// MVP federation surface: initialize, notifications/initialized, ping,
// tools/list (merged across upstreams), tools/call (routed by tool
// name). Resources, prompts, and upstream-initiated notifications are
// out of scope for now and answered with method-not-found.
package proxy

import (
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"

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
	Call(method string, params any) (json.RawMessage, error)
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
	Key       []byte
	SessionID string
	Clock     clock.Clock
	Logf      func(format string, args ...any)

	routes map[string]*Upstream
	turn   int
}

func (s *Server) logf(format string, args ...any) {
	if s.Logf != nil {
		s.Logf(format, args...)
	} else {
		log.Printf(format, args...)
	}
}

// Run serves the downstream connection until EOF.
func (s *Server) Run() error {
	for {
		m, err := s.Down.Read()
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
	switch m.Method {
	case "initialize":
		return s.handleInitialize(m)
	case "notifications/initialized":
		for _, u := range s.Upstreams {
			if err := u.Client.Notify(m.Method, m.Params); err != nil {
				s.logf("proxy: forward initialized to %s: %v", u.Name, err)
			}
		}
		return nil
	case "ping":
		return s.reply(m, json.RawMessage(`{}`))
	case "tools/list":
		return s.handleToolsList(m)
	case "tools/call":
		return s.handleToolsCall(m)
	default:
		if m.IsNotification() {
			return nil // unknown notifications are dropped, per JSON-RPC
		}
		return s.replyError(m, mcp.CodeMethodNotFound, fmt.Sprintf("method %q not federated by vouch proxy", m.Method))
	}
}

func (s *Server) handleInitialize(m *mcp.Message) error {
	var params struct {
		ProtocolVersion string `json:"protocolVersion"`
	}
	_ = json.Unmarshal(m.Params, &params)
	for _, u := range s.Upstreams {
		if _, err := u.Client.Call("initialize", json.RawMessage(m.Params)); err != nil {
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
		raw, err := u.Client.Call("tools/list", nil)
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
	s.routes = routes
	return nil
}

func (s *Server) handleToolsList(m *mcp.Message) error {
	var merged []json.RawMessage
	for _, u := range s.Upstreams {
		raw, err := u.Client.Call("tools/list", json.RawMessage(m.Params))
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

func (s *Server) handleToolsCall(m *mcp.Message) error {
	var params struct {
		Name      string          `json:"name"`
		Arguments json.RawMessage `json:"arguments"`
	}
	if err := json.Unmarshal(m.Params, &params); err != nil || params.Name == "" {
		return s.replyError(m, mcp.CodeInvalidParams, "tools/call: missing tool name")
	}
	u, ok := s.routes[params.Name]
	if !ok {
		return s.replyError(m, mcp.CodeInvalidParams, fmt.Sprintf("tools/call: unknown tool %q", params.Name))
	}

	start := s.Clock.Now()
	result, err := u.Client.Call("tools/call", json.RawMessage(m.Params))
	latency := s.Clock.Now().Sub(start).Milliseconds()
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
	payload := resultPayload(result)
	resultCanon, err := receipt.Canonicalize(payload)
	if err != nil {
		return fmt.Errorf("canonicalize result: %w", err)
	}

	var facts []receipt.Fact
	var dataAsOf string
	if schema, ok := s.Schemas[tool]; ok {
		facts, err = schema.Extract(resultCanon)
		if err != nil {
			return err
		}
		dataAsOf = schema.ResultAsOf(resultCanon)
	}

	r := &receipt.Receipt{
		ReceiptID:         newID(),
		SessionID:         s.SessionID,
		TurnIndex:         s.turn,
		ToolName:          tool,
		ArgsCanonical:     argsCanon,
		ResultCanonical:   resultCanon,
		ResultDigest:      receipt.Digest(resultCanon),
		Facts:             facts,
		DataAsOf:          dataAsOf,
		WallTime:          s.Clock.Now(),
		LogicalTime:       s.Clock.Tick(),
		UpstreamLatencyMS: latencyMS,
	}
	if err := r.Sign(s.Key); err != nil {
		return err
	}
	if err := s.Log.Append(r); err != nil {
		return err
	}
	s.turn++
	return nil
}

// resultPayload picks the JSON document facts are extracted from:
// structuredContent when the upstream provides it, else the first text
// content block when it parses as JSON, else the whole MCP result.
func resultPayload(result json.RawMessage) json.RawMessage {
	var res struct {
		StructuredContent json.RawMessage `json:"structuredContent"`
		Content           []struct {
			Type string `json:"type"`
			Text string `json:"text"`
		} `json:"content"`
	}
	if err := json.Unmarshal(result, &res); err == nil {
		if len(res.StructuredContent) > 0 {
			return res.StructuredContent
		}
		for _, c := range res.Content {
			if c.Type == "text" && json.Valid([]byte(c.Text)) {
				return json.RawMessage(c.Text)
			}
		}
	}
	return result
}

func (s *Server) reply(m *mcp.Message, result json.RawMessage) error {
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
