package receipt

import (
	"crypto/hmac"
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

// Receipt records one tool call: the canonicalized request/response pair,
// the facts extracted from the response, and an HMAC signature over the
// whole record.
//
// Threat model note: in a single-process deployment the LLM cannot write to
// our storage, so the signature is not protecting against the model. It
// provides (a) tamper-evidence once receipts cross process or machine
// boundaries, (b) third-party re-verifiability of eval results, and
// (c) replay protection via (SessionID, TurnIndex) uniqueness.
type Receipt struct {
	ReceiptID         string          `json:"receipt_id"`
	SessionID         string          `json:"session_id"`
	TurnIndex         int             `json:"turn_index"`
	ToolName          string          `json:"tool_name"`
	ArgsCanonical     json.RawMessage `json:"args_canonical"`
	ResultCanonical   json.RawMessage `json:"result_canonical"`
	ResultDigest      string          `json:"result_digest"`
	Facts             []Fact          `json:"facts"`
	DataAsOf          string          `json:"data_asof,omitempty"`
	WallTime          time.Time       `json:"wall_time"`
	LogicalTime       int64           `json:"logical_time"`
	UpstreamLatencyMS int64           `json:"upstream_latency_ms"`
	Sig               string          `json:"sig,omitempty"`
}

// Digest returns "sha256:<hex>" over canonical bytes.
func Digest(canonical []byte) string {
	sum := sha256.Sum256(canonical)
	return "sha256:" + hex.EncodeToString(sum[:])
}

// signingPayload returns the canonical bytes of the receipt with the Sig
// field cleared, which is the exact byte sequence the HMAC covers.
func (r *Receipt) signingPayload() ([]byte, error) {
	unsigned := *r
	unsigned.Sig = ""
	raw, err := json.Marshal(&unsigned)
	if err != nil {
		return nil, fmt.Errorf("receipt: marshal for signing: %w", err)
	}
	return Canonicalize(raw)
}

// Sign computes the HMAC-SHA256 signature over the canonical form of the
// receipt (excluding Sig) and stores it on the receipt.
func (r *Receipt) Sign(key []byte) error {
	payload, err := r.signingPayload()
	if err != nil {
		return err
	}
	mac := hmac.New(sha256.New, key)
	mac.Write(payload)
	r.Sig = "hmac-sha256:" + hex.EncodeToString(mac.Sum(nil))
	return nil
}

// Verify recomputes the signature and compares it in constant time.
func (r *Receipt) Verify(key []byte) (bool, error) {
	if r.Sig == "" {
		return false, nil
	}
	got := r.Sig
	cp := *r
	if err := cp.Sign(key); err != nil {
		return false, err
	}
	return hmac.Equal([]byte(got), []byte(cp.Sig)), nil
}
