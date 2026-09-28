package receipt

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"time"
)

// Fact is a verifiable atom extracted from a tool result at receipt-write
// time via schema-driven extraction (see docs/design.md section 4).
type Fact struct {
	Entity    string  `json:"entity"`
	Metric    string  `json:"metric"`
	Value     float64 `json:"value"`
	Unit      string  `json:"unit,omitempty"`
	AsOf      string  `json:"as_of,omitempty"`
	Timeframe string  `json:"timeframe,omitempty"`
	JSONPtr   string  `json:"json_ptr"`
	TolClass  string  `json:"tol_class"`
}

// Receipt records one tool call: the canonicalized request, the payload
// facts were extracted from, the whole response the agent received, and
// the facts themselves. On disk each receipt is the payload of a signed
// DSSE envelope (package sign); Body gives the exact bytes signed.
//
// Threat model note: in a single-process deployment the LLM cannot write to
// our storage, so the signature is not protecting against the model. It
// provides (a) tamper-evidence once receipts cross process or machine
// boundaries, (b) third-party verification with only the public key, and
// (c) replay protection via (SessionID, TurnIndex) uniqueness.
type Receipt struct {
	Link
	ReceiptID       string          `json:"receipt_id"`
	SessionID       string          `json:"session_id"`
	TurnIndex       int             `json:"turn_index"`
	ToolName        string          `json:"tool_name"`
	ArgsCanonical   json.RawMessage `json:"args_canonical"`
	ResultCanonical json.RawMessage `json:"result_canonical"`
	ResultDigest    string          `json:"result_digest"`
	// PayloadSource says where in the response result_canonical was
	// taken from: "structuredContent", "content/<i>/text", or "result".
	PayloadSource string `json:"payload_source"`
	// ResponseCanonical is the whole tools/call result exactly as the
	// agent received it. Facts come from the payload, but the signature
	// must cover what the model actually read, which can differ (a text
	// block beside structured content, extra blocks) (#20).
	ResponseCanonical json.RawMessage `json:"response_canonical"`
	ResponseDigest    string          `json:"response_digest"`
	Facts             []Fact          `json:"facts"`
	DataAsOf          string          `json:"data_asof,omitempty"`
	WallTime          time.Time       `json:"wall_time"`
	LogicalTime       int64           `json:"logical_time"`
	UpstreamLatencyMS int64           `json:"upstream_latency_ms"`
}

// PayloadType identifies a receipt body inside a DSSE envelope. The
// version changes whenever the body's fields or the canonical JSON
// rules change (docs/canonical-json.md, "Versioning"). Version 3 added
// the chain link; version 4 is canonical JSON v2 (nesting limit, #62).
const PayloadType = "application/vnd.vouch.receipt+json; version=4"

// CheckpointType identifies a checkpoint body. A distinct type means a
// signature over a receipt can never be replayed as a checkpoint, since
// DSSE's pre-authentication encoding binds the type.
const CheckpointType = "application/vnd.vouch.checkpoint+json; version=2"

// Genesis is the prev_digest of the first entry in a log.
const Genesis = "sha256:0000000000000000000000000000000000000000000000000000000000000000"

// Link chains every log entry, receipt or checkpoint, to the one before
// it (#54). Seq is the entry's position in the log, from 0; PrevDigest
// is the digest of the previous entry's payload bytes, or Genesis. Both
// are inside the signed body, so deleting, reordering, or inserting an
// entry breaks the chain for everything after it.
type Link struct {
	Seq        int64  `json:"seq"`
	PrevDigest string `json:"prev_digest"`
}

// Checkpoint seals a log at a point: how many receipts precede it and,
// through its Link, the digest of the entry before it. The proxy
// appends one when a session ends cleanly. A log that ends in a
// checkpoint was not cut short after that session; whether it was cut
// back to an earlier checkpoint can only be told against a head digest
// kept outside the log (docs/threat-model.md).
type Checkpoint struct {
	Link
	Receipts  int64     `json:"receipts"`
	SessionID string    `json:"session_id"`
	SealedAt  time.Time `json:"sealed_at"`
}

// Body returns the canonical JSON bytes of the checkpoint.
func (c *Checkpoint) Body() ([]byte, error) {
	raw, err := json.Marshal(c)
	if err != nil {
		return nil, fmt.Errorf("receipt: marshal checkpoint: %w", err)
	}
	return Canonicalize(raw)
}

// Digest returns "sha256:<hex>" over canonical bytes.
func Digest(canonical []byte) string {
	sum := sha256.Sum256(canonical)
	return "sha256:" + hex.EncodeToString(sum[:])
}

// Body returns the canonical JSON bytes of the receipt: the payload of
// its envelope, and therefore the exact bytes its signature covers.
func (r *Receipt) Body() ([]byte, error) {
	raw, err := json.Marshal(r)
	if err != nil {
		return nil, fmt.Errorf("receipt: marshal %s: %w", r.ReceiptID, err)
	}
	return Canonicalize(raw)
}

// ParseBody decodes an envelope payload into a receipt.
func ParseBody(body []byte) (*Receipt, error) {
	var r Receipt
	// Strict, so Go reads a body the way the Python verifier does (#98).
	if err := DecodeStrict(body, &r); err != nil {
		return nil, fmt.Errorf("receipt: parse body: %w", err)
	}
	if err := RequirePresent(body, r); err != nil {
		return nil, fmt.Errorf("receipt: parse body: %w", err)
	}
	return &r, nil
}
