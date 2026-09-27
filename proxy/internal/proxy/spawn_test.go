package proxy

import (
	"encoding/json"
	"errors"
	"io"
	"os"
	"strings"
	"testing"
	"time"

	"github.com/jma49/vouch/proxy/internal/mcp"
)

// helperEnv selects a behavior when the test binary re-executes itself
// as an upstream process (the os/exec TestHelperProcess pattern).
const helperEnv = "VOUCH_TEST_UPSTREAM"

func TestHelperProcess(t *testing.T) {
	mode := os.Getenv(helperEnv)
	if mode == "" {
		return
	}
	defer os.Exit(0)
	switch mode {
	case "ignore-eof":
		// An upstream that does not exit when its stdin closes.
		_, _ = io.Copy(io.Discard, os.Stdin)
		time.Sleep(time.Hour)
	case "big-frame":
		// Answers the first tools/call with a frame over mcp.MaxFrame,
		// every later call normally.
		conn := mcp.NewConn(os.Stdin, os.Stdout)
		calls := 0
		for {
			m, err := conn.Read()
			if err != nil {
				return
			}
			if m.IsNotification() {
				continue
			}
			result := json.RawMessage(`{"content":[{"type":"text","text":"ok"}]}`)
			if m.Method == "tools/call" {
				calls++
				if calls == 1 {
					big := strings.Repeat("x", mcp.MaxFrame+1<<20)
					result = json.RawMessage(`{"content":[{"type":"text","text":"` + big + `"}]}`)
				}
			}
			if err := conn.Write(&mcp.Message{ID: m.ID, Result: result}); err != nil {
				return
			}
		}
	}
}

func spawnHelper(t *testing.T, mode string) *Upstream {
	t.Helper()
	t.Setenv(helperEnv, mode)
	u, err := Spawn(UpstreamSpec{Name: mode, Command: os.Args[0] + " -test.run=^TestHelperProcess$"})
	if err != nil {
		t.Fatal(err)
	}
	return u
}

// TestParseUpstreams pins upstream identity: the name keys fixtures
// and routes errors, so two upstreams must never share one. Deriving it
// from the executable alone collapsed "python3 a.py" and "python3 b.py".
func TestParseUpstreams(t *testing.T) {
	cases := []struct {
		name    string
		specs   []string
		want    []UpstreamSpec
		wantErr string
	}{
		{"shared executable", []string{"python3 fake.py tool_a", "python3 fake.py tool_b"}, []UpstreamSpec{
			{Name: "python3 fake.py tool_a", Command: "python3 fake.py tool_a"},
			{Name: "python3 fake.py tool_b", Command: "python3 fake.py tool_b"},
		}, ""},
		{"whitespace normalized", []string{"  python3   -m  market "}, []UpstreamSpec{
			{Name: "python3 -m market", Command: "python3 -m market"},
		}, ""},
		{"explicit name", []string{"market=python3 -m market", "quotes=python3 -m market --quotes"}, []UpstreamSpec{
			{Name: "market", Command: "python3 -m market"},
			{Name: "quotes", Command: "python3 -m market --quotes"},
		}, ""},
		{"equals inside a path is not a name", []string{"/opt/a=b/bin/srv --x=1"}, []UpstreamSpec{
			{Name: "/opt/a=b/bin/srv --x=1", Command: "/opt/a=b/bin/srv --x=1"},
		}, ""},
		{"empty", []string{"  "}, nil, "empty upstream command"},
		{"name without command", []string{"market="}, nil, "empty upstream command"},
		{"duplicate command", []string{"python3 a.py", "python3  a.py"}, nil, "duplicate upstream name"},
		{"duplicate explicit name", []string{"m=python3 a.py", "m=python3 b.py"}, nil, "duplicate upstream name"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got, err := ParseUpstreams(tc.specs)
			if tc.wantErr != "" {
				if err == nil || !strings.Contains(err.Error(), tc.wantErr) {
					t.Fatalf("got %v, %v; want error mentioning %q", got, err, tc.wantErr)
				}
				return
			}
			if err != nil {
				t.Fatal(err)
			}
			if len(got) != len(tc.want) {
				t.Fatalf("got %+v, want %+v", got, tc.want)
			}
			for i := range got {
				if got[i] != tc.want[i] {
					t.Fatalf("spec %d: got %+v, want %+v", i, got[i], tc.want[i])
				}
			}
		})
	}
}

// TestSpawnUsesSpecName guards the original bug at its source: Spawn
// named every upstream after its executable.
func TestSpawnUsesSpecName(t *testing.T) {
	cmd := os.Args[0] + " -test.run=^TestHelperProcess$"
	specs, err := ParseUpstreams([]string{cmd + " a", cmd + " b"})
	if err != nil {
		t.Fatal(err)
	}
	var names []string
	for _, spec := range specs {
		u, err := Spawn(spec)
		if err != nil {
			t.Fatal(err)
		}
		defer u.Close()
		names = append(names, u.Name)
	}
	if names[0] == names[1] {
		t.Fatalf("both upstreams named %q", names[0])
	}
}

// TestCloseKillsUpstreamThatIgnoresEOF pins that shutdown is bounded:
// an upstream that keeps running after its stdin closes is killed
// after closeTimeout instead of holding the proxy open forever.
func TestCloseKillsUpstreamThatIgnoresEOF(t *testing.T) {
	defer func(d time.Duration) { closeTimeout = d }(closeTimeout)
	closeTimeout = 200 * time.Millisecond

	u := spawnHelper(t, "ignore-eof")
	done := make(chan error, 1)
	go func() { done <- u.Close() }()
	select {
	case err := <-done:
		if err == nil || !strings.Contains(err.Error(), "killed") {
			t.Fatalf("Close: got %v, want a killed-after-timeout error", err)
		}
	case <-time.After(10 * time.Second):
		t.Fatal("Close did not return: shutdown waits on the upstream forever")
	}
}

// TestOversizedUpstreamFrameFailsOnlyThatCall pins that a response over
// the frame limit is a bounded error for its call, not a dead upstream.
func TestOversizedUpstreamFrameFailsOnlyThatCall(t *testing.T) {
	u := spawnHelper(t, "big-frame")
	defer u.Close()

	params := map[string]any{"name": "t", "arguments": map[string]any{}}
	if _, err := u.Client.Call("tools/call", params); !errors.Is(err, mcp.ErrFrameTooLarge) {
		t.Fatalf("first call: got %v, want ErrFrameTooLarge", err)
	}
	res, err := u.Client.Call("tools/call", params)
	if err != nil {
		t.Fatalf("second call: %v", err)
	}
	if !strings.Contains(string(res), `"ok"`) {
		t.Fatalf("second call result: %.200s", res)
	}
}
