package mcp

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"mime"
	"net/http"
	"strings"
	"sync"
	"time"
)

// HTTPClient is the client side of MCP's Streamable HTTP transport, as
// a Transport for Client: an upstream at a URL instead of a subprocess.
// Each request is POSTed; the response, and anything the server sends
// with it, arrives as JSON or as an event stream, and Read returns those
// messages in order of arrival. Once the session is initialized, a GET
// stream carries what the server sends unprompted (list_changed,
// server-to-client requests), if the server offers one. The session id
// and negotiated protocol version ride on every later request.
type HTTPClient struct {
	url    string
	header http.Header
	http   *http.Client
	logf   func(format string, args ...any)

	in     chan *Message
	ctx    context.Context
	cancel context.CancelFunc

	mu        sync.Mutex
	sessionID string
	version   string
	getOnce   sync.Once
	closeOnce sync.Once
}

// NewHTTPClient connects to the MCP endpoint at url. header is added to
// every request (authorization, typically). logf may be nil.
func NewHTTPClient(url string, header http.Header, logf func(format string, args ...any)) *HTTPClient {
	if logf == nil {
		logf = log.Printf
	}
	ctx, cancel := context.WithCancel(context.Background())
	return &HTTPClient{
		url: url, header: header, http: &http.Client{}, logf: logf,
		in: make(chan *Message, 64), ctx: ctx, cancel: cancel,
	}
}

// Read returns the next message from the server, or io.EOF once the
// client is closed.
func (h *HTTPClient) Read() (*Message, error) {
	select {
	case m := <-h.in:
		return m, nil
	case <-h.ctx.Done():
		return nil, io.EOF
	}
}

// Write POSTs one message. A request is sent in the background, so
// Write, like a pipe write, does not wait for the server's answer; a
// transport failure comes back through Read as an error response to
// that request, so the call waiting on it fails. A notification or
// response is sent synchronously and must be accepted.
func (h *HTTPClient) Write(m *Message) error {
	if m.JSONRPC == "" {
		m.JSONRPC = "2.0"
	}
	body, err := json.Marshal(m)
	if err != nil {
		return fmt.Errorf("mcp: marshal frame: %w", err)
	}
	if m.Method == "" || len(m.ID) == 0 {
		resp, err := h.post(body)
		if err != nil {
			return err
		}
		defer resp.Body.Close()
		_, _ = io.Copy(io.Discard, resp.Body)
		if resp.StatusCode/100 != 2 {
			return fmt.Errorf("mcp: %s: POST %s: %s", h.url, m.Method, resp.Status)
		}
		return nil
	}
	go h.request(m, body)
	return nil
}

func (h *HTTPClient) post(body []byte) (*http.Response, error) {
	req, err := http.NewRequestWithContext(h.ctx, http.MethodPost, h.url, bytes.NewReader(body))
	if err != nil {
		return nil, fmt.Errorf("mcp: %w", err)
	}
	h.decorate(req)
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Accept", "application/json, text/event-stream")
	resp, err := h.http.Do(req)
	if err != nil {
		return nil, fmt.Errorf("mcp: %s: %w", h.url, err)
	}
	return resp, nil
}

// decorate adds the caller's headers and the session's.
func (h *HTTPClient) decorate(req *http.Request) {
	for k, vs := range h.header {
		for _, v := range vs {
			req.Header.Add(k, v)
		}
	}
	h.mu.Lock()
	defer h.mu.Unlock()
	if h.sessionID != "" {
		req.Header.Set("Mcp-Session-Id", h.sessionID)
	}
	if h.version != "" {
		req.Header.Set("MCP-Protocol-Version", h.version)
	}
}

