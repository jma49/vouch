// Package sign implements vouch's signature format: Ed25519 signatures
// in DSSE envelopes (https://github.com/secure-systems-lab/dsse).
//
// The signature covers the exact payload bytes, via DSSE's
// pre-authentication encoding, so a third party verifies a receipt with
// any Ed25519 library and base64, without vouch's canonicalizer
// (docs/canonical-json.md). Ed25519 replaces the HMAC scheme vouch
// started with: HMAC is symmetric, so anyone able to verify could forge
// (issue #53, docs/handoff.md "Decisions and trade-offs").
package sign

import (
	"crypto/ed25519"
	"crypto/rand"
	"crypto/sha256"
	"crypto/x509"
	"encoding/base64"
	"encoding/hex"
	"encoding/pem"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"runtime"
	"strconv"
)

// Envelope is a DSSE envelope. Payload is standard base64.
type Envelope struct {
	Payload     string      `json:"payload"`
	PayloadType string      `json:"payloadType"`
	Signatures  []Signature `json:"signatures"`
}

// Signature is one DSSE signature. Sig is standard base64.
type Signature struct {
	KeyID string `json:"keyid"`
	Sig   string `json:"sig"`
}

// PAE is DSSE's pre-authentication encoding: the exact bytes signed.
// Binding the payload type prevents a signature over one kind of
// payload from being replayed as another.
func PAE(payloadType string, payload []byte) []byte {
	out := []byte("DSSEv1 " + strconv.Itoa(len(payloadType)) + " " + payloadType + " " +
		strconv.Itoa(len(payload)) + " ")
	return append(out, payload...)
}

// KeyID names a public key: "ed25519:" and the first 16 hex characters
// of sha256 over the raw 32-byte key.
func KeyID(pub ed25519.PublicKey) string {
	sum := sha256.Sum256(pub)
	return "ed25519:" + hex.EncodeToString(sum[:])[:16]
}

// Signer signs payloads with one Ed25519 private key.
type Signer struct {
	key ed25519.PrivateKey
	id  string
}

// NewSigner wraps a private key.
func NewSigner(key ed25519.PrivateKey) *Signer {
	return &Signer{key: key, id: KeyID(key.Public().(ed25519.PublicKey))}
}

// KeyID returns the signer's key id.
func (s *Signer) KeyID() string { return s.id }

// Public returns the signer's public key.
func (s *Signer) Public() ed25519.PublicKey { return s.key.Public().(ed25519.PublicKey) }

// Sign wraps payload in a signed envelope. Ed25519 is deterministic, so
// the same key and payload always produce the same envelope.
func (s *Signer) Sign(payloadType string, payload []byte) Envelope {
	sig := ed25519.Sign(s.key, PAE(payloadType, payload))
	return Envelope{
		Payload:     base64.StdEncoding.EncodeToString(payload),
		PayloadType: payloadType,
		Signatures:  []Signature{{KeyID: s.id, Sig: base64.StdEncoding.EncodeToString(sig)}},
	}
}

// Keyring holds the public keys a verifier trusts, by key id.
type Keyring map[string]ed25519.PublicKey

// Add trusts pub.
func (k Keyring) Add(pub ed25519.PublicKey) { k[KeyID(pub)] = pub }

// ErrUntrusted means no signature on the envelope verified under a
// trusted key.
var ErrUntrusted = errors.New("no valid signature from a trusted key")

// Decode returns the payload without verifying anything.
func Decode(env Envelope) ([]byte, error) {
	payload, err := base64.StdEncoding.DecodeString(env.Payload)
	if err != nil {
		return nil, fmt.Errorf("sign: payload is not base64: %w", err)
	}
	return payload, nil
}

// Open verifies env against keys and returns its payload and the id of
// the key that signed it. Following DSSE, one valid signature from a
// trusted key suffices; signatures under unknown key ids are ignored,
// which lets a log span a key rotation.
func Open(env Envelope, payloadType string, keys Keyring) ([]byte, string, error) {
	if env.PayloadType != payloadType {
		return nil, "", fmt.Errorf("sign: payload type %q, want %q", env.PayloadType, payloadType)
	}
	payload, err := Decode(env)
	if err != nil {
		return nil, "", err
	}
	msg := PAE(env.PayloadType, payload)
	for _, s := range env.Signatures {
		pub, ok := keys[s.KeyID]
		if !ok {
			continue
		}
		sig, err := base64.StdEncoding.DecodeString(s.Sig)
		if err == nil && ed25519.Verify(pub, msg, sig) {
			return payload, s.KeyID, nil
		}
	}
	return nil, "", ErrUntrusted
}

