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
	"fmt"
	"io"
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
	CodeMethodNotFound = -32601
	CodeInvalidParams  = -32602
	CodeInternalError  = -32603
)

// IsNotification reports whether m is a notification (no id).
func (m *Message) IsNotification() bool {
	return m.Method != "" && len(m.ID) == 0
}

// Conn frames Messages over a newline-delimited JSON transport.
// Reads and writes are independently serialized.
type Conn struct {
	rmu sync.Mutex
	wmu sync.Mutex
	sc  *bufio.Scanner
	w   io.Writer
}

// NewConn wraps a reader/writer pair (stdin/stdout of a process, or a
// pipe in tests).
func NewConn(r io.Reader, w io.Writer) *Conn {
	sc := bufio.NewScanner(r)
	sc.Buffer(make([]byte, 0, 1<<20), 16<<20)
	return &Conn{sc: sc, w: w}
}

// Read returns the next message, or io.EOF when the peer closes.
func (c *Conn) Read() (*Message, error) {
	c.rmu.Lock()
	defer c.rmu.Unlock()
	for c.sc.Scan() {
		line := bytes.TrimSpace(c.sc.Bytes())
		if len(line) == 0 {
			continue
		}
		var m Message
		if err := json.Unmarshal(line, &m); err != nil {
			return nil, fmt.Errorf("mcp: bad frame: %w", err)
		}
		return &m, nil
	}
	if err := c.sc.Err(); err != nil {
		return nil, err
	}
	return nil, io.EOF
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
	for {
		m, err := c.conn.Read()
		if err != nil {
			return nil, fmt.Errorf("mcp: %s: %w", method, err)
		}
		if m.IsNotification() {
			continue
		}
		if !bytes.Equal(m.ID, id) {
			return nil, fmt.Errorf("mcp: %s: response id %s does not match request id %s", method, m.ID, id)
		}
		if m.Error != nil {
			return nil, m.Error
		}
		return m.Result, nil
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
