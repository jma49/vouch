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
// "name=command". The command is split into words as a POSIX shell
// would (see splitCommand) and stored normalized: words joined by one
// space, each quoted only if it has to be. Without an explicit name,
// the name is that normalized command, so upstreams that share an
// executable ("python3 a.py", "python3 b.py") stay distinct, and a
// command without quotes is named exactly as before quoting existed.
// The name prefix is recognized only before the first whitespace and
// only when it is a plain identifier, so "=" inside a path or flag is
// left alone. Duplicate names are an error rather than a silent
// overwrite of one upstream's fixtures by another's.
func ParseUpstreams(specs []string) ([]UpstreamSpec, error) {
	out := make([]UpstreamSpec, 0, len(specs))
	seen := make(map[string]bool)
	for _, raw := range specs {
		var spec UpstreamSpec
		command := strings.TrimSpace(raw)
		if first, _, _ := strings.Cut(command, " "); strings.Contains(first, "=") {
			name, rest, _ := strings.Cut(command, "=")
			if upstreamName.MatchString(name) {
				spec.Name, command = name, rest
			}
		}
		words, err := splitCommand(command)
		if err != nil {
			return nil, fmt.Errorf("proxy: upstream %q: %w", raw, err)
		}
		spec.Command = joinCommand(words)
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

// splitCommand splits a command line into words the way a POSIX shell
// does, without running one (#70): 'single quotes' keep everything
// literally; "double quotes" keep everything except \" \\ \$ \` escapes;
// a backslash outside quotes escapes the next character. There is no
// expansion of any kind ($VAR, globs, ~, command substitution), and
// operators such as | or > are ordinary characters: an upstream that
// needs them should be a script.
func splitCommand(s string) ([]string, error) {
	var words []string
	var word strings.Builder
	inWord := false
	for i := 0; i < len(s); i++ {
		switch c := s[i]; c {
		case ' ', '\t', '\n':
			if inWord {
				words = append(words, word.String())
				word.Reset()
				inWord = false
			}
		case '\'':
			end := strings.IndexByte(s[i+1:], '\'')
			if end < 0 {
				return nil, fmt.Errorf("unterminated single quote")
			}
			word.WriteString(s[i+1 : i+1+end])
			i += end + 1
			inWord = true
		case '"':
			i++
			for ; i < len(s) && s[i] != '"'; i++ {
				if s[i] == '\\' && i+1 < len(s) && strings.IndexByte("\"\\$`", s[i+1]) >= 0 {
					i++
				}
				word.WriteByte(s[i])
			}
			if i == len(s) {
				return nil, fmt.Errorf("unterminated double quote")
			}
			inWord = true
		case '\\':
			if i+1 == len(s) {
				return nil, fmt.Errorf("trailing backslash")
			}
			i++
			word.WriteByte(s[i])
			inWord = true
		default:
			word.WriteByte(c)
			inWord = true
		}
	}
	if inWord {
		words = append(words, word.String())
	}
	return words, nil
}

// joinCommand is the inverse of splitCommand: words joined by one
// space, each single-quoted only when it contains a character the
// splitter treats specially, so splitCommand(joinCommand(w)) == w.
func joinCommand(words []string) string {
	quoted := make([]string, len(words))
	for i, w := range words {
		if w != "" && !strings.ContainsAny(w, " \t\n'\"\\") {
			quoted[i] = w
			continue
		}
		quoted[i] = "'" + strings.ReplaceAll(w, "'", `'\''`) + "'"
	}
	return strings.Join(quoted, " ")
}

// Spawn starts an upstream MCP server as a subprocess speaking stdio.
// The command is split into words by splitCommand; no shell runs.
// Stderr passes through to the proxy's own stderr.
func Spawn(spec UpstreamSpec) (*Upstream, error) {
	fields, err := splitCommand(spec.Command)
	if err != nil {
		return nil, fmt.Errorf("proxy: upstream %s: %w", spec.Name, err)
	}
	if len(fields) == 0 {
		return nil, fmt.Errorf("proxy: empty upstream command")
	}
	name := spec.Name
	if name == "" {
		name = joinCommand(fields)
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
