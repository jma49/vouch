// Package extract implements schema-driven fact extraction (docs/design.md
// section 4): each upstream tool gets a sidecar YAML mapping from JSON
// pointers in the tool result to verifiable Facts. Extraction is
// deterministic and unit-testable; adding a tool means adding config,
// not code.
package extract

import (
	"bytes"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"strings"

	"gopkg.in/yaml.v3"

	"github.com/jma49/vouch/proxy/internal/receipt"
)

// Schema is one sidecar config: how to pull Facts out of one tool's
// results.
type Schema struct {
	Tool      string        `yaml:"tool"`
	EntityPtr string        `yaml:"entity_ptr"`
	AsOfPtr   string        `yaml:"asof_ptr"`
	Facts     []FactMapping `yaml:"facts"`
}

// FactMapping maps one JSON pointer to a Fact. When Each is set, Ptr
// must address an array and the sub-mappings apply to every element
// (one fact per bar in an OHLCV series, for example); sub-mapping
// pointers are relative to the element.
type FactMapping struct {
	Ptr       string        `yaml:"ptr"`
	Metric    string        `yaml:"metric"`
	Unit      string        `yaml:"unit"`
	Timeframe string        `yaml:"timeframe"`
	TolClass  string        `yaml:"tol_class"`
	AsOfPtr   string        `yaml:"asof_ptr"` // per-element as_of, relative to the element
	Each      []FactMapping `yaml:"each"`
}

// LoadSchema reads and validates one sidecar YAML file.
func LoadSchema(path string) (*Schema, error) {
	raw, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf("extract: read schema: %w", err)
	}
	var s Schema
	dec := yaml.NewDecoder(bytes.NewReader(raw))
	dec.KnownFields(true)
	if err := dec.Decode(&s); err != nil {
		return nil, fmt.Errorf("extract: parse schema %s: %w", path, err)
	}
	if err := s.validate(); err != nil {
		return nil, fmt.Errorf("extract: schema %s: %w", path, err)
	}
	return &s, nil
}

// LoadDir loads every *.yaml schema in dir, keyed by tool name.
// Files named *.example.yaml are skipped.
func LoadDir(dir string) (map[string]*Schema, error) {
	entries, err := os.ReadDir(dir)
	if err != nil {
		return nil, fmt.Errorf("extract: read schema dir: %w", err)
	}
	out := make(map[string]*Schema)
	for _, e := range entries {
		name := e.Name()
		if e.IsDir() || !strings.HasSuffix(name, ".yaml") || strings.HasSuffix(name, ".example.yaml") {
			continue
		}
		s, err := LoadSchema(filepath.Join(dir, name))
		if err != nil {
			return nil, err
		}
		if prev, dup := out[s.Tool]; dup {
			_ = prev
			return nil, fmt.Errorf("extract: duplicate schema for tool %q (%s)", s.Tool, name)
		}
		out[s.Tool] = s
	}
	return out, nil
}

func (s *Schema) validate() error {
	if s.Tool == "" {
		return fmt.Errorf("missing tool name")
	}
	if len(s.Facts) == 0 {
		return fmt.Errorf("no fact mappings")
	}
	return validateMappings(s.Facts, false)
}

func validateMappings(ms []FactMapping, nested bool) error {
	for _, m := range ms {
		if m.Ptr == "" {
			return fmt.Errorf("fact mapping missing ptr")
		}
		switch {
		case len(m.Each) > 0:
			if nested {
				return fmt.Errorf("ptr %s: nested each is not supported", m.Ptr)
			}
			if m.Metric != "" {
				return fmt.Errorf("ptr %s: each and metric are mutually exclusive", m.Ptr)
			}
			if err := validateMappings(m.Each, true); err != nil {
				return err
			}
		case m.Metric == "":
			return fmt.Errorf("ptr %s: missing metric", m.Ptr)
		case m.TolClass == "":
			return fmt.Errorf("ptr %s: missing tol_class", m.Ptr)
		}
	}
	return nil
}

