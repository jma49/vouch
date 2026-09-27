// Package store implements the append-only receipt log: one signed DSSE
// envelope per line (JSONL), fsynced per append, immutable after write,
// and hash-chained: every entry's signed body names its position and the
// digest of the entry before it (#54). Lookup indexes are rebuilt from
// the log on open; the SQLite index used by the Python verifier is
// derived from the same file (docs/design.md section 12: the log is the
// source of truth, indexes are disposable).
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
	"time"

	"github.com/jma49/vouch/proxy/internal/receipt"
	"github.com/jma49/vouch/proxy/internal/sign"
)

// Log is an append-only, hash-chained receipt log backed by one JSONL
// file. Appends are serialized and fsynced; (session_id, turn_index)
// pairs are unique across the life of the log (replay protection, design
// section 3.1). The log checks the chain when it opens but never judges
// signatures: trust is the verifier's decision, made with its own
// keyring.
type Log struct {
	mu     sync.Mutex
	signer *sign.Signer
	f      logFile
	path   string
	size   int64                  // bytes of complete, acknowledged lines
	broken error                  // set when a failed append could not be rolled back
	seen   map[sessionTurn]string // -> receipt_id
	next   map[string]int         // session_id -> its next turn_index
	chain  chainState
}

// chainState is what the next entry links to.
type chainState struct {
	seq      int64  // the next entry's position
	head     string // digest of the last entry's payload, or receipt.Genesis
	receipts int64  // receipts so far (checkpoints excluded)
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

// Entry is one decoded log line: a receipt or a checkpoint, with the
// payload bytes its signature covers and the id of the key that signed
// it (set only by verified reads).
type Entry struct {
	Receipt    *receipt.Receipt
	Checkpoint *receipt.Checkpoint
	Payload    []byte
	KeyID      string
}

func (e *Entry) link() receipt.Link {
	if e.Checkpoint != nil {
		return e.Checkpoint.Link
	}
	return e.Receipt.Link
}

// Open opens (or creates) the receipt log at path, checks its chain, and
// rebuilds the uniqueness index. signer signs every appended entry; a
// log opened with a nil signer can be read but refuses appends.
func Open(path string, signer *sign.Signer) (*Log, error) {
	// Owner-only: receipts hold tool arguments and results, which can be
	// confidential (docs/threat-model.md, #99). Existing files and
	// directories keep their modes.
	if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
		return nil, fmt.Errorf("store: mkdir: %w", err)
	}
	f, err := os.OpenFile(path, os.O_CREATE|os.O_RDWR|os.O_APPEND, 0o600)
	if err != nil {
		return nil, fmt.Errorf("store: open: %w", err)
	}
	if err := lockFile(f); err != nil {
		f.Close()
		return nil, err
	}
	l := &Log{
		f: f, path: path, signer: signer,
		seen:  make(map[sessionTurn]string),
		next:  make(map[string]int),
		chain: chainState{head: receipt.Genesis},
	}
	if err := l.rebuild(); err != nil {
		f.Close()
		return nil, err
	}
	return l, nil
}

// rebuild indexes the existing log, checks its chain, and recovers from
// a crash during the last append. Append acknowledges an entry only
// after its whole line, newline included, is written and fsynced, so an
// unterminated final line was never acknowledged: when it does not parse
// it is residue of that crash and is truncated with a warning, and when
// it does parse only its newline is missing and is restored. Any other
// unparsable line, and any break in the chain, cannot come from a crash
// and stays a hard error.
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
			e, err := decodeEntry(body, nil)
			if err != nil {
				if terminated {
					return fmt.Errorf("store: %s line %d: %w", l.path, line, err)
				}
				if err := l.f.Truncate(off); err != nil {
					return fmt.Errorf("store: truncate partial line %d of %s: %w", line, l.path, err)
				}
				fmt.Fprintf(stderr, "store: warning: %s line %d: dropped %d bytes of an unterminated, unparsable entry left by an interrupted append (%v)\n",
					l.path, line, len(raw), err)
				break
			}
			if err := l.chain.accept(e); err != nil {
				return fmt.Errorf("store: %s line %d: %w", l.path, line, err)
			}
			if r := e.Receipt; r != nil {
				key := sessionTurn{r.SessionID, r.TurnIndex}
				if prev, dup := l.seen[key]; dup {
					return fmt.Errorf("store: %s line %d: duplicate (session_id=%s, turn_index=%d), first seen as receipt %s",
						l.path, line, r.SessionID, r.TurnIndex, prev)
				}
				l.index(r)
			}
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

