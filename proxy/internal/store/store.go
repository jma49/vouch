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
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"sync"

	"github.com/jma49/vouch/proxy/internal/receipt"
	"github.com/jma49/vouch/proxy/internal/sign"
)

// Log is an append-only receipt log backed by a single JSONL file, one
// signed DSSE envelope per line. Appends are serialized and fsynced;
// (session_id, turn_index) pairs are unique across the life of the log
// (replay protection, design section 3.1). The log reads envelopes
// structurally and never judges signatures: trust is the verifier's
// decision, made with its own keyring.
type Log struct {
	mu     sync.Mutex
	signer *sign.Signer
	f      logFile
	path   string
	size   int64                  // bytes of complete, acknowledged lines
	broken error                  // set when a failed append could not be rolled back
	seen   map[sessionTurn]string // -> receipt_id
}

// logFile is the slice of *os.File the log uses; tests substitute a
// failing implementation.
type logFile interface {
	io.ReadWriteSeeker
	Sync() error
	Truncate(size int64) error
	Close() error
}

// stderr receives crash-recovery warnings; a variable so tests can
// capture them.
var stderr io.Writer = os.Stderr

type sessionTurn struct {
	session string
	turn    int
}

// Open opens (or creates) the receipt log at path and rebuilds the
// uniqueness index from existing entries. signer signs every appended
// receipt; a log opened with a nil signer can be read but refuses
// appends.
func Open(path string, signer *sign.Signer) (*Log, error) {
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		return nil, fmt.Errorf("store: mkdir: %w", err)
	}
	f, err := os.OpenFile(path, os.O_CREATE|os.O_RDWR|os.O_APPEND, 0o644)
	if err != nil {
		return nil, fmt.Errorf("store: open: %w", err)
	}
	l := &Log{f: f, path: path, signer: signer, seen: make(map[sessionTurn]string)}
	if err := l.rebuild(); err != nil {
		f.Close()
		return nil, err
	}
	return l, nil
}

// rebuild indexes the existing log and recovers from a crash during
// the last append. Append acknowledges a receipt only after its whole
// line, newline included, is written and fsynced, so an unterminated
// final line was never acknowledged: when it does not parse it is
// residue of that crash and is truncated with a warning, and when it
// does parse only its newline is missing and is restored. Any other
// unparsable line cannot come from a crash and stays a hard error.
func (l *Log) rebuild() error {
	if _, err := l.f.Seek(0, io.SeekStart); err != nil {
		return fmt.Errorf("store: seek: %w", err)
	}
	br := bufio.NewReaderSize(l.f, 1<<20)
	var off int64
	for line := 1; ; line++ {
		raw, err := br.ReadBytes('\n')
		if err != nil && !errors.Is(err, io.EOF) {
			return fmt.Errorf("store: read %s: %w", l.path, err)
		}
		if len(raw) == 0 {
			break
		}
		terminated := raw[len(raw)-1] == '\n'
		if body := bytes.TrimSpace(raw); len(body) > 0 {
			r, err := decodeLine(body)
			if err != nil {
				if terminated {
					return fmt.Errorf("store: %s line %d: %w", l.path, line, err)
				}
				if err := l.f.Truncate(off); err != nil {
					return fmt.Errorf("store: truncate partial line %d of %s: %w", line, l.path, err)
				}
				fmt.Fprintf(stderr, "store: warning: %s line %d: dropped %d bytes of an unterminated, unparsable receipt left by an interrupted append (%v)\n",
					l.path, line, len(raw), err)
				break
			}
			key := sessionTurn{r.SessionID, r.TurnIndex}
			if prev, dup := l.seen[key]; dup {
				return fmt.Errorf("store: %s line %d: duplicate (session_id=%s, turn_index=%d), first seen as receipt %s",
					l.path, line, r.SessionID, r.TurnIndex, prev)
			}
			l.seen[key] = r.ReceiptID
			if !terminated {
				if _, err := l.f.Write([]byte{'\n'}); err != nil {
					return fmt.Errorf("store: terminate line %d of %s: %w", line, l.path, err)
				}
				if err := l.f.Sync(); err != nil {
					return fmt.Errorf("store: fsync: %w", err)
				}
				off++
			}
		}
		off += int64(len(raw))
		if !terminated {
			break
		}
	}
	l.size = off
	return nil
}

