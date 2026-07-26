package proxy

import (
	"fmt"
	"os"
	"os/exec"
	"strings"

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
	return &Upstream{
		Name:   fields[0],
		Client: mcp.NewClient(mcp.NewConn(stdout, stdin)),
		Close: func() error {
			stdin.Close()
			return cmd.Wait()
		},
	}, nil
}
