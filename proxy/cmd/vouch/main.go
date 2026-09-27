// Command vouch is the entry point for the vouch proxy.
//
//	vouch keygen [--out <dir>] [--name <name>]
//	vouch proxy --signing-key <key.pem> --upstream "[name=]cmd args" \
//	    [--upstream ...] --receipts <dir> --schemas <dir> [--session <id>]
//	vouch receipts cat <log>
//	vouch canon [--lines] < input
//	vouch receipts verify --public-key <key.pub.pem> [--public-key ...] \
//	    [--require-sealed] [--expect-head <digest>] <log>
//
// An upstream's name defaults to its whole command; name= sets a short,
// stable one. Names key record/replay fixtures and must be unique.
//
// Receipts are signed with an Ed25519 key (--signing-key, else
// $VOUCH_SIGNING_KEY) in DSSE envelopes; anyone with the public key can
// verify them, with `vouch receipts verify` or any Ed25519 library.
// Judging an agent's answer against the log is the Python side's job
// (vouch-verify).
package main

import (
	"crypto/rand"
	"encoding/hex"
	"flag"
	"fmt"
	"os"
	"path/filepath"
	"sync"

	"github.com/jma49/vouch/proxy/internal/clock"
	"github.com/jma49/vouch/proxy/internal/extract"
	"github.com/jma49/vouch/proxy/internal/fixture"
	"github.com/jma49/vouch/proxy/internal/mcp"
	"github.com/jma49/vouch/proxy/internal/proxy"
	"github.com/jma49/vouch/proxy/internal/sign"
	"github.com/jma49/vouch/proxy/internal/store"
)

const version = "0.0.1-dev"

type stringSlice []string

func (s *stringSlice) String() string     { return fmt.Sprint(*s) }
func (s *stringSlice) Set(v string) error { *s = append(*s, v); return nil }

func main() {
	if len(os.Args) < 2 {
		usage()
		os.Exit(2)
	}
	switch os.Args[1] {
	case "version":
		fmt.Println("vouch", version)
	case "proxy", "keygen", "receipts", "canon":
		run := map[string]func([]string) error{
			"proxy": runProxy, "keygen": runKeygen, "receipts": runReceipts, "canon": runCanon,
		}[os.Args[1]]
		if err := run(os.Args[2:]); err != nil {
			fmt.Fprintln(os.Stderr, "vouch:", err)
			os.Exit(1)
		}
	default:
		usage()
		os.Exit(2)
	}
}

func usage() {
	fmt.Fprintln(os.Stderr, `usage:
  vouch keygen [--out <dir>] [--name <name>]
  vouch proxy --signing-key <key.pem> --upstream "[name=]cmd args" [--upstream ...] \
      --receipts <dir> --schemas <dir> [--session <id>]
  vouch receipts cat <log>
  vouch receipts verify --public-key <key.pub.pem> [--public-key ...] \
      [--require-sealed] [--expect-head <digest>] <log>
  vouch canon [--lines] < input
  vouch version`)
}

func runProxy(args []string) error {
	fs := flag.NewFlagSet("proxy", flag.ExitOnError)
	var upstreams stringSlice
	fs.Var(&upstreams, "upstream", "upstream MCP server as \"[name=]command\" (repeatable)")
	receiptsDir := fs.String("receipts", "receipts", "directory for the receipt log")
	schemasDir := fs.String("schemas", "schemas", "directory of fact-extraction sidecar configs")
	session := fs.String("session", "", "session id (default: random)")
	mode := fs.String("mode", "live", "live | record | replay (docs/design.md section 8.1)")
	fixturesDir := fs.String("fixtures", "fixtures", "fixture directory for record/replay")
	keyPath := fs.String("signing-key", os.Getenv("VOUCH_SIGNING_KEY"),
		"Ed25519 private key (PKCS#8 PEM, mode 0600) that signs receipts; default $VOUCH_SIGNING_KEY")
	if err := fs.Parse(args); err != nil {
		return err
	}
	if *mode != "live" && *mode != "record" && *mode != "replay" {
		return fmt.Errorf("invalid --mode %q", *mode)
	}
	if len(upstreams) == 0 && *mode != "replay" {
		return fmt.Errorf("at least one --upstream is required (except in replay mode)")
	}
	if *keyPath == "" {
		return fmt.Errorf("no signing key: pass --signing-key or set VOUCH_SIGNING_KEY (create one with `vouch keygen`)")
	}
	priv, err := sign.LoadPrivateKey(*keyPath)
	if err != nil {
		return err
	}
	signer := sign.NewSigner(priv)
	if *session == "" {
		*session = "s-" + randomHex(8)
	}

	schemas, err := extract.LoadDir(*schemasDir)
	if err != nil {
		return err
	}
	rlog, err := store.Open(filepath.Join(*receiptsDir, "receipts.jsonl"), signer)
	if err != nil {
		return err
	}
	defer rlog.Close()

	fixStore := &fixture.Store{Dir: *fixturesDir}
	var clk clock.Clock = &clock.Wall{}
	var ups []*proxy.Upstream

	switch *mode {
	case "replay":
		tools, err := fixStore.LoadAllTools()
		if err != nil {
			return err
		}
		if len(tools) == 0 {
			return fmt.Errorf("replay: no recorded upstreams in %s (run --mode=record first)", *fixturesDir)
		}
		epoch, err := fixStore.Epoch()
		if err != nil {
			return err
		}
		clk = &clock.Logical{Epoch: epoch}
		for _, rec := range tools {
			ups = append(ups, &proxy.Upstream{
				Name:   rec.Name,
				Client: &fixture.Replayer{Upstream: rec.Name, Store: fixStore, Tools: rec.Tools},
			})
		}
	default: // live, record
		specs, err := proxy.ParseUpstreams(upstreams)
		if err != nil {
			return err
		}
		defer func() { closeAll(ups) }()
		for _, spec := range specs {
			u, err := proxy.Spawn(spec)
			if err != nil {
				return err
			}
			if *mode == "record" {
				u.Client = fixture.NewRecorder(u.Name, u.Client, fixStore, clk.Now)
			}
			ups = append(ups, u)
		}
	}

	srv := &proxy.Server{
		Down:      mcp.NewConn(os.Stdin, os.Stdout),
		Upstreams: ups,
		Schemas:   schemas,
		Log:       rlog,
		SessionID: *session,
		Clock:     clk,
	}
	fmt.Fprintf(os.Stderr, "vouch proxy: mode %s, session %s, %d upstream(s), receipts in %s\n",
		*mode, *session, len(ups), *receiptsDir)
	if err := srv.Run(); err != nil {
		// No checkpoint: a verifier with --require-sealed will see that
		// this session did not end cleanly.
		return err
	}
	// The session ended cleanly: seal it. The head digest is printed so
	// it can be kept outside the log, the only way to later detect the
	// log being cut back to an earlier point (#54).
	head, err := rlog.Seal(*session, clk.Now())
	if err != nil {
		return err
	}
	fmt.Fprintf(os.Stderr, "vouch proxy: sealed session %s; head %s\n", *session, head)
	return nil
}

