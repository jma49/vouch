package proxy

import (
	"fmt"
	"os"
	"os/exec"
	"regexp"
	"strings"
	"time"

	"github.com/jma49/vouch/proxy/internal/mcp"
)

// UpstreamSpec is one --upstream flag value.
type UpstreamSpec struct {
	// Name identifies the upstream in fixtures and error messages. It
	// must be unique and stable across runs: record keys each
	// upstream's tools/list fixture by it, and replay restores
	// upstreams by it (docs/design.md section 8.1).
	Name    string
	Command string
}

// upstreamName is the syntax of an explicit name in "name=command".
var upstreamName = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9_.-]*$`)

// ParseUpstreams parses --upstream values of the form "command" or
// "name=command". Without an explicit name, the name is the whole
// command with whitespace normalized, so upstreams that share an
// executable ("python3 a.py", "python3 b.py") stay distinct. The name
// prefix is recognized only before the first whitespace and only when
// it is a plain identifier, so "=" inside a path or flag is left alone.
// Duplicate names are an error rather than a silent overwrite of one
// upstream's fixtures by another's.
func ParseUpstreams(specs []string) ([]UpstreamSpec, error) {
	out := make([]UpstreamSpec, 0, len(specs))
	seen := make(map[string]bool)
	for _, raw := range specs {
		spec := UpstreamSpec{Command: strings.Join(strings.Fields(raw), " ")}
		if first, _, _ := strings.Cut(spec.Command, " "); strings.Contains(first, "=") {
			name, command, _ := strings.Cut(spec.Command, "=")
			if upstreamName.MatchString(name) {
				spec = UpstreamSpec{Name: name, Command: strings.TrimSpace(command)}
			}
		}
		if spec.Command == "" {
			return nil, fmt.Errorf("proxy: empty upstream command in %q", raw)
		}
		if spec.Name == "" {
			spec.Name = spec.Command
		}
		if seen[spec.Name] {
			return nil, fmt.Errorf("proxy: duplicate upstream name %q (use name=command to tell them apart)", spec.Name)
		}
		seen[spec.Name] = true
		out = append(out, spec)
	}
	return out, nil
}

// Spawn starts an upstream MCP server as a subprocess speaking stdio.
// The command is split on whitespace (no shell quoting; wrap complex
// invocations in a script). Stderr passes through to the proxy's own
// stderr.
func Spawn(spec UpstreamSpec) (*Upstream, error) {
	fields := strings.Fields(spec.Command)
	if len(fields) == 0 {
		return nil, fmt.Errorf("proxy: empty upstream command")
	}
	name := spec.Name
	if name == "" {
		name = strings.Join(fields, " ")
	}
	cmd := exec.Command(fields[0], fields[1:]...)
	cmd.Stderr = os.Stderr

	stdin, err := cmd.StdinPipe()
	if err != nil {
		return nil, fmt.Errorf("proxy: upstream %s: %w", name, err)
	}
	stdout, err := cmd.StdoutPipe()
	if err != nil {
		return nil, fmt.Errorf("proxy: upstream %s: %w", name, err)
	}
	if err := cmd.Start(); err != nil {
		return nil, fmt.Errorf("proxy: start upstream %s: %w", name, err)
	}
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
