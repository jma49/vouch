// Package mcp implements the minimal slice of MCP the proxy needs:
// JSON-RPC 2.0 messages over a newline-delimited stdio transport, plus
// a serial client for talking to upstream servers. Stdlib only — the
// federation surface (initialize, tools/list, tools/call) is small
// enough that an SDK would cost more than it saves (docs/design.md
// section 12).
package mcp

import (
	"bufio"
	"bytes"
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
		// MCP 2025-06-18 removed JSON-RPC batching, and the proxy's
		// one-receipt-per-call path is serial by design.
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

// Client drives one upstream server serially: one Call in flight at a
// time. Notifications arriving while a response is pending are dropped
// — the MVP proxy does not forward upstream notifications
// (docs/design.md section 12, open questions).
type Client struct {
	// Logf reports frames the client skips; nil means log.Printf.
	Logf func(format string, args ...any)

	mu     sync.Mutex
	conn   *Conn
	nextID int64
}

// NewClient wraps an established connection.
func NewClient(conn *Conn) *Client {
	return &Client{conn: conn}
}

// Call sends a request and blocks until its response arrives.
// A JSON-RPC error response is returned as *Error.
func (c *Client) Call(method string, params any) (json.RawMessage, error) {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.nextID++
	id := json.RawMessage(strconv.FormatInt(c.nextID, 10))

	req := &Message{ID: id, Method: method}
	if params != nil {
		raw, err := json.Marshal(params)
		if err != nil {
			return nil, fmt.Errorf("mcp: marshal params: %w", err)
		}
		req.Params = raw
	}
	if err := c.conn.Write(req); err != nil {
		return nil, err
	}
	// Upstreams share stdout with their own logging more often than they
	// should. Returning on the first stray line would leave the real
	// response in the pipe for the next call to read, desynchronizing
	// the upstream for the rest of the session, so anything that is not
	// this call's response is skipped. Only EOF, an I/O error, or a
	// frame too large to read ends the call.
	for {
		m, err := c.conn.Read()
		var fe *FrameError
		if errors.As(err, &fe) && !errors.Is(err, ErrFrameTooLarge) {
			c.logf("mcp: %s: skipping upstream output: %v", method, err)
			continue
		}
		if err != nil {
			return nil, fmt.Errorf("mcp: %s: %w", method, err)
		}
		switch {
		case m.Method != "":
			// Notifications are not forwarded (see Client). Requests from
			// the upstream are not supported yet (docs/pitfalls.md P-022).
			if !m.IsNotification() {
				c.logf("mcp: %s: ignoring upstream request %s (id %s)", method, m.Method, m.ID)
			}
			continue
		case bytes.Equal(m.ID, id):
		case bytes.Equal(m.ID, nullID) && m.Error != nil:
			// The upstream could not read the request (JSON-RPC 2.0
			// section 5.1). With one call in flight it can only be ours.
		default:
			c.logf("mcp: %s: discarding response with id %s while waiting for %s", method, m.ID, id)
			continue
		}
		if m.Error != nil {
			return nil, m.Error
		}
		return m.Result, nil
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