// closeAll shuts upstreams down in parallel, so shutdown takes at most
// one close timeout rather than one per upstream.
func closeAll(ups []*proxy.Upstream) {
	var wg sync.WaitGroup
	for _, u := range ups {
		if u.Close == nil {
			continue
		}
		wg.Add(1)
		go func() {
			defer wg.Done()
			if err := u.Close(); err != nil {
				fmt.Fprintln(os.Stderr, "vouch:", err)
			}
		}()
	}
	wg.Wait()
}

func runKeygen(args []string) error {
	fs := flag.NewFlagSet("keygen", flag.ExitOnError)
	out := fs.String("out", ".", "directory for the key files")
	name := fs.String("name", "vouch", "file name stem: <name>.pem and <name>.pub.pem")
	if err := fs.Parse(args); err != nil {
		return err
	}
	privPath, pubPath, err := sign.GenerateFiles(*out, *name)
	if err != nil {
		return err
	}
	pub, err := sign.LoadPublicKey(pubPath)
	if err != nil {
		return err
	}
	fmt.Printf("private key: %s (keep secret; mode 0600)\npublic key:  %s\nkey id:      %s\n",
		privPath, pubPath, sign.KeyID(pub))
	return nil
}

// runReceipts reads a receipt log for people: cat prints each receipt
// body as one JSON line (for jq and grep, since envelope payloads are
// base64), and verify checks every signature against trusted keys.
func runReceipts(args []string) error {
	if len(args) == 0 {
		return fmt.Errorf("receipts: want a subcommand: cat or verify")
	}
	switch args[0] {
	case "cat":
		if len(args) != 2 {
			return fmt.Errorf("usage: vouch receipts cat <log>")
		}
		// Each entry's payload is its canonical JSON body: receipts and
		// checkpoints alike, one per line.
		return store.Walk(args[1], nil, func(e *store.Entry) error {
			fmt.Println(string(e.Payload))
			return nil
		})
	case "verify":
		fs := flag.NewFlagSet("receipts verify", flag.ExitOnError)
		var pubs stringSlice
		fs.Var(&pubs, "public-key", "trusted Ed25519 public key PEM (repeatable)")
		requireSealed := fs.Bool("require-sealed", false, "fail unless the log ends in a checkpoint")
		expectHead := fs.String("expect-head", "", "fail unless the chain head is this digest (kept outside the log)")
		if err := fs.Parse(args[1:]); err != nil {
			return err
		}
		if fs.NArg() != 1 || len(pubs) == 0 {
			return fmt.Errorf("usage: vouch receipts verify --public-key <key.pub.pem> [--public-key ...] <log>")
		}
		keys := sign.Keyring{}
		for _, p := range pubs {
			pub, err := sign.LoadPublicKey(p)
			if err != nil {
				return err
			}
			keys.Add(pub)
		}
		audit, err := store.Verify(fs.Arg(0), keys)
		if err != nil {
			return err
		}
		if *requireSealed && !audit.Sealed {
			return fmt.Errorf("%s does not end in a checkpoint: it may have been cut short", fs.Arg(0))
		}
		if *expectHead != "" && audit.Head != *expectHead {
			return fmt.Errorf("%s head is %s, expected %s: entries were removed or added after that head",
				fs.Arg(0), audit.Head, *expectHead)
		}
		fmt.Printf("%d receipts and %d checkpoints verified; chain intact; sealed: %v\nhead %s\n",
			len(audit.Receipts), audit.Checkpoints, audit.Sealed, audit.Head)
		return nil
	default:
		return fmt.Errorf("receipts: unknown subcommand %q", args[0])
	}
}

func randomHex(n int) string {
	b := make([]byte, n)
	if _, err := rand.Read(b); err != nil {
		panic(err)
	}
	return hex.EncodeToString(b)
}