// accept checks that e links to the current head and advances the chain.
func (c *chainState) accept(e *Entry) error {
	link := e.link()
	if link.Seq != c.seq {
		return fmt.Errorf("chain broken: entry has seq %d, want %d (an entry was removed, inserted, or reordered)",
			link.Seq, c.seq)
	}
	if link.PrevDigest != c.head {
		return fmt.Errorf("chain broken: entry %d names prev_digest %s, want %s", link.Seq, link.PrevDigest, c.head)
	}
	if cp := e.Checkpoint; cp != nil && cp.Receipts != c.receipts {
		return fmt.Errorf("checkpoint %d counts %d receipts, the log has %d", link.Seq, cp.Receipts, c.receipts)
	}
	c.seq++
	c.head = receipt.Digest(e.Payload)
	if e.Receipt != nil {
		c.receipts++
	}
	return nil
}

// index records r as seen and advances its session's next turn.
func (l *Log) index(r *receipt.Receipt) {
	l.seen[sessionTurn{r.SessionID, r.TurnIndex}] = r.ReceiptID
	if r.TurnIndex >= l.next[r.SessionID] {
		l.next[r.SessionID] = r.TurnIndex + 1
	}
}

// Append links, signs, appends, and fsyncs one receipt. It sets the
// receipt's Link. It rejects (session_id, turn_index) reuse.
func (l *Log) Append(r *receipt.Receipt) error {
	l.mu.Lock()
	defer l.mu.Unlock()
	return l.appendReceipt(r)
}

// AppendNextTurn is Append with the turn assigned by the log: the
// receipt gets its session's next turn_index, one past the highest the
// log holds for it. The proxy uses it, so a session reused after a
// restart continues where it stopped instead of colliding with its own
// earlier receipts (#69), and concurrent calls never race for a turn.
func (l *Log) AppendNextTurn(r *receipt.Receipt) error {
	l.mu.Lock()
	defer l.mu.Unlock()
	r.TurnIndex = l.next[r.SessionID]
	return l.appendReceipt(r)
}

func (l *Log) appendReceipt(r *receipt.Receipt) error {
	key := sessionTurn{r.SessionID, r.TurnIndex}
	if prev, dup := l.seen[key]; dup {
		return fmt.Errorf("store: duplicate (session_id=%s, turn_index=%d), first seen as receipt %s",
			r.SessionID, r.TurnIndex, prev)
	}
	r.Link = receipt.Link{Seq: l.chain.seq, PrevDigest: l.chain.head}
	body, err := r.Body()
	if err != nil {
		return fmt.Errorf("store: %w", err)
	}
	if err := l.appendEntry(receipt.PayloadType, body); err != nil {
		return err
	}
	l.chain.receipts++
	l.index(r)
	return nil
}

// Seal appends a checkpoint for session and returns the new head digest:
// the value to keep outside the log, since only an external copy can
// show that the log was later cut back to an earlier point.
func (l *Log) Seal(sessionID string, at time.Time) (string, error) {
	l.mu.Lock()
	defer l.mu.Unlock()
	cp := &receipt.Checkpoint{
		Link:      receipt.Link{Seq: l.chain.seq, PrevDigest: l.chain.head},
		Receipts:  l.chain.receipts,
		SessionID: sessionID,
		SealedAt:  at.UTC(),
	}
	body, err := cp.Body()
	if err != nil {
		return "", fmt.Errorf("store: %w", err)
	}
	if err := l.appendEntry(receipt.CheckpointType, body); err != nil {
		return "", err
	}
	return l.chain.head, nil
}

// Head returns the digest of the last entry, or receipt.Genesis.
func (l *Log) Head() string {
	l.mu.Lock()
	defer l.mu.Unlock()
	return l.chain.head
}

