package receipt

import (
	"os"
	"path/filepath"
	"reflect"
	"regexp"
	"sort"
	"strings"
	"testing"
)

// specTable reads one field table of docs/receipt-format.md: the rows
// under the heading, split into required and optional keys.
func specTable(t *testing.T, spec, heading string) (required, optional []string) {
	t.Helper()
	start := strings.Index(spec, "\n"+heading+"\n")
	if start < 0 {
		t.Fatalf("spec has no %q section", heading)
	}
	section := spec[start+len(heading)+2:]
	if end := strings.Index(section, "\n#"); end >= 0 {
		section = section[:end]
	}
	row := regexp.MustCompile("^\\| `([a-z_]+)` \\| [^|]+ \\| (yes|no) \\|")
	for _, line := range strings.Split(section, "\n") {
		if m := row.FindStringSubmatch(line); m != nil {
			if m[2] == "yes" {
				required = append(required, m[1])
			} else {
				optional = append(optional, m[1])
			}
		}
	}
	sort.Strings(required)
	sort.Strings(optional)
	return required, optional
}

// TestSpecMatchesTheCode holds docs/receipt-format.md to the structs
// that write and read receipts: every key, whether it is required, and
// the payload types. The Python verifier has the same test.
func TestSpecMatchesTheCode(t *testing.T) {
	raw, err := os.ReadFile(filepath.Join("..", "..", "..", "docs", "receipt-format.md"))
	if err != nil {
		t.Fatal(err)
	}
	spec := string(raw)
	for _, c := range []struct {
		heading string
		v       any
	}{
		{"### Receipt body", Receipt{}},
		{"### Fact", Fact{}},
		{"### Checkpoint body", Checkpoint{}},
	} {
		wantReq, wantOpt := Keys(c.v)
		gotReq, gotOpt := specTable(t, spec, c.heading)
		if !reflect.DeepEqual(gotReq, wantReq) || !reflect.DeepEqual(gotOpt, wantOpt) {
			t.Errorf("%s: spec has required %v optional %v; code has required %v optional %v",
				c.heading, gotReq, gotOpt, wantReq, wantOpt)
		}
	}
	for _, typ := range []string{PayloadType, CheckpointType, Genesis} {
		if !strings.Contains(spec, typ) {
			t.Errorf("spec does not name %q", typ)
		}
	}
}

func TestRequirePresent(t *testing.T) {
	cases := []struct {
		name, body string
		v          any
		want       string
	}{
		{"checkpoint complete", `{"prev_digest":"x","receipts":1,"sealed_at":"t","seq":1,"session_id":"s"}`, Checkpoint{}, ""},
		{"checkpoint without sealed_at", `{"prev_digest":"x","receipts":1,"seq":1,"session_id":"s"}`, Checkpoint{}, `"sealed_at"`},
		{"fact without json_ptr", `{"entity":"A","metric":"m","tol_class":"price","value":1}`, Fact{}, `"json_ptr"`},
		{"fact without optional unit", `{"entity":"A","json_ptr":"/x","metric":"m","tol_class":"price","value":1}`, Fact{}, ""},
	}
	for _, c := range cases {
		err := RequirePresent([]byte(c.body), c.v)
		switch {
		case c.want == "" && err != nil:
			t.Errorf("%s: %v", c.name, err)
		case c.want != "" && (err == nil || !strings.Contains(err.Error(), c.want)):
			t.Errorf("%s: error %v, want one naming %s", c.name, err, c.want)
		}
	}
}
