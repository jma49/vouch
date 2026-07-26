package extract

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

const indicatorsSchema = `
tool: get_indicators
entity_ptr: /symbol
asof_ptr: /as_of
facts:
  - ptr: /rsi_14
    metric: rsi_14
    tol_class: indicator
  - ptr: /macd/histogram
    metric: macd_hist
    tol_class: indicator
  - ptr: /close
    metric: close_price
    unit: USD
    tol_class: price
`

const ohlcvSchema = `
tool: get_ohlcv
entity_ptr: /symbol
facts:
  - ptr: /bars
    each:
      - ptr: /close
        metric: close_price
        unit: USD
        tol_class: price
        asof_ptr: /t
      - ptr: /volume
        metric: volume
        tol_class: count
        asof_ptr: /t
`

func writeSchema(t *testing.T, name, body string) string {
	t.Helper()
	path := filepath.Join(t.TempDir(), name)
	if err := os.WriteFile(path, []byte(body), 0o644); err != nil {
		t.Fatal(err)
	}
	return path
}

func TestExtractFlat(t *testing.T) {
	s, err := LoadSchema(writeSchema(t, "get_indicators.yaml", indicatorsSchema))
	if err != nil {
		t.Fatal(err)
	}
	result := []byte(`{"symbol":"NVDA","as_of":"2026-07-24T20:00:00Z","rsi_14":62.3,"macd":{"histogram":-0.42},"close":181.52}`)
	facts, err := s.Extract(result)
	if err != nil {
		t.Fatal(err)
	}
	if len(facts) != 3 {
		t.Fatalf("got %d facts, want 3: %+v", len(facts), facts)
	}
	f := facts[0]
	if f.Entity != "NVDA" || f.Metric != "rsi_14" || f.Value != 62.3 || f.JSONPtr != "/rsi_14" {
		t.Fatalf("rsi fact wrong: %+v", f)
	}
	if f.AsOf != "2026-07-24T20:00:00Z" || f.TolClass != "indicator" {
		t.Fatalf("rsi fact metadata wrong: %+v", f)
	}
	if facts[1].Value != -0.42 || facts[1].JSONPtr != "/macd/histogram" {
		t.Fatalf("nested fact wrong: %+v", facts[1])
	}
	if facts[2].Unit != "USD" {
		t.Fatalf("unit lost: %+v", facts[2])
	}
}

func TestExtractSkipsAbsentFields(t *testing.T) {
	s, err := LoadSchema(writeSchema(t, "get_indicators.yaml", indicatorsSchema))
	if err != nil {
		t.Fatal(err)
	}
	facts, err := s.Extract([]byte(`{"symbol":"NVDA","rsi_14":62.3}`))
	if err != nil {
		t.Fatal(err)
	}
	if len(facts) != 1 || facts[0].Metric != "rsi_14" {
		t.Fatalf("got %+v, want only rsi_14", facts)
	}
}

func TestExtractRejectsNonNumeric(t *testing.T) {
	s, err := LoadSchema(writeSchema(t, "get_indicators.yaml", indicatorsSchema))
	if err != nil {
		t.Fatal(err)
	}
	_, err = s.Extract([]byte(`{"symbol":"NVDA","rsi_14":"62.3"}`))
	if err == nil || !strings.Contains(err.Error(), "expected number") {
		t.Fatalf("got %v, want type error", err)
	}
}

func TestExtractEach(t *testing.T) {
	s, err := LoadSchema(writeSchema(t, "get_ohlcv.yaml", ohlcvSchema))
	if err != nil {
		t.Fatal(err)
	}
	result := []byte(`{"symbol":"NVDA","bars":[
		{"t":"2026-07-23T20:00:00Z","close":179.10,"volume":1000},
		{"t":"2026-07-24T20:00:00Z","close":181.52,"volume":1200}]}`)
	facts, err := s.Extract(result)
	if err != nil {
		t.Fatal(err)
	}
	if len(facts) != 4 {
		t.Fatalf("got %d facts, want 4: %+v", len(facts), facts)
	}
	if facts[0].JSONPtr != "/bars/0/close" || facts[0].AsOf != "2026-07-23T20:00:00Z" {
		t.Fatalf("first bar fact wrong: %+v", facts[0])
	}
	if facts[3].JSONPtr != "/bars/1/volume" || facts[3].Value != 1200 || facts[3].AsOf != "2026-07-24T20:00:00Z" {
		t.Fatalf("last bar fact wrong: %+v", facts[3])
	}
}

func TestLoadDirSkipsExamples(t *testing.T) {
	dir := t.TempDir()
	if err := os.WriteFile(filepath.Join(dir, "get_indicators.yaml"), []byte(indicatorsSchema), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, "get_quote.example.yaml"), []byte("not even yaml: ["), 0o644); err != nil {
		t.Fatal(err)
	}
	schemas, err := LoadDir(dir)
	if err != nil {
		t.Fatal(err)
	}
	if len(schemas) != 1 || schemas["get_indicators"] == nil {
		t.Fatalf("got %v, want just get_indicators", schemas)
	}
}

func TestValidateRejectsUnknownFields(t *testing.T) {
	_, err := LoadSchema(writeSchema(t, "bad.yaml", "tool: t\nfacts:\n  - ptr: /x\n    metric: m\n    tol_class: c\n    typo_field: v\n"))
	if err == nil {
		t.Fatal("want error on unknown schema field, got nil")
	}
}

func TestValidateRequiresTolClass(t *testing.T) {
	_, err := LoadSchema(writeSchema(t, "bad.yaml", "tool: t\nfacts:\n  - ptr: /x\n    metric: m\n"))
	if err == nil || !strings.Contains(err.Error(), "tol_class") {
		t.Fatalf("got %v, want tol_class error", err)
	}
}

func TestJSONPointerEscapes(t *testing.T) {
	doc := map[string]any{"a/b": map[string]any{"c~d": "hit"}}
	v, err := resolvePtr(doc, "/a~1b/c~0d")
	if err != nil || v != "hit" {
		t.Fatalf("got %v, %v; want hit", v, err)
	}
}

func TestLoadRepoSchemas(t *testing.T) {
	schemas, err := LoadDir("../../../schemas")
	if err != nil {
		t.Fatal(err)
	}
	for _, tool := range []string{"get_indicators", "get_quote", "get_ohlcv"} {
		if schemas[tool] == nil {
			t.Fatalf("repo schema for %s missing or failed to load", tool)
		}
	}
}
