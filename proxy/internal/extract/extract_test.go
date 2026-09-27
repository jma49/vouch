package extract

import (
	"fmt"
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
	for _, v := range []string{`"62.3"`, `true`, `{"v":62.3}`, `[62.3]`} {
		_, err = s.Extract([]byte(`{"symbol":"NVDA","rsi_14":` + v + `}`))
		if err == nil || !strings.Contains(err.Error(), "expected number") {
			t.Fatalf("rsi_14=%s: got %v, want type error", v, err)
		}
	}
}

// TestExtractSkipsNullAndOutOfRange pins that "no value" and "no
// representable value" produce no fact rather than failing the call:
// a missing fact can only tighten verification (UNSUPPORTED), while a
// failed call hides the result from the agent entirely.
func TestExtractSkipsNullAndOutOfRange(t *testing.T) {
	indicators, err := LoadSchema(writeSchema(t, "get_indicators.yaml", indicatorsSchema))
	if err != nil {
		t.Fatal(err)
	}
	ohlcv, err := LoadSchema(writeSchema(t, "get_ohlcv.yaml", ohlcvSchema))
	if err != nil {
		t.Fatal(err)
	}
	cases := []struct {
		name    string
		schema  *Schema
		result  string
		metrics []string
	}{
		{"null field", indicators, `{"symbol":"NVDA","rsi_14":null,"close":181.52}`, []string{"close_price"}},
		{"null parent", indicators, `{"symbol":"NVDA","macd":null,"rsi_14":62.3}`, []string{"rsi_14"}},
		{"overflow", indicators, `{"symbol":"NVDA","rsi_14":1e400,"close":-1e400}`, nil},
		{"null array", ohlcv, `{"symbol":"NVDA","bars":null}`, nil},
		{"null element field", ohlcv, `{"symbol":"NVDA","bars":[{"t":"x","close":null,"volume":1200}]}`, []string{"volume"}},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			facts, err := tc.schema.Extract([]byte(tc.result))
			if err != nil {
				t.Fatalf("Extract: %v", err)
			}
			var got []string
			for _, f := range facts {
				got = append(got, f.Metric)
			}
			if strings.Join(got, ",") != strings.Join(tc.metrics, ",") {
				t.Fatalf("metrics %v, want %v", got, tc.metrics)
			}
		})
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

// TestExtractEachEntity pins per-element entities (#88): each row of a
// query result names its own entity, and a row without one gets none
// rather than borrowing another's.
func TestExtractEachEntity(t *testing.T) {
	s, err := LoadSchema(writeSchema(t, "run_sql.yaml", `
tool: run_sql
asof_ptr: /as_of
facts:
  - ptr: /rows
    entity_ptr: /region
    each:
      - ptr: /revenue
        metric: revenue
        unit: USD
        tol_class: price
`))
	if err != nil {
		t.Fatal(err)
	}
	facts, err := s.Extract([]byte(`{"as_of":"2026-06-30","rows":[
		{"region":"EMEA","revenue":1200.5},{"region":"APAC","revenue":900},{"revenue":10}]}`))
	if err != nil {
		t.Fatal(err)
	}
	got := []string{}
	for _, f := range facts {
		got = append(got, fmt.Sprintf("%s=%g@%s", f.Entity, f.Value, f.AsOf))
	}
	if strings.Join(got, " ") != "EMEA=1200.5@2026-06-30 APAC=900@2026-06-30 =10@2026-06-30" {
		t.Fatalf("got %v", got)
	}
	if _, err := LoadSchema(writeSchema(t, "bad.yaml", "tool: x\nfacts:\n  - ptr: /a\n    entity_ptr: /e\n    metric: m\n    tol_class: price\n")); err == nil ||
		!strings.Contains(err.Error(), "only to an each") {
		t.Fatalf("entity_ptr without each: %v", err)
	}
}

// TestLoadExamplePacks keeps the second domain's schemas loadable
// (examples/analytics, #88).
func TestLoadExamplePacks(t *testing.T) {
	schemas, err := LoadDir(filepath.Join("..", "..", "..", "examples", "analytics", "schemas"))
	if err != nil {
		t.Fatal(err)
	}
	if s := schemas["run_sql"]; s == nil || s.Facts[0].EntityPtr != "/region" {
		t.Fatalf("run_sql schema: %+v", schemas)
	}
}
