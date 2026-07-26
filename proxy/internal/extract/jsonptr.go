package extract

import (
	"fmt"
	"strconv"
	"strings"
)

// resolvePtr resolves an RFC 6901 JSON pointer against a decoded JSON
// value. An empty pointer returns the document itself.
func resolvePtr(doc any, ptr string) (any, error) {
	if ptr == "" {
		return doc, nil
	}
	if !strings.HasPrefix(ptr, "/") {
		return nil, fmt.Errorf("json pointer %q: must start with /", ptr)
	}
	cur := doc
	for _, tok := range strings.Split(ptr[1:], "/") {
		tok = strings.ReplaceAll(strings.ReplaceAll(tok, "~1", "/"), "~0", "~")
		switch node := cur.(type) {
		case map[string]any:
			v, ok := node[tok]
			if !ok {
				return nil, fmt.Errorf("json pointer %q: key %q not found", ptr, tok)
			}
			cur = v
		case []any:
			i, err := strconv.Atoi(tok)
			if err != nil || i < 0 || i >= len(node) {
				return nil, fmt.Errorf("json pointer %q: bad array index %q", ptr, tok)
			}
			cur = node[i]
		default:
			return nil, fmt.Errorf("json pointer %q: cannot descend into %T at %q", ptr, cur, tok)
		}
	}
	return cur, nil
}