// appendEntry signs body and writes it as one fsynced line, advancing
// the chain on success. The caller holds l.mu.
func (l *Log) appendEntry(payloadType string, body []byte) error {
	if l.signer == nil {
		return fmt.Errorf("store: log %s was opened without a signing key", l.path)
	}
	if l.broken != nil {
		return l.broken
	}
	raw, err := json.Marshal(l.signer.Sign(payloadType, body))
	if err != nil {
		return fmt.Errorf("store: marshal envelope: %w", err)
	}
	line, err := receipt.Canonicalize(raw)
	if err != nil {
		return fmt.Errorf("store: canonicalize envelope: %w", err)
	}
	line = append(line, '\n')
	if _, err := l.f.Write(line); err != nil {
		return l.rollback(fmt.Errorf("store: append: %w", err))
	}
	if err := l.f.Sync(); err != nil {
		return l.rollback(fmt.Errorf("store: fsync: %w", err))
	}
	l.size += int64(len(line))
	l.chain.seq++
	l.chain.head = receipt.Digest(body)
	return nil
}

// rollback truncates the log back to its last acknowledged line after
// a failed append. Leaving a partial line would make the next append
// land on it, corrupting a line in the middle of the log. The failed
// entry was never acknowledged (the call fails, invariant 2), so
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

// decodeEntry reads one log line. With keys, it requires a valid
// signature from one of them before trusting the payload; without, it
// only decodes.
func decodeEntry(line []byte, keys sign.Keyring) (*Entry, error) {
	// Exactly the DSSE keys, in both languages: a line with "payload"
	// and "Payload" would otherwise be one log to Go and another to the
	// Python verifier (#98).
	if _, err := receipt.Canonicalize(line); err != nil {
		return nil, fmt.Errorf("not an envelope: %w", err)
	}
	if err := receipt.ExactKeys(line, "payload", "payloadType", "signatures"); err != nil {
		return nil, fmt.Errorf("not an envelope: %w", err)
	}
	var env sign.Envelope
	if err := json.Unmarshal(line, &env); err != nil {
		return nil, fmt.Errorf("not an envelope: %w", err)
	}
	var sigs []json.RawMessage
	if err := json.Unmarshal(extractRaw(line, "signatures"), &sigs); err != nil {
		return nil, fmt.Errorf("not an envelope: signatures: %w", err)
	}
	for i, s := range sigs {
		if err := receipt.ExactKeys(s, "keyid", "sig"); err != nil {
			return nil, fmt.Errorf("not an envelope: signature %d: %w", i, err)
		}
	}
	if env.PayloadType != receipt.PayloadType && env.PayloadType != receipt.CheckpointType {
		return nil, fmt.Errorf("unknown payload type %q", env.PayloadType)
	}
	var payload []byte
	var keyID string
	var err error
	if keys != nil {
		payload, keyID, err = sign.Open(env, env.PayloadType, keys)
	} else {
		payload, err = sign.Decode(env)
	}
	if err != nil {
		return nil, err
	}
	e := &Entry{Payload: payload, KeyID: keyID}
	if env.PayloadType == receipt.CheckpointType {
		var cp receipt.Checkpoint
		if err := receipt.DecodeStrict(payload, &cp); err != nil {
			return nil, fmt.Errorf("parse checkpoint: %w", err)
		}
		e.Checkpoint = &cp
		return e, nil
	}
	if e.Receipt, err = receipt.ParseBody(payload); err != nil {
		return nil, err
	}
	return e, nil
}

// Scan reads every receipt in the log at path, in append order, without
// verifying signatures or the chain: a reader for tooling and tests.
func Scan(path string) ([]receipt.Receipt, error) {
	var out []receipt.Receipt
	err := walk(path, nil, func(e *Entry) error {
		if e.Receipt != nil {
			out = append(out, *e.Receipt)
		}
		return nil
	})
	return out, err
}

// Audit is what a verified read of a log establishes.
type Audit struct {
	Receipts    []receipt.Receipt
	Checkpoints int
	Head        string // digest of the last entry, or receipt.Genesis
	Sealed      bool   // the last entry is a checkpoint
}

