// Package store implements the append-only receipt log: one canonical
// JSON receipt per line (JSONL), fsynced per append, immutable after
// write. Lookup indexes are rebuilt from the log on open; the SQLite
// index used by the Python verifier is derived from the same file (see
// docs/design.md section 12 — the log is the source of truth, indexes
// are disposable).
package store

import (
	"bufio"
	"bytes"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"sync"

	"github.com/jma49/vouch/proxy/internal/receipt"
)

// Log is an append-only receipt log backed by a single JSONL file.
// Appends are serialized and fsynced; (session_id, turn_index) pairs
// are unique across the life of the log (replay protection, design
// section 3.1).
type Log struct {
	mu   sync.Mutex
	f    *os.File
	path string
	seen map[sessionTurn]string // -> receipt_id
}

type sessionTurn struct {
	session string
	turn    int
}

// Open opens (or creates) the receipt log at path and rebuilds the
// uniqueness index from existing entries.
func Open(path string) (*Log, error) {
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		return nil, fmt.Errorf("store: mkdir: %w", err)
	}
	f, err := os.OpenFile(path, os.O_CREATE|os.O_RDWR|os.O_APPEND, 0o644)
	if err != nil {
		return nil, fmt.Errorf("store: open: %w", err)
	}
	l := &Log{f: f, path: path, seen: make(map[sessionTurn]string)}
	if err := l.rebuild(); err != nil {
		f.Close()
		return nil, err
	}
	return l, nil
}

func (l *Log) rebuild() error {
	if _, err := l.f.Seek(0, 0); err != nil {
		return fmt.Errorf("store: seek: %w", err)
	}
	sc := bufio.NewScanner(l.f)
	sc.Buffer(make([]byte, 0, 1<<20), 16<<20)
	line := 0
	for sc.Scan() {
		line++
		raw := bytes.TrimSpace(sc.Bytes())
		if len(raw) == 0 {
			continue
		}
		var r receipt.Receipt
		if err := json.Unmarshal(raw, &r); err != nil {
			return fmt.Errorf("store: %s line %d: %w", l.path, line, err)
		}
		key := sessionTurn{r.SessionID, r.TurnIndex}
		if prev, dup := l.seen[key]; dup {
			return fmt.Errorf("store: %s line %d: duplicate (session_id=%s, turn_index=%d), first seen as receipt %s",
				l.path, line, r.SessionID, r.TurnIndex, prev)
		}
		l.seen[key] = r.ReceiptID
	}
	if err := sc.Err(); err != nil {
		return fmt.Errorf("store: scan %s: %w", l.path, err)
	}
	return nil
}

// Append canonicalizes, appends, and fsyncs one signed receipt.
// It rejects unsigned receipts and (session_id, turn_index) reuse.
func (l *Log) Append(r *receipt.Receipt) error {
	if r.Sig == "" {
		return fmt.Errorf("store: refusing to append unsigned receipt %s", r.ReceiptID)
	}
	raw, err := json.Marshal(r)
	if err != nil {
		return fmt.Errorf("store: marshal receipt %s: %w", r.ReceiptID, err)
	}
	line, err := receipt.Canonicalize(raw)
	if err != nil {
		return fmt.Errorf("store: canonicalize receipt %s: %w", r.ReceiptID, err)
	}

	l.mu.Lock()
	defer l.mu.Unlock()
	key := sessionTurn{r.SessionID, r.TurnIndex}
	if prev, dup := l.seen[key]; dup {
		return fmt.Errorf("store: duplicate (session_id=%s, turn_index=%d), first seen as receipt %s",
			r.SessionID, r.TurnIndex, prev)
	}
	if _, err := l.f.Write(append(line, '\n')); err != nil {
		return fmt.Errorf("store: append: %w", err)
	}
	if err := l.f.Sync(); err != nil {
		return fmt.Errorf("store: fsync: %w", err)
	}
	l.seen[key] = r.ReceiptID
	return nil
}

// Close closes the underlying file.
func (l *Log) Close() error {
	return l.f.Close()
}

// Scan reads every receipt in the log at path, in append order.
func Scan(path string) ([]receipt.Receipt, error) {
	f, err := os.Open(path)
	if err != nil {
		return nil, fmt.Errorf("store: open: %w", err)
	}
	defer f.Close()

	var out []receipt.Receipt
	sc := bufio.NewScanner(f)
	sc.Buffer(make([]byte, 0, 1<<20), 16<<20)
	line := 0
	for sc.Scan() {
		line++
		raw := bytes.TrimSpace(sc.Bytes())
		if len(raw) == 0 {
			continue
		}
		var r receipt.Receipt
		if err := json.Unmarshal(raw, &r); err != nil {
			return nil, fmt.Errorf("store: %s line %d: %w", path, line, err)
		}
		out = append(out, r)
	}
	if err := sc.Err(); err != nil {
		return nil, fmt.Errorf("store: scan %s: %w", path, err)
	}
	return out, nil
}
