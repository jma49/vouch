package proxy

import (
	"io"
	"strings"
	"testing"

	"github.com/jma49/vouch/proxy/internal/mcp"
)

// TestBadFramesAreAnsweredAndServingContinues pins JSON-RPC 2.0
// section 5.1: a frame the proxy cannot parse gets an error with id
// null, and the session keeps going.
func TestBadFramesAreAnsweredAndServingContinues(t *testing.T) {
	cases := []struct {
		name string
		line string
		code int
	}{
		{"not json", "not json", mcp.CodeParseError},
		{"batch", `[{"jsonrpc":"2.0","id":2,"method":"ping"}]`, mcp.CodeInvalidRequest},
		{"not an object", `"ping"`, mcp.CodeInvalidRequest},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			s := startSession(t)
			defer s.shutdown()

			if _, err := io.WriteString(s.send, tc.line+"\n"); err != nil {
				t.Fatal(err)
			}
			m, err := s.raw.Read()
			if err != nil {
				t.Fatalf("read error reply: %v", err)
			}
			if string(m.ID) != "null" || m.Error == nil || m.Error.Code != tc.code {
				t.Fatalf("reply: id=%s error=%+v, want id null code %d", m.ID, m.Error, tc.code)
			}
			if _, err := s.agent.Call("ping", nil); err != nil {
				t.Fatalf("ping after bad frame: %v", err)
			}
		})
	}
}

func TestOversizedDownstreamFrameIsAnswered(t *testing.T) {
	s := startSession(t)
	defer s.shutdown()

	errc := make(chan error, 1)
	go func() {
		_, err := io.WriteString(s.send, `{"pad":"`+strings.Repeat("x", mcp.MaxFrame)+`"}`+"\n")
		errc <- err
	}()
	m, err := s.raw.Read()
	if err != nil {
		t.Fatalf("read error reply: %v", err)
	}
	if err := <-errc; err != nil {
		t.Fatal(err)
	}
	if string(m.ID) != "null" || m.Error == nil || m.Error.Code != mcp.CodeInvalidRequest {
		t.Fatalf("reply: id=%s error=%+v", m.ID, m.Error)
	}
	if _, err := s.agent.Call("ping", nil); err != nil {
		t.Fatalf("ping after oversized frame: %v", err)
	}
}
