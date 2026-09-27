// Package mcp implements the minimal slice of MCP the proxy needs:
// JSON-RPC 2.0 messages over a newline-delimited stdio transport, plus
// a multiplexing client for talking to upstream servers. Stdlib only — the
// federation surface (initialize, tools/list, tools/call) is small
// enough that an SDK would cost more than it saves (docs/design.md
// section 12).
package mcp

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"strconv"
	"sync"
)

// Message is a JSON-RPC 2.0 request, response, or notification.
type Message struct {
	JSONRPC string          `json:"jsonrpc"`
	ID      json.RawMessage `json:"id,omitempty"`
	Method  string          `json:"method,omitempty"`
	Params  json.RawMessage `json:"params,omitempty"`
	Result  json.RawMessage `json:"result,omitempty"`
	Error   *Error          `json:"error,omitempty"`
}

// Error is a JSON-RPC 2.0 error object.
type Error struct {
	Code    int             `json:"code"`
	Message string          `json:"message"`
	Data    json.RawMessage `json:"data,omitempty"`
}

func (e *Error) Error() string {
	return fmt.Sprintf("jsonrpc error %d: %s", e.Code, e.Message)
}

// Standard JSON-RPC error codes used by the proxy.
const (
	CodeParseError     = -32700
	CodeInvalidRequest = -32600
	CodeMethodNotFound = -32601
	CodeInvalidParams  = -32602
	CodeInternalError  = -32603
)

// MaxFrame bounds one newline-delimited frame. A peer that exceeds it
// loses that frame, not the connection.
const MaxFrame = 16 << 20

// ErrFrameTooLarge is wrapped by the FrameError for a line over the
// frame limit.
var ErrFrameTooLarge = errors.New("frame exceeds size limit")

// FrameError reports one line that is not a usable JSON-RPC message.
// The connection stays readable: the caller decides whether to answer
// it (a server, JSON-RPC 2.0 section 5.1) or skip it (a client).
type FrameError struct {
	Code int // CodeParseError or CodeInvalidRequest
	Err  error
}

func (e *FrameError) Error() string { return "mcp: bad frame: " + e.Err.Error() }
func (e *FrameError) Unwrap() error { return e.Err }

// IsNotification reports whether m is a notification (no id).
func (m *Message) IsNotification() bool {
	return m.Method != "" && len(m.ID) == 0
}

// Conn frames Messages over a newline-delimited JSON transport.
// Reads and writes are independently serialized.
type Conn struct {
	rmu      sync.Mutex
	wmu      sync.Mutex
	r        *bufio.Reader
	w        io.Writer
	maxFrame int
	line     []byte
}

// NewConn wraps a reader/writer pair (stdin/stdout of a process, or a
// pipe in tests).
func NewConn(r io.Reader, w io.Writer) *Conn {
	return &Conn{r: bufio.NewReaderSize(r, 64<<10), w: w, maxFrame: MaxFrame}
}

// Read returns the next message, or io.EOF when the peer closes. A
// line that is not a single JSON-RPC object yields a *FrameError and
// leaves the connection positioned at the next line, so one bad frame
// never ends a session. (bufio.Scanner, used before, could not resume
// after an oversized line: its error is sticky.)
func (c *Conn) Read() (*Message, error) {
	c.rmu.Lock()
	defer c.rmu.Unlock()
	for {
		raw, err := c.readLine()
		if err != nil {
			return nil, err
		}
		line := bytes.TrimSpace(raw)
		if len(line) == 0 {
			continue
		}
		return decodeFrame(line)
	}
}

func decodeFrame(line []byte) (*Message, error) {
	if !json.Valid(line) {
		var v any
		err := json.Unmarshal(line, &v) // for a precise syntax error
		if err == nil {
			err = errors.New("invalid JSON")
		}
		return nil, &FrameError{Code: CodeParseError, Err: err}
	}
	switch line[0] {
	case '{':
	case '[':
		// MCP 2025-06-18 removed JSON-RPC batching; a batch would also
		// need one receipt per element under one response.
		return nil, &FrameError{Code: CodeInvalidRequest, Err: errors.New("batch requests are not supported")}
	default:
		return nil, &FrameError{Code: CodeInvalidRequest, Err: errors.New("message is not a JSON object")}
	}
	var m Message
	if err := json.Unmarshal(line, &m); err != nil {
		return nil, &FrameError{Code: CodeInvalidRequest, Err: err}
	}
	return &m, nil
}