func (h *HTTPClient) request(m *Message, body []byte) {
	resp, err := h.post(body)
	if err != nil {
		h.fail(m.ID, err)
		return
	}
	defer resp.Body.Close()
	if resp.StatusCode/100 != 2 {
		h.fail(m.ID, fmt.Errorf("mcp: %s: POST %s: %s%s", h.url, m.Method, resp.Status, errorDetail(resp.Body)))
		return
	}
	if id := resp.Header.Get("Mcp-Session-Id"); id != "" && m.Method == "initialize" {
		h.mu.Lock()
		h.sessionID = id
		h.mu.Unlock()
	}
	answered := false
	deliver := func(got *Message) {
		if got.Method == "" && bytes.Equal(bytes.TrimSpace(got.ID), bytes.TrimSpace(m.ID)) {
			answered = true
			if m.Method == "initialize" && got.Error == nil {
				h.initialized(got.Result)
			}
		}
		h.deliver(got)
	}
	mediaType, _, _ := mime.ParseMediaType(resp.Header.Get("Content-Type"))
	switch mediaType {
	case "application/json":
		raw, err := io.ReadAll(io.LimitReader(resp.Body, MaxFrame+1))
		if err != nil {
			h.fail(m.ID, fmt.Errorf("mcp: %s: read response: %w", h.url, err))
			return
		}
		got, err := decodeFrame(bytes.TrimSpace(raw))
		if err != nil {
			h.fail(m.ID, err)
			return
		}
		deliver(got)
	case "text/event-stream":
		events := newSSEReader(resp.Body)
		for !answered {
			got, err := events.Next()
			var fe *FrameError
			if errors.As(err, &fe) {
				h.logf("mcp: %s: skipping event: %v", h.url, err)
				continue
			}
			if err != nil {
				break
			}
			deliver(got)
		}
	default:
		h.fail(m.ID, fmt.Errorf("mcp: %s: unexpected response type %q", h.url, mediaType))
		return
	}
	if !answered && h.ctx.Err() == nil {
		// The stream ended without the response; vouch does not resume
		// streams (Last-Event-ID), so the call fails rather than waits.
		h.fail(m.ID, fmt.Errorf("mcp: %s: stream for %s ended without a response", h.url, m.Method))
	}
}

// initialized records the negotiated version and opens the GET stream.
func (h *HTTPClient) initialized(result json.RawMessage) {
	var r struct {
		ProtocolVersion string `json:"protocolVersion"`
	}
	_ = json.Unmarshal(result, &r)
	h.mu.Lock()
	h.version = r.ProtocolVersion
	h.mu.Unlock()
	h.getOnce.Do(func() { go h.listen() })
}

// listen holds the GET stream open for messages the server sends on its
// own, reconnecting after a drop. A server without one answers 405,
// which ends listening: its messages then come only with responses.
func (h *HTTPClient) listen() {
	for backoff := 250 * time.Millisecond; h.ctx.Err() == nil; backoff = min(2*backoff, 10*time.Second) {
		req, err := http.NewRequestWithContext(h.ctx, http.MethodGet, h.url, nil)
		if err != nil {
			return
		}
		h.decorate(req)
		req.Header.Set("Accept", "text/event-stream")
		resp, err := h.http.Do(req)
		if err == nil && resp.StatusCode == http.StatusOK {
			backoff = 250 * time.Millisecond
			events := newSSEReader(resp.Body)
			for {
				m, err := events.Next()
				var fe *FrameError
				if errors.As(err, &fe) {
					continue
				}
				if err != nil {
					break
				}
				h.deliver(m)
			}
		}
		if resp != nil {
			resp.Body.Close()
			if resp.StatusCode == http.StatusMethodNotAllowed {
				return
			}
		}
		select {
		case <-time.After(backoff):
		case <-h.ctx.Done():
		}
	}
}

func (h *HTTPClient) deliver(m *Message) {
	select {
	case h.in <- m:
	case <-h.ctx.Done():
	}
}

// fail ends the request with id by handing Client an error response.
func (h *HTTPClient) fail(id json.RawMessage, err error) {
	if h.ctx.Err() != nil {
		return
	}
	h.deliver(&Message{JSONRPC: "2.0", ID: id, Error: &Error{Code: CodeInternalError, Message: err.Error()}})
}

// Close ends the session: the server is told with DELETE (best effort,
// bounded), and in-flight requests and the GET stream are abandoned.
func (h *HTTPClient) Close() error {
	var err error
	h.closeOnce.Do(func() {
		h.mu.Lock()
		session := h.sessionID
		h.mu.Unlock()
		if session != "" {
			ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
			defer cancel()
			req, rerr := http.NewRequestWithContext(ctx, http.MethodDelete, h.url, nil)
			if rerr == nil {
				h.decorate(req)
				if resp, derr := h.http.Do(req); derr == nil {
					resp.Body.Close()
				} else {
					err = fmt.Errorf("mcp: %s: end session: %w", h.url, derr)
				}
			}
		}
		h.cancel()
	})
	return err
}

// errorDetail is the start of an error response's body, for messages.
func errorDetail(body io.Reader) string {
	raw, _ := io.ReadAll(io.LimitReader(body, 512))
	if s := strings.TrimSpace(string(raw)); s != "" {
		return ": " + s
	}
	return ""
}
