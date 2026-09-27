package mcp

import (
	"encoding/json"
	"errors"
	"io"
	"strings"
	"testing"
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
