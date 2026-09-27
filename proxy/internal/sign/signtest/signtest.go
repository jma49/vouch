// Package signtest provides deterministic signing keys for tests, in the
// spirit of net/http/httptest. Never use these keys outside tests.
package signtest

import (
	"crypto/ed25519"

	"github.com/jma49/vouch/proxy/internal/sign"
)

// Signer returns a signer whose key is derived from seed, so tests and
// golden data are reproducible.
func Signer(seed byte) *sign.Signer {
	s := make([]byte, ed25519.SeedSize)
	for i := range s {
		s[i] = seed
	}
	return sign.NewSigner(ed25519.NewKeyFromSeed(s))
}

// Keyring trusts the public keys of signers.
func Keyring(signers ...*sign.Signer) sign.Keyring {
	k := sign.Keyring{}
	for _, s := range signers {
		k.Add(s.Public())
	}
	return k
}