// Extract pulls Facts from a canonicalized tool result. Pointers that
// resolve to nothing are skipped (upstreams omit optional fields);
// pointers that resolve to a non-numeric value are an error, because a
// silently mistyped schema would erase coverage.
func (s *Schema) Extract(resultCanonical []byte) ([]receipt.Fact, error) {
	dec := json.NewDecoder(bytes.NewReader(resultCanonical))
	dec.UseNumber()
	var doc any
	if err := dec.Decode(&doc); err != nil {
		return nil, fmt.Errorf("extract: parse result: %w", err)
	}

	entity := stringAt(doc, s.EntityPtr)
	asOf := stringAt(doc, s.AsOfPtr)

	var facts []receipt.Fact
	for _, m := range s.Facts {
		if len(m.Each) == 0 {
			f, ok, err := factAt(doc, m, m.Ptr, entity, asOf)
			if err != nil {
				return nil, err
			}
			if ok {
				facts = append(facts, f)
			}
			continue
		}

		node, err := resolvePtr(doc, m.Ptr)
		if err != nil {
			continue // absent array: skip like any absent field
		}
		arr, ok := node.([]any)
		if !ok {
			return nil, fmt.Errorf("extract: ptr %s: each requires an array, got %T", m.Ptr, node)
		}
		for i, elem := range arr {
			for _, sub := range m.Each {
				elemAsOf := asOf
				if sub.AsOfPtr != "" {
					if v := stringAt(elem, sub.AsOfPtr); v != "" {
						elemAsOf = v
					}
				}
				absPtr := fmt.Sprintf("%s/%d%s", m.Ptr, i, sub.Ptr)
				f, ok, err := factAt(elem, sub, absPtr, entity, elemAsOf)
				if err != nil {
					return nil, err
				}
				if ok {
					facts = append(facts, f)
				}
			}
		}
	}
	return facts, nil
}

// factAt builds one Fact from the mapping m evaluated against doc,
// recording absPtr as provenance. ok=false means the field was absent.
func factAt(doc any, m FactMapping, absPtr, entity, asOf string) (receipt.Fact, bool, error) {
	node, err := resolvePtr(doc, m.Ptr)
	if err != nil {
		return receipt.Fact{}, false, nil
	}
	num, ok := node.(json.Number)
	if !ok {
		return receipt.Fact{}, false, fmt.Errorf("extract: ptr %s: expected number, got %T", absPtr, node)
	}
	v, err := num.Float64()
	if err != nil {
		return receipt.Fact{}, false, fmt.Errorf("extract: ptr %s: %w", absPtr, err)
	}
	return receipt.Fact{
		Entity:    entity,
		Metric:    m.Metric,
		Value:     v,
		Unit:      m.Unit,
		AsOf:      asOf,
		Timeframe: m.Timeframe,
		JSONPtr:   absPtr,
		TolClass:  m.TolClass,
	}, true, nil
}

// ResultAsOf resolves the schema's asof_ptr against a canonical result,
// for the receipt-level data_asof field. Returns "" when unset or absent.
func (s *Schema) ResultAsOf(resultCanonical []byte) string {
	if s.AsOfPtr == "" {
		return ""
	}
	dec := json.NewDecoder(bytes.NewReader(resultCanonical))
	dec.UseNumber()
	var doc any
	if err := dec.Decode(&doc); err != nil {
		return ""
	}
	return stringAt(doc, s.AsOfPtr)
}

// stringAt resolves ptr and returns the string value, or "" when the
// pointer is empty, absent, or non-string.
func stringAt(doc any, ptr string) string {
	if ptr == "" {
		return ""
	}
	node, err := resolvePtr(doc, ptr)
	if err != nil {
		return ""
	}
	s, _ := node.(string)
	return s
}