// readLine returns the next line, terminator included. A final line
// without a newline is still returned. A line longer than maxFrame is
// consumed through its newline and reported as ErrFrameTooLarge, so
// the next read starts on a frame boundary.
func (c *Conn) readLine() ([]byte, error) {
	c.line = c.line[:0]
	for {
		chunk, err := c.r.ReadSlice('\n')
		if len(c.line)+len(chunk) > c.maxFrame+1 { // +1: the newline
			for errors.Is(err, bufio.ErrBufferFull) {
				_, err = c.r.ReadSlice('\n')
			}
			if err != nil && !errors.Is(err, io.EOF) {
				return nil, err
			}
			c.line = c.line[:0]
			return nil, &FrameError{Code: CodeInvalidRequest,
				Err: fmt.Errorf("%w of %d bytes", ErrFrameTooLarge, c.maxFrame)}
		}
		c.line = append(c.line, chunk...)
		switch {
		case err == nil:
			return c.line, nil
		case errors.Is(err, bufio.ErrBufferFull):
			continue
		case errors.Is(err, io.EOF):
			if len(c.line) > 0 {
				return c.line, nil
			}
			return nil, io.EOF
		default:
			return nil, err
		}
	}
}

// Write sends one message as a single line.
func (c *Conn) Write(m *Message) error {
	if m.JSONRPC == "" {
		m.JSONRPC = "2.0"
	}
	raw, err := json.Marshal(m)
	if err != nil {
		return fmt.Errorf("mcp: marshal frame: %w", err)
	}
	c.wmu.Lock()
	defer c.wmu.Unlock()
	if _, err := c.w.Write(append(raw, '\n')); err != nil {
		return fmt.Errorf("mcp: write frame: %w", err)
	}
	return nil
}

// Handler receives the messages a peer sends that are not responses to
// the client's own calls.
type Handler interface {
	// HandleNotification is called for each notification, in arrival
	// order, on the client's reader goroutine: it must not block.
	HandleNotification(m *Message)
	// HandleRequest answers a request from the peer (MCP server-to-
	// client requests such as sampling). It runs on its own goroutine;
	// ctx is cancelled if the peer cancels the request, in which case
	// no response is sent. An *Error is sent as is; any other error as
	// an internal error.
	HandleRequest(ctx context.Context, m *Message) (json.RawMessage, error)
}

// Client multiplexes calls to one peer over a connection: any number
// of calls may be in flight, matched to responses by id (#67). A reader
// goroutine, started by the first call, routes every incoming message:
// responses to their callers, notifications and requests to the
// Handler. Without a Handler, notifications are dropped and requests
// are answered with method-not-found.
type Client struct {
	// Logf reports frames the client skips; nil means log.Printf.
	Logf func(format string, args ...any)

	conn      *Conn
	startOnce sync.Once

	mu       sync.Mutex
	handler  Handler
	nextID   int64
	pending  map[string]chan reply         // our requests, by id
	inbound  map[string]context.CancelFunc // peer requests being handled, by id
	closeErr error                         // set when the reader stops
}

// NewClient wraps an established connection.
func NewClient(conn *Conn) *Client {
	return &Client{
		conn:    conn,
		pending: make(map[string]chan reply),
		inbound: make(map[string]context.CancelFunc),
	}
}

// Handle sets the handler for notifications and requests from the peer.
func (c *Client) Handle(h Handler) {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.handler = h
}

// Call is CallContext without cancellation.
func (c *Client) Call(method string, params any) (json.RawMessage, error) {
	return c.CallContext(context.Background(), method, params)
}

// CallContext sends a request and waits for its response. A JSON-RPC
// error response is returned as *Error. If ctx ends first, the peer is
// sent notifications/cancelled for the request (MCP cancellation), a
// late response is discarded, and ctx's error is returned.
func (c *Client) CallContext(ctx context.Context, method string, params any) (json.RawMessage, error) {
	c.startOnce.Do(func() { go c.readLoop() })
	req := &Message{Method: method}
	if params != nil {
		raw, err := json.Marshal(params)
		if err != nil {
			return nil, fmt.Errorf("mcp: marshal params: %w", err)
		}
		req.Params = raw
	}
	done := make(chan reply, 1)
	c.mu.Lock()
	if c.closeErr != nil {
		err := c.closeErr
		c.mu.Unlock()
		return nil, fmt.Errorf("mcp: %s: %w", method, err)
	}
	c.nextID++
	req.ID = json.RawMessage(strconv.FormatInt(c.nextID, 10))
	key := string(req.ID)
	c.pending[key] = done
	c.mu.Unlock()

	if err := c.conn.Write(req); err != nil {
		c.forget(key)
		return nil, err
	}
	select {
	case r := <-done:
		switch {
		case r.err != nil:
			return nil, fmt.Errorf("mcp: %s: %w", method, r.err)
		case r.msg.Error != nil:
			return nil, r.msg.Error
		}
		return r.msg.Result, nil
	case <-ctx.Done():
		c.forget(key)
		reason, _ := json.Marshal(map[string]any{"requestId": req.ID, "reason": ctx.Err().Error()})
		if err := c.conn.Write(&Message{Method: "notifications/cancelled", Params: reason}); err != nil {
			c.logf("mcp: %s: send cancellation: %v", method, err)
		}
		return nil, ctx.Err()
	}
}

