package receipt

import (
	"testing"
	"time"
)

func sampleReceipt() *Receipt {
	return &Receipt{
		ReceiptID:       "r-0001",
		SessionID:       "s-abc",
		TurnIndex:       3,
		ToolName:        "get_indicators",
		ArgsCanonical:   []byte(`{"symbol":"NVDA","timeframe":"1d"}`),
		ResultCanonical: []byte(`{"rsi_14":62.3,"symbol":"NVDA"}`),
		Facts: []Fact{{
			Entity:   "NVDA",
			Metric:   "rsi_14",
			Value:    62.3,
			JSONPtr:  "/rsi_14",
			TolClass: "indicator",
		}},
		DataAsOf:    "2026-07-24T20:00:00Z",
		WallTime:    time.Date(2026, 7, 25, 1, 12, 9, 0, time.UTC),
		LogicalTime: 41,
	}
}

func TestBodyIsCanonicalAndRoundTrips(t *testing.T) {
	r := sampleReceipt()
	r.ResultDigest = Digest(r.ResultCanonical)
	body, err := r.Body()
	if err != nil {
		t.Fatal(err)
	}
	again, err := Canonicalize(body)
	if err != nil || string(again) != string(body) {
		t.Fatalf("body is not canonical: %s", body)
	}
	if second, _ := r.Body(); string(second) != string(body) {
		t.Fatal("Body must be deterministic: its bytes are what gets signed")
	}
	parsed, err := ParseBody(body)
	if err != nil {
		t.Fatal(err)
	}
	reencoded, err := parsed.Body()
	if err != nil || string(reencoded) != string(body) {
		t.Fatalf("ParseBody(Body()) changed the receipt:\n%s\n%s", body, reencoded)
	}
}

func TestBodyCoversFacts(t *testing.T) {
	// Signatures cover Body, so a changed fact must change the bytes.
	r := sampleReceipt()
	before, _ := r.Body()
	r.Facts[0].Value = 68.0 // the classic hallucination
	after, _ := r.Body()
	if string(before) == string(after) {
		t.Fatal("a changed fact did not change the signed bytes")
	}
}

func TestDigestStableAcrossKeyOrder(t *testing.T) {
	a, _ := Canonicalize([]byte(`{"b":1,"a":2}`))
	b, _ := Canonicalize([]byte(`{"a":2,"b":1}`))
	if Digest(a) != Digest(b) {
		t.Fatal("digest differs for semantically identical documents")
	}
}
