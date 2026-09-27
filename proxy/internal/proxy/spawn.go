package proxy

import (
	"fmt"
	"os"
	"os/exec"
	"strings"
	"time"

	"github.com/jma49/vouch/proxy/internal/mcp"
)

// Spawn starts an upstream MCP server as a subprocess speaking stdio.
// The command is split on whitespace (no shell quoting; wrap complex
// invocations in a script). Stderr passes through to the proxy's own
// stderr.
func Spawn(command string) (*Upstream, error) {
	fields := strings.Fields(command)
	if len(fields) == 0 {
		return nil, fmt.Errorf("proxy: empty upstream command")
	}
	cmd := exec.Command(fields[0], fields[1:]...)
	cmd.Stderr = os.Stderr

	stdin, err := cmd.StdinPipe()
	if err != nil {
		return nil, fmt.Errorf("proxy: upstream %s: %w", fields[0], err)
	}
	stdout, err := cmd.StdoutPipe()
	if err != nil {
		return nil, fmt.Errorf("proxy: upstream %s: %w", fields[0], err)
	}
	if err := cmd.Start(); err != nil {
		return nil, fmt.Errorf("proxy: start upstream %s: %w", fields[0], err)
	}
	name := fields[0]
	return &Upstream{
		Name:   name,
		Client: mcp.NewClient(mcp.NewConn(stdout, stdin)),
		Close: func() error {
			// Closing stdin is the MCP stdio shutdown signal, but an
			// upstream may ignore it or be blocked writing to a pipe no
			// one reads any more; an unbounded Wait would then hold the
			// proxy open forever.
			stdin.Close()
			done := make(chan error, 1)
			go func() { done <- cmd.Wait() }()
			select {
			case err := <-done:
				return err
			case <-time.After(closeTimeout):
				_ = cmd.Process.Kill()
				<-done
				return fmt.Errorf("proxy: upstream %s did not exit within %s of stdin closing; killed", name, closeTimeout)
			}
		},
	}, nil
}

// closeTimeout bounds how long Upstream.Close waits for a graceful exit
// before killing the process. A variable so tests can shorten it.
var closeTimeout = 5 * time.Second