// Verify reads the log at path, requiring a valid signature from a key
// in keys on every entry and an unbroken chain. It is the Go counterpart
// of the Python verifier's load_log. Truncation of the tail cannot be
// seen from inside the log; callers compare Audit.Head with a head
// digest kept elsewhere, or require Sealed.
func Verify(path string, keys sign.Keyring) (*Audit, error) {
	if len(keys) == 0 {
		return nil, fmt.Errorf("store: no trusted keys to verify %s with", path)
	}
	a := &Audit{}
	chain := chainState{head: receipt.Genesis}
	ids := make(map[string]bool)
	turns := make(map[sessionTurn]bool)
	err := walk(path, keys, func(e *Entry) error {
		if err := chain.accept(e); err != nil {
			return err
		}
		if r := e.Receipt; r != nil {
			// The same checks the Python verifier makes (#99): a signed
			// receipt must still be consistent with itself and unique.
			if err := checkDigests(r); err != nil {
				return err
			}
			key := sessionTurn{r.SessionID, r.TurnIndex}
			switch {
			case ids[r.ReceiptID]:
				return fmt.Errorf("duplicate receipt_id %s", r.ReceiptID)
			case turns[key]:
				return fmt.Errorf("duplicate (session_id=%s, turn_index=%d)", r.SessionID, r.TurnIndex)
			}
			ids[r.ReceiptID], turns[key] = true, true
			a.Receipts = append(a.Receipts, *r)
		} else {
			a.Checkpoints++
		}
		a.Sealed = e.Checkpoint != nil
		return nil
	})
	if err != nil {
		return nil, err
	}
	a.Head = chain.head
	return a, nil
}

// checkDigests confirms a receipt's digests cover what they claim to.
func checkDigests(r *receipt.Receipt) error {
	if got := receipt.Digest(r.ResultCanonical); got != r.ResultDigest {
		return fmt.Errorf("receipt %s: result_digest %s does not match result_canonical (%s)", r.ReceiptID, r.ResultDigest, got)
	}
	// Every body carries response_canonical (null at worst), and Python
	// always checks it; so does Go.
	if got := receipt.Digest(r.ResponseCanonical); got != r.ResponseDigest {
		return fmt.Errorf("receipt %s: response_digest %s does not match response_canonical (%s)", r.ReceiptID, r.ResponseDigest, got)
	}
	return nil
}

// ScanVerified is Verify returning only the receipts.
func ScanVerified(path string, keys sign.Keyring) ([]receipt.Receipt, error) {
	a, err := Verify(path, keys)
	if err != nil {
		return nil, err
	}
	return a.Receipts, nil
}

// Walk visits every entry of the log at path in order, receipts and
// checkpoints alike. With keys, each entry's signature is verified
// first; the chain is not checked (use Verify).
func Walk(path string, keys sign.Keyring, visit func(*Entry) error) error {
	return walk(path, keys, visit)
}

func walk(path string, keys sign.Keyring, visit func(*Entry) error) error {
	f, err := os.Open(path)
	if err != nil {
		return fmt.Errorf("store: open: %w", err)
	}
	defer f.Close()
	br := bufio.NewReaderSize(f, 1<<20)
	for line := 1; ; line++ {
		raw, err := br.ReadBytes('\n')
		if err != nil && !errors.Is(err, io.EOF) {
			return fmt.Errorf("store: scan %s: %w", path, err)
		}
		if trimmed := bytes.TrimSpace(raw); len(trimmed) > 0 {
			e, derr := decodeEntry(trimmed, keys)
			if derr == nil {
				derr = visit(e)
			}
			if derr != nil {
				return fmt.Errorf("store: %s line %d: %w", path, line, derr)
			}
		}
		if err != nil {
			return nil
		}
	}
}

// extractRaw returns the raw value of key in a JSON object already
// checked by receipt.ExactKeys.
func extractRaw(obj []byte, key string) json.RawMessage {
	var m map[string]json.RawMessage
	_ = json.Unmarshal(obj, &m)
	return m[key]
}
