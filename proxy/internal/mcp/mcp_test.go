package mcp

import (
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