// ParsePrivateKeyPEM parses a PKCS#8 "PRIVATE KEY" PEM block holding an
// Ed25519 key.
func ParsePrivateKeyPEM(data []byte) (ed25519.PrivateKey, error) {
	block, _ := pem.Decode(data)
	if block == nil || block.Type != "PRIVATE KEY" {
		return nil, errors.New("sign: no PKCS#8 PRIVATE KEY PEM block")
	}
	key, err := x509.ParsePKCS8PrivateKey(block.Bytes)
	if err != nil {
		return nil, fmt.Errorf("sign: parse private key: %w", err)
	}
	ed, ok := key.(ed25519.PrivateKey)
	if !ok {
		return nil, fmt.Errorf("sign: private key is %T, want Ed25519", key)
	}
	return ed, nil
}

// ParsePublicKeyPEM parses a PKIX "PUBLIC KEY" PEM block holding an
// Ed25519 key.
func ParsePublicKeyPEM(data []byte) (ed25519.PublicKey, error) {
	block, _ := pem.Decode(data)
	if block == nil || block.Type != "PUBLIC KEY" {
		return nil, errors.New("sign: no PUBLIC KEY PEM block")
	}
	key, err := x509.ParsePKIXPublicKey(block.Bytes)
	if err != nil {
		return nil, fmt.Errorf("sign: parse public key: %w", err)
	}
	ed, ok := key.(ed25519.PublicKey)
	if !ok {
		return nil, fmt.Errorf("sign: public key is %T, want Ed25519", key)
	}
	return ed, nil
}

// LoadPrivateKey reads a private key file, refusing one that other
// users can read, as ssh does: a signing key others can read is a
// signing key others can use.
func LoadPrivateKey(path string) (ed25519.PrivateKey, error) {
	info, err := os.Stat(path)
	if err != nil {
		return nil, fmt.Errorf("sign: %w", err)
	}
	if runtime.GOOS != "windows" && info.Mode().Perm()&0o077 != 0 {
		return nil, fmt.Errorf("sign: private key %s is accessible by other users (mode %#o); chmod 600 it",
			path, info.Mode().Perm())
	}
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf("sign: %w", err)
	}
	return ParsePrivateKeyPEM(data)
}

// LoadPublicKey reads a public key file.
func LoadPublicKey(path string) (ed25519.PublicKey, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf("sign: %w", err)
	}
	return ParsePublicKeyPEM(data)
}

// MarshalPrivateKeyPEM encodes key as PKCS#8 PEM.
func MarshalPrivateKeyPEM(key ed25519.PrivateKey) ([]byte, error) {
	der, err := x509.MarshalPKCS8PrivateKey(key)
	if err != nil {
		return nil, fmt.Errorf("sign: marshal private key: %w", err)
	}
	return pem.EncodeToMemory(&pem.Block{Type: "PRIVATE KEY", Bytes: der}), nil
}

// MarshalPublicKeyPEM encodes pub as PKIX PEM.
func MarshalPublicKeyPEM(pub ed25519.PublicKey) ([]byte, error) {
	der, err := x509.MarshalPKIXPublicKey(pub)
	if err != nil {
		return nil, fmt.Errorf("sign: marshal public key: %w", err)
	}
	return pem.EncodeToMemory(&pem.Block{Type: "PUBLIC KEY", Bytes: der}), nil
}

// GenerateFiles creates a new keypair as <dir>/<name>.pem (mode 0600)
// and <dir>/<name>.pub.pem, refusing to overwrite either.
func GenerateFiles(dir, name string) (privPath, pubPath string, err error) {
	pub, priv, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		return "", "", fmt.Errorf("sign: generate: %w", err)
	}
	privPEM, err := MarshalPrivateKeyPEM(priv)
	if err != nil {
		return "", "", err
	}
	pubPEM, err := MarshalPublicKeyPEM(pub)
	if err != nil {
		return "", "", err
	}
	if err := os.MkdirAll(dir, 0o700); err != nil {
		return "", "", fmt.Errorf("sign: %w", err)
	}
	privPath = filepath.Join(dir, name+".pem")
	pubPath = filepath.Join(dir, name+".pub.pem")
	// Both or neither: a new private key beside an old public key would
	// sign receipts nobody can verify with the published key (#99).
	for _, path := range []string{privPath, pubPath} {
		if _, err := os.Lstat(path); err == nil {
			return "", "", fmt.Errorf("sign: %s already exists", path)
		}
	}
	if err := writeNew(privPath, privPEM, 0o600); err != nil {
		return "", "", err
	}
	if err := writeNew(pubPath, pubPEM, 0o644); err != nil {
		os.Remove(privPath)
		return "", "", err
	}
	return privPath, pubPath, nil
}

// writeNew creates path with data, failing if it already exists.
func writeNew(path string, data []byte, mode os.FileMode) error {
	f, err := os.OpenFile(path, os.O_WRONLY|os.O_CREATE|os.O_EXCL, mode)
	if err != nil {
		return fmt.Errorf("sign: %w", err)
	}
	if _, err := f.Write(data); err != nil {
		f.Close()
		os.Remove(path)
		return fmt.Errorf("sign: write %s: %w", path, err)
	}
	if err := f.Close(); err != nil {
		os.Remove(path)
		return fmt.Errorf("sign: write %s: %w", path, err)
	}
	return nil
}
