// Command vouch is the entry point for the vouch proxy.
//
//	vouch keygen [--out <dir>] [--name <name>]
//	vouch proxy --signing-key <key.pem> --upstream "[name=]cmd args" \
//	    [--upstream "[name=]https://host/mcp" --upstream-header "name=H: v"] \
//	    [--listen 127.0.0.1:8765] [--cite] --receipts <dir> --schemas <dir> [--session <id>]
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
	"context"
	"crypto/rand"
	"encoding/hex"
	"errors"
	"flag"
	"fmt"
	"net"
	"net/http"
	"os"
	"os/signal"
	"path/filepath"
	"strings"
	"sync"
	"syscall"
	"time"

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
      [--upstream "[name=]https://host/mcp" --upstream-header "name=Header: value"] \
      [--listen 127.0.0.1:8765] [--cite] --receipts <dir> --schemas <dir> [--session <id>]
  vouch receipts cat <log>
  vouch receipts verify --public-key <key.pub.pem> [--public-key ...] \
      [--require-sealed] [--expect-head <digest>] <log>
  vouch canon [--lines] < input
  vouch version`)
}

func runProxy(args []string) error {
	fs := flag.NewFlagSet("proxy", flag.ExitOnError)
	var upstreams stringSlice
	fs.Var(&upstreams, "upstream", "upstream MCP server as \"[name=]command\" or \"[name=]https://host/mcp\" (repeatable)")
	var headers stringSlice
	fs.Var(&headers, "upstream-header", "header for an HTTP upstream as \"name=Header: value\"; a value of env:VAR reads $VAR (repeatable)")
	listen := fs.String("listen", "", "serve the agent over Streamable HTTP at http://ADDR/mcp instead of stdio")
	cite := fs.Bool("cite", false, "add a block to each result telling the model how to cite its receipted facts (design section 5)")
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
	// Upstreams are spawned with this process's environment; the key's
	// path is none of their business once the key is loaded (#101).
	os.Unsetenv("VOUCH_SIGNING_KEY")
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
		byName, err := parseHeaders(headers, specs)
		if err != nil {
			return err
		}
		defer func() { closeAll(ups) }()
		for _, spec := range specs {
			u, err := proxy.Connect(spec, byName[spec.Name])
			if err != nil {
				return err
			}
			if *mode == "record" {
				u.Client = fixture.NewRecorder(u.Name, u.Client, fixStore, clk.Now)
			}
			ups = append(ups, u)
		}
	}

	var down mcp.Transport = mcp.NewConn(os.Stdin, os.Stdout)
	if *listen != "" {
		hs, stop, err := serveHTTP(*listen)
		if err != nil {
			return err
		}
		defer stop()
		down = hs
	}
	srv := &proxy.Server{
		Down:      down,
		Upstreams: ups,
		Schemas:   schemas,
		Log:       rlog,
		SessionID: *session,
		Clock:     clk,
		Cite:      *cite,
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

// parseHeaders reads --upstream-header values into per-upstream
// headers. A value of env:VAR is read from the environment, so a token
// need not appear in the process list.
func parseHeaders(values []string, specs []proxy.UpstreamSpec) (map[string]http.Header, error) {
	known := make(map[string]bool)
	for _, s := range specs {
		known[s.Name] = true
	}
	out := make(map[string]http.Header)
	for _, v := range values {
		name, header, ok := strings.Cut(v, "=")
		key, value, ok2 := strings.Cut(header, ":")
		key, value = strings.TrimSpace(key), strings.TrimSpace(value)
		if !ok || !ok2 || key == "" {
			return nil, fmt.Errorf("--upstream-header %q: want name=Header: value", v)
		}
		if !known[name] {
			return nil, fmt.Errorf("--upstream-header %q: no upstream named %q (name it with name=URL)", v, name)
		}
		if env, ok := strings.CutPrefix(value, "env:"); ok {
			value = os.Getenv(env)
			if value == "" {
				return nil, fmt.Errorf("--upstream-header %q: $%s is empty", v, env)
			}
			// The credential is for one HTTP upstream; spawned upstreams
			// inherit the environment, so it leaves it (#101).
			os.Unsetenv(env)
		}
		if out[name] == nil {
			out[name] = http.Header{}
		}
		out[name].Add(key, value)
	}
	return out, nil
}

// serveHTTP serves one agent session over Streamable HTTP at /mcp. A
// signal ends the session as DELETE would, so the log is sealed. A
// non-loopback address is allowed but warned about: the endpoint has no
// authentication of its own.
func serveHTTP(addr string) (*mcp.HTTPServer, func(), error) {
	ln, err := net.Listen("tcp", addr)
	if err != nil {
		return nil, nil, fmt.Errorf("listen: %w", err)
	}
	if host, _, _ := net.SplitHostPort(ln.Addr().String()); !net.ParseIP(host).IsLoopback() {
		fmt.Fprintf(os.Stderr, "vouch proxy: warning: listening on %s, beyond this machine; the endpoint has no authentication\n", ln.Addr())
	}
	hs := mcp.NewHTTPServer(nil)
	mux := http.NewServeMux()
	mux.Handle("/mcp", hs)
	server := &http.Server{Handler: mux, ReadHeaderTimeout: 10 * time.Second}
	go func() {
		if err := server.Serve(ln); err != nil && !errors.Is(err, http.ErrServerClosed) {
			fmt.Fprintln(os.Stderr, "vouch proxy:", err)
			hs.Close()
		}
	}()
	signals := make(chan os.Signal, 1)
	signal.Notify(signals, os.Interrupt, syscall.SIGTERM)
	go func() {
		if _, ok := <-signals; ok {
			hs.Close()
		}
	}()
	fmt.Fprintf(os.Stderr, "vouch proxy: serving http://%s/mcp\n", ln.Addr())
	return hs, func() {
		signal.Stop(signals)
		close(signals)
		ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		_ = server.Shutdown(ctx)
	}, nil
}
