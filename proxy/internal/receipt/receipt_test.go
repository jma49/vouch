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

func TestSignVerifyRoundtrip(t *testing.T) {
	key := []byte("test-key")
	r := sampleReceipt()
	r.ResultDigest = Digest(r.ResultCanonical)

	if err := r.Sign(key); err != nil {
		t.Fatalf("Sign: %v", err)
	}
	if r.Sig == "" {
		t.Fatal("empty signature after Sign")
	}
	ok, err := r.Verify(key)
	if err != nil {
		t.Fatalf("Verify: %v", err)
	}
	if !ok {
		t.Fatal("signature did not verify")
	}
}

func TestVerifyDetectsTampering(t *testing.T) {
	key := []byte("test-key")
	r := sampleReceipt()
	if err := r.Sign(key); err != nil {
		t.Fatal(err)
	}
	r.Facts[0].Value = 68.0 // the classic hallucination
	ok, err := r.Verify(key)
	if err != nil {
		t.Fatal(err)
	}
	if ok {
		t.Fatal("tampered receipt verified; it must not")
	}
}

func TestVerifyRejectsWrongKey(t *testing.T) {
	r := sampleReceipt()
	if err := r.Sign([]byte("key-a")); err != nil {
		t.Fatal(err)
	}
	ok, err := r.Verify([]byte("key-b"))
	if err != nil {
		t.Fatal(err)
	}
	if ok {
		t.Fatal("verified under wrong key")
	}
}

func TestVerifyUnsignedIsFalse(t *testing.T) {
	r := sampleReceipt()
	ok, err := r.Verify([]byte("k"))
	if err != nil {
		t.Fatal(err)
	}
	if ok {
		t.Fatal("unsigned receipt must not verify")
	}
}

func TestDigestStableAcrossKeyOrder(t *testing.T) {
	a, _ := Canonicalize([]byte(`{"b":1,"a":2}`))
	b, _ := Canonicalize([]byte(`{"a":2,"b":1}`))
	if Digest(a) != Digest(b) {
		t.Fatal("digest differs for semantically identical documents")
	}
}
