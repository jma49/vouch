// Package fixture implements record/replay of upstream responses
// (docs/design.md section 8.1, the VCR pattern): responses are recorded
// once, content-addressed by hash(tool + canonical args, numbers
// normalized), then replayed for all subsequent runs. Replay mode never touches the network.
package fixture

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"time"

	"github.com/jma49/vouch/proxy/internal/receipt"
)

// Store is a directory of fixture files: call_<key>.json for tool
// calls, tools_<key>.json for per-upstream tools/list results.
type Store struct {
	Dir string
}

// Key content-addresses one tool call. Arguments are compared as JSON
// values, not as text: canonical JSON keeps number literals verbatim
// (docs/canonical-json.md), but {"limit": 5} and {"limit": 5.0} are the
// same request to an upstream, so numbers are normalized first (#64).
// Receipts are unaffected; they record the literal the agent sent.
func Key(tool string, argsCanonical []byte) (string, error) {
	norm, err := normalizeNumbers(argsCanonical)
	if err != nil {
		return "", err
	}
	h := sha256.New()
	h.Write([]byte(tool))
	h.Write([]byte{'\n'})
	h.Write(norm)
	return hex.EncodeToString(h.Sum(nil)), nil
}

// normalizeNumbers rewrites every number in a JSON document to one
// spelling per value (see normalizeNumber) and re-serializes it with
// sorted keys.
func normalizeNumbers(doc []byte) ([]byte, error) {
	dec := json.NewDecoder(bytes.NewReader(doc))
	dec.UseNumber()
	var v any
	if err := dec.Decode(&v); err != nil {
		return nil, fmt.Errorf("fixture: parse args: %w", err)
	}
	var walk func(any) any
	walk = func(v any) any {
		switch t := v.(type) {
		case json.Number:
			return json.Number(normalizeNumber(string(t)))
		case []any:
			for i := range t {
				t[i] = walk(t[i])
			}
		case map[string]any:
			for k := range t {
				t[k] = walk(t[k])
			}
		}
		return v
	}
	out, err := json.Marshal(walk(v)) // map keys are sorted
	if err != nil {
		return nil, fmt.Errorf("fixture: normalize args: %w", err)
	}
	return out, nil
}

// normalizeNumber spells a JSON number literal as <digits>e<exponent>,
// with no leading or trailing zeros in the digits, so every literal of
// one value maps to one string: 5, 5.0, 5e0, 50e-1, and 0.5E+1 all
// become 5e0, and 0 and -0 become 0. It works on the decimal digits
// and never goes through float64, so integers beyond 2^53 stay
// distinct. An exponent too large to handle exactly is left as written.
func normalizeNumber(lit string) string {
	s, sign := strings.CutPrefix(lit, "-")
	exp := int64(0)
	if i := strings.IndexAny(s, "eE"); i >= 0 {
		e, err := strconv.ParseInt(s[i+1:], 10, 64)
		if err != nil || e > 1<<40 || e < -(1<<40) {
			return lit
		}
		exp, s = e, s[:i]
	}
	whole, frac, _ := strings.Cut(s, ".")
	digits := strings.TrimLeft(whole+frac, "0")
	exp -= int64(len(frac))
	if digits == "" {
		return "0"
	}
	trimmed := strings.TrimRight(digits, "0")
	exp += int64(len(digits) - len(trimmed))
	if sign {
		trimmed = "-" + trimmed
	}
	return trimmed + "e" + strconv.FormatInt(exp, 10)
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
	key, err := Key(tool, argsCanonical)
	if err != nil {
		return err
	}
	return s.write("call_"+key+".json", callFixture{
		Tool: tool, ArgsCanonical: argsCanonical, Result: result, RecordedAt: at.UTC(),
	})
}

// LoadCall returns the recorded response for (tool, args), if any.
func (s *Store) LoadCall(tool string, argsCanonical []byte) (json.RawMessage, bool, error) {
	key, err := Key(tool, argsCanonical)
	if err != nil {
		return nil, false, err
	}
	raw, err := os.ReadFile(filepath.Join(s.Dir, "call_"+key+".json"))
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

// RecordedUpstream is one upstream's recorded tools/list result.
type RecordedUpstream struct {
	Name  string
	Tools json.RawMessage
}

// LoadAllTools returns every recorded tools/list result, sorted by
// upstream name. Replay builds its upstream list, and so the merged
// tools/list the agent sees, in this order; sorting keeps it identical
// across runs (docs/design.md section 8).
func (s *Store) LoadAllTools() ([]RecordedUpstream, error) {
	entries, err := os.ReadDir(s.Dir)
	if err != nil {
		return nil, fmt.Errorf("fixture: read dir: %w", err)
	}
	var out []RecordedUpstream
	seen := make(map[string]string) // upstream -> file
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
		if prev, dup := seen[f.Upstream]; dup {
			return nil, fmt.Errorf("fixture: upstream %q recorded in both %s and %s", f.Upstream, prev, e.Name())
		}
		seen[f.Upstream] = e.Name()
		out = append(out, RecordedUpstream{Name: f.Upstream, Tools: f.Result})
	}
	sort.Slice(out, func(i, j int) bool { return out[i].Name < out[j].Name })
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
	if err := receipt.DecodeStrict(raw, &p); err != nil {
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
