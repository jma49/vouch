// Package fixture implements record/replay of upstream responses
// (docs/design.md section 8.1, the VCR pattern): responses are recorded
// once, content-addressed by hash(tool + canonical_args), then replayed
// for all subsequent runs. Replay mode never touches the network.
package fixture

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"time"

	"github.com/jma49/vouch/proxy/internal/receipt"
)

// Store is a directory of fixture files: call_<key>.json for tool
// calls, tools_<key>.json for per-upstream tools/list results.
type Store struct {
	Dir string
}

// Key content-addresses one tool call.
func Key(tool string, argsCanonical []byte) string {
	h := sha256.New()
	h.Write([]byte(tool))
	h.Write([]byte{'\n'})
	h.Write(argsCanonical)
	return hex.EncodeToString(h.Sum(nil))
}

type callFixture struct {
	Tool          string          `json:"tool"`
	ArgsCanonical json.RawMessage `json:"args_canonical"`
	Result        json.RawMessage `json:"result"`
	RecordedAt    time.Time       `json:"recorded_at"`
}

type toolsFixture struct {
	Upstream   string          `json:"upstream"`
	Result     json.RawMessage `json:"result"`
	RecordedAt time.Time       `json:"recorded_at"`
}

func (s *Store) write(name string, v any) error {
	if err := os.MkdirAll(s.Dir, 0o755); err != nil {
		return fmt.Errorf("fixture: mkdir: %w", err)
	}
	raw, err := json.MarshalIndent(v, "", "  ")
	if err != nil {
		return fmt.Errorf("fixture: marshal %s: %w", name, err)
	}
	if err := os.WriteFile(filepath.Join(s.Dir, name), append(raw, '\n'), 0o644); err != nil {
		return fmt.Errorf("fixture: write %s: %w", name, err)
	}
	return nil
}

// SaveCall records one tools/call response.
func (s *Store) SaveCall(tool string, argsCanonical, result json.RawMessage, at time.Time) error {
	return s.write("call_"+Key(tool, argsCanonical)+".json", callFixture{
		Tool: tool, ArgsCanonical: argsCanonical, Result: result, RecordedAt: at.UTC(),
	})
}

// LoadCall returns the recorded response for (tool, args), if any.
func (s *Store) LoadCall(tool string, argsCanonical []byte) (json.RawMessage, bool, error) {
	raw, err := os.ReadFile(filepath.Join(s.Dir, "call_"+Key(tool, argsCanonical)+".json"))
	if os.IsNotExist(err) {
		return nil, false, nil
	}
	if err != nil {
		return nil, false, fmt.Errorf("fixture: %w", err)
	}
	var f callFixture
	if err := json.Unmarshal(raw, &f); err != nil {
		return nil, false, fmt.Errorf("fixture: parse call fixture: %w", err)
	}
	return f.Result, true, nil
}

// SaveTools records one upstream's tools/list result.
func (s *Store) SaveTools(upstream string, result json.RawMessage, at time.Time) error {
	sum := sha256.Sum256([]byte(upstream))
	return s.write("tools_"+hex.EncodeToString(sum[:8])+".json", toolsFixture{
		Upstream: upstream, Result: result, RecordedAt: at.UTC(),
	})
}

// LoadAllTools returns every recorded tools/list result, keyed by
// upstream name.
func (s *Store) LoadAllTools() (map[string]json.RawMessage, error) {
	entries, err := os.ReadDir(s.Dir)
	if err != nil {
		return nil, fmt.Errorf("fixture: read dir: %w", err)
	}
	out := make(map[string]json.RawMessage)
	for _, e := range entries {
		if !strings.HasPrefix(e.Name(), "tools_") || !strings.HasSuffix(e.Name(), ".json") {
			continue
		}
		raw, err := os.ReadFile(filepath.Join(s.Dir, e.Name()))
		if err != nil {
			return nil, fmt.Errorf("fixture: %w", err)
		}
		var f toolsFixture
		if err := json.Unmarshal(raw, &f); err != nil {
			return nil, fmt.Errorf("fixture: parse %s: %w", e.Name(), err)
		}
		out[f.Upstream] = f.Result
	}
	return out, nil
}

// Epoch returns the latest recorded_at across all fixtures — the
// logical-clock epoch for replay runs. Zero time when the store is
// empty.
func (s *Store) Epoch() (time.Time, error) {
	entries, err := os.ReadDir(s.Dir)
	if err != nil {
		return time.Time{}, fmt.Errorf("fixture: read dir: %w", err)
	}
	var epoch time.Time
	for _, e := range entries {
		if !strings.HasSuffix(e.Name(), ".json") {
			continue
		}
		raw, err := os.ReadFile(filepath.Join(s.Dir, e.Name()))
		if err != nil {
			return time.Time{}, fmt.Errorf("fixture: %w", err)
		}
		var meta struct {
			RecordedAt time.Time `json:"recorded_at"`
		}
		if err := json.Unmarshal(raw, &meta); err != nil {
			continue
		}
		if meta.RecordedAt.After(epoch) {
			epoch = meta.RecordedAt
		}
	}
	return epoch, nil
}

// canonicalCallArgs extracts and canonicalizes the arguments of a
// tools/call params value.
func canonicalCallArgs(params any) (tool string, argsCanonical []byte, err error) {
	raw, err := json.Marshal(params)
	if err != nil {
		return "", nil, fmt.Errorf("fixture: marshal params: %w", err)
	}
	var p struct {
		Name      string          `json:"name"`
		Arguments json.RawMessage `json:"arguments"`
	}
	if err := json.Unmarshal(raw, &p); err != nil {
		return "", nil, fmt.Errorf("fixture: parse tools/call params: %w", err)
	}
	if len(p.Arguments) == 0 {
		p.Arguments = json.RawMessage(`{}`)
	}
	canon, err := receipt.Canonicalize(p.Arguments)
	if err != nil {
		return "", nil, fmt.Errorf("fixture: canonicalize args: %w", err)
	}
	return p.Name, canon, nil
}
