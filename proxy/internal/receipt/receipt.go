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
// rules change (docs/canonical-json.md, "Versioning").
const PayloadType = "application/vnd.vouch.receipt+json; version=2"

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
	if err := json.Unmarshal(body, &r); err != nil {
		return nil, fmt.Errorf("receipt: parse body: %w", err)
	}
	return &r, nil
}