// Append signs one receipt, then appends and fsyncs its envelope as a
// single line. It rejects (session_id, turn_index) reuse.
func (l *Log) Append(r *receipt.Receipt) error {
	if l.signer == nil {
		return fmt.Errorf("store: log %s was opened without a signing key", l.path)
	}
	body, err := r.Body()
	if err != nil {
		return fmt.Errorf("store: %w", err)
	}
	raw, err := json.Marshal(l.signer.Sign(receipt.PayloadType, body))
	if err != nil {
		return fmt.Errorf("store: marshal envelope for %s: %w", r.ReceiptID, err)
	}
	line, err := receipt.Canonicalize(raw)
	if err != nil {
		return fmt.Errorf("store: canonicalize envelope for %s: %w", r.ReceiptID, err)
	}

	l.mu.Lock()
	defer l.mu.Unlock()
	key := sessionTurn{r.SessionID, r.TurnIndex}
	if prev, dup := l.seen[key]; dup {
		return fmt.Errorf("store: duplicate (session_id=%s, turn_index=%d), first seen as receipt %s",
			r.SessionID, r.TurnIndex, prev)
	}
	if l.broken != nil {
		return l.broken
	}
	line = append(line, '\n')
	if _, err := l.f.Write(line); err != nil {
		return l.rollback(fmt.Errorf("store: append: %w", err))
	}
	if err := l.f.Sync(); err != nil {
		return l.rollback(fmt.Errorf("store: fsync: %w", err))
	}
	l.size += int64(len(line))
	l.seen[key] = r.ReceiptID
	return nil
}

// rollback truncates the log back to its last acknowledged line after
// a failed append. Leaving a partial line would make the next append
// land on it, corrupting a line in the middle of the log. The failed
// receipt was never acknowledged (the call fails, invariant 2), so
// removing it loses nothing. If the truncate fails too, the log can no
// longer be appended to safely and refuses further appends.
func (l *Log) rollback(cause error) error {
	if err := l.f.Truncate(l.size); err != nil {
		l.broken = fmt.Errorf("store: %s unusable after failed append: %w (rollback: %v)", l.path, cause, err)
		return l.broken
	}
	return cause
}

// Close closes the underlying file.
func (l *Log) Close() error {
	return l.f.Close()
}

// decodeLine reads one log line: a DSSE envelope whose payload is a
// receipt body. The signature is not checked (see Log).
func decodeLine(line []byte) (*receipt.Receipt, error) {
	var env sign.Envelope
	if err := json.Unmarshal(line, &env); err != nil {
		return nil, fmt.Errorf("not an envelope: %w", err)
	}
	if env.PayloadType != receipt.PayloadType {
		return nil, fmt.Errorf("payload type %q, want %q", env.PayloadType, receipt.PayloadType)
	}
	body, err := sign.Decode(env)
	if err != nil {
		return nil, err
	}
	return receipt.ParseBody(body)
}

// Scan reads every receipt in the log at path, in append order,
// without verifying signatures.
func Scan(path string) ([]receipt.Receipt, error) {
	return scan(path, nil)
}

// ScanVerified reads every receipt in the log at path and requires each
// envelope to carry a valid signature from a key in keys. It is the Go
// counterpart of the Python verifier's load_log.
func ScanVerified(path string, keys sign.Keyring) ([]receipt.Receipt, error) {
	if len(keys) == 0 {
		return nil, fmt.Errorf("store: no trusted keys to verify %s with", path)
	}
	return scan(path, keys)
}

func scan(path string, keys sign.Keyring) ([]receipt.Receipt, error) {
	f, err := os.Open(path)
	if err != nil {
		return nil, fmt.Errorf("store: open: %w", err)
	}
	defer f.Close()

	var out []receipt.Receipt
	br := bufio.NewReaderSize(f, 1<<20)
	for line := 1; ; line++ {
		raw, err := br.ReadBytes('\n')
		if err != nil && !errors.Is(err, io.EOF) {
			return nil, fmt.Errorf("store: scan %s: %w", path, err)
		}
		if trimmed := bytes.TrimSpace(raw); len(trimmed) > 0 {
			r, err := readLine(trimmed, keys)
			if err != nil {
				return nil, fmt.Errorf("store: %s line %d: %w", path, line, err)
			}
			out = append(out, *r)
		}
		if err != nil {
			return out, nil
		}
	}
}

// readLine decodes one log line and, when keys is non-nil, requires a
// valid signature from one of them.
func readLine(line []byte, keys sign.Keyring) (*receipt.Receipt, error) {
	if keys == nil {
		return decodeLine(line)
	}
	var env sign.Envelope
	if err := json.Unmarshal(line, &env); err != nil {
		return nil, fmt.Errorf("not an envelope: %w", err)
	}
	body, _, err := sign.Open(env, receipt.PayloadType, keys)
	if err != nil {
		return nil, err
	}
	return receipt.ParseBody(body)
}