// reply ends a pending call: the peer's response, or the error that
// means none will come.
type reply struct {
	msg *Message
	err error
}

func (c *Client) forget(key string) {
	c.mu.Lock()
	defer c.mu.Unlock()
	delete(c.pending, key)
}

// readLoop routes incoming messages until the connection ends, then
// fails every pending call. Upstreams share stdout with their own
// logging more often than they should, so anything unreadable is
// skipped rather than ending the connection; only EOF or an I/O error
// does.
func (c *Client) readLoop() {
	for {
		m, err := c.conn.Read()
		var fe *FrameError
		if errors.As(err, &fe) {
			if errors.Is(err, ErrFrameTooLarge) {
				// Its id is unreadable. With one call in flight the frame
				// can only be that call's response, which is lost: fail
				// the call rather than leave it waiting forever.
				c.failSole(err)
			} else {
				c.logf("mcp: skipping peer output: %v", err)
			}
			continue
		}
		if err != nil {
			c.shutdown(err)
			return
		}
		switch {
		case m.Method != "" && len(m.ID) == 0:
			c.notification(m)
		case m.Method != "":
			go c.request(m)
		default:
			c.response(m)
		}
	}
}

func (c *Client) response(m *Message) {
	c.mu.Lock()
	defer c.mu.Unlock()
	if bytes.Equal(m.ID, nullID) && m.Error != nil {
		// The peer could not read a request (JSON-RPC 2.0 section 5.1).
		// It can be attributed only when one call is in flight.
		if len(c.pending) == 1 {
			for key, done := range c.pending {
				delete(c.pending, key)
				done <- reply{msg: m}
			}
			return
		}
		c.logf("mcp: peer error with id null, %d calls in flight: %s", len(c.pending), m.Error.Message)
		return
	}
	key := string(bytes.TrimSpace(m.ID))
	done, ok := c.pending[key]
	if !ok {
		c.logf("mcp: discarding response with unknown id %s", m.ID)
		return
	}
	delete(c.pending, key)
	done <- reply{msg: m}
}

func (c *Client) failSole(err error) {
	c.mu.Lock()
	defer c.mu.Unlock()
	if len(c.pending) != 1 {
		c.logf("mcp: %v, %d calls in flight", err, len(c.pending))
		return
	}
	for key, done := range c.pending {
		delete(c.pending, key)
		done <- reply{err: err}
	}
}

func (c *Client) shutdown(err error) {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.closeErr = err
	for key, done := range c.pending {
		delete(c.pending, key)
		done <- reply{err: err}
	}
	for _, cancel := range c.inbound {
		cancel()
	}
}

func (c *Client) notification(m *Message) {
	c.mu.Lock()
	h := c.handler
	if m.Method == "notifications/cancelled" {
		var p struct {
			RequestID json.RawMessage `json:"requestId"`
		}
		if json.Unmarshal(m.Params, &p) == nil {
			if cancel, ok := c.inbound[string(bytes.TrimSpace(p.RequestID))]; ok {
				cancel()
				c.mu.Unlock()
				return // one of ours to cancel; not the handler's business
			}
		}
	}
	c.mu.Unlock()
	if h != nil {
		h.HandleNotification(m)
	}
}

func (c *Client) request(m *Message) {
	key := string(bytes.TrimSpace(m.ID))
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	c.mu.Lock()
	h := c.handler
	c.inbound[key] = cancel
	c.mu.Unlock()
	defer func() {
		c.mu.Lock()
		delete(c.inbound, key)
		c.mu.Unlock()
	}()

	resp := &Message{ID: m.ID}
	if h == nil {
		resp.Error = &Error{Code: CodeMethodNotFound, Message: fmt.Sprintf("method %q not supported", m.Method)}
	} else if result, err := h.HandleRequest(ctx, m); err != nil {
		var rpcErr *Error
		if !errors.As(err, &rpcErr) {
			rpcErr = &Error{Code: CodeInternalError, Message: err.Error()}
		}
		resp.Error = rpcErr
	} else {
		resp.Result = result
	}
	if ctx.Err() != nil {
		return // cancelled by the peer: MCP says not to respond
	}
	if err := c.conn.Write(resp); err != nil {
		c.logf("mcp: answer %s: %v", m.Method, err)
	}
}

var nullID = json.RawMessage("null")

func (c *Client) logf(format string, args ...any) {
	if c.Logf != nil {
		c.Logf(format, args...)
	} else {
		log.Printf(format, args...)
	}
}

// Notify sends a notification (no response expected).
func (c *Client) Notify(method string, params any) error {
	m := &Message{Method: method}
	if params != nil {
		raw, err := json.Marshal(params)
		if err != nil {
			return fmt.Errorf("mcp: marshal params: %w", err)
		}
		m.Params = raw
	}
	return c.conn.Write(m)
}
