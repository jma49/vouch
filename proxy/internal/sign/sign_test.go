package sign

import (
	"crypto/ed25519"
	"encoding/base64"
	"errors"
	"os"
	"path/filepath"
	"runtime"
	"testing"
)

const testType = "application/vnd.vouch.test"

func newSigner(t *testing.T, seed byte) *Signer {
	t.Helper()
	s := make([]byte, ed25519.SeedSize)
	for i := range s {
		s[i] = seed
	}
	return NewSigner(ed25519.NewKeyFromSeed(s))
}

func TestPAEMatchesTheDSSESpec(t *testing.T) {
	// The example from the DSSE protocol document.
	got := string(PAE("http://example.com/HelloWorld", []byte("hello world")))
	want := "DSSEv1 29 http://example.com/HelloWorld 11 hello world"
	if got != want {
		t.Fatalf("PAE = %q, want %q", got, want)
	}
}

func TestSignOpenRoundTrip(t *testing.T) {
	s := newSigner(t, 1)
	env := s.Sign(testType, []byte(`{"a":1}`))
	if again := s.Sign(testType, []byte(`{"a":1}`)); again.Signatures[0].Sig != env.Signatures[0].Sig {
		t.Fatal("Ed25519 signing must be deterministic for reproducible golden data")
	}
	keys := Keyring{}
	keys.Add(s.Public())
	payload, id, err := Open(env, testType, keys)
	if err != nil || string(payload) != `{"a":1}` || id != s.KeyID() {
		t.Fatalf("Open = %q, %q, %v", payload, id, err)
	}
}

func TestOpenRejects(t *testing.T) {
	s := newSigner(t, 1)
	other := newSigner(t, 2)
	trusted := Keyring{}
	trusted.Add(s.Public())
	env := s.Sign(testType, []byte(`{"a":1}`))

	tampered := env
	tampered.Payload = base64.StdEncoding.EncodeToString([]byte(`{"a":2}`))
	forged := other.Sign(testType, []byte(`{"a":1}`))
	forged.Signatures[0].KeyID = s.KeyID() // claim the trusted id
	cases := map[string]Envelope{
		"edited payload":        tampered,
		"signed by another key": other.Sign(testType, []byte(`{"a":1}`)),
		"forged key id":         forged,
		"no signatures":         {Payload: env.Payload, PayloadType: testType},
	}
	for name, e := range cases {
		if _, _, err := Open(e, testType, trusted); !errors.Is(err, ErrUntrusted) {
			t.Errorf("%s: err = %v, want ErrUntrusted", name, err)
		}
	}
	if _, _, err := Open(env, "application/vnd.vouch.other", trusted); err == nil {
		t.Error("a different payload type must not verify")
	}
}

func TestOneTrustedSignatureSuffices(t *testing.T) {
	old, cur := newSigner(t, 1), newSigner(t, 2)
	env := cur.Sign(testType, []byte("x"))
	env.Signatures = append([]Signature{{KeyID: "ed25519:unknown", Sig: "AAAA"}}, env.Signatures...)
	keys := Keyring{}
	keys.Add(old.Public())
	keys.Add(cur.Public())
	if _, id, err := Open(env, testType, keys); err != nil || id != cur.KeyID() {
		t.Fatalf("id=%q err=%v", id, err)
	}
}

func TestKeyFilesRoundTripAndPermissions(t *testing.T) {
	dir := t.TempDir()
	privPath, pubPath, err := GenerateFiles(dir, "k")
	if err != nil {
		t.Fatal(err)
	}
	priv, err := LoadPrivateKey(privPath)
	if err != nil {
		t.Fatal(err)
	}
	pub, err := LoadPublicKey(pubPath)
	if err != nil {
		t.Fatal(err)
	}
	if KeyID(pub) != NewSigner(priv).KeyID() {
		t.Fatal("public key does not match private key")
	}
	if _, _, err := GenerateFiles(dir, "k"); err == nil {
		t.Fatal("GenerateFiles must not overwrite existing keys")
	}
	if runtime.GOOS == "windows" {
		return
	}
	if err := os.Chmod(privPath, 0o644); err != nil {
		t.Fatal(err)
	}
	if _, err := LoadPrivateKey(privPath); err == nil {
		t.Fatal("a world-readable private key must be refused")
	}
	if _, err := LoadPrivateKey(filepath.Join(dir, "missing.pem")); err == nil {
		t.Fatal("missing key file must be an error")
	}
}
