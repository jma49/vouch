// Command vouch is the entry point for the vouch proxy.
//
//	vouch proxy --upstream "cmd args" [--upstream ...] \
//	    --receipts <dir> --schemas <dir> [--session <id>]
//
// The HMAC signing key is read from $VOUCH_HMAC_KEY. Verification of
// answers against the receipt log is the Python side's job (vouch-verify).
package main

import (
	"crypto/rand"
	"encoding/hex"
	"flag"
	"fmt"
	"os"
	"path/filepath"

	"github.com/jma49/vouch/proxy/internal/clock"
	"github.com/jma49/vouch/proxy/internal/extract"
	"github.com/jma49/vouch/proxy/internal/fixture"
	"github.com/jma49/vouch/proxy/internal/mcp"
	"github.com/jma49/vouch/proxy/internal/proxy"
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
	case "proxy":
		if err := runProxy(os.Args[2:]); err != nil {
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
  vouch proxy --upstream "cmd args" [--upstream ...] --receipts <dir> --schemas <dir>
  vouch version`)
}

func runProxy(args []string) error {
	fs := flag.NewFlagSet("proxy", flag.ExitOnError)
	var upstreams stringSlice
	fs.Var(&upstreams, "upstream", "upstream MCP server command (repeatable)")
	receiptsDir := fs.String("receipts", "receipts", "directory for the receipt log")
	schemasDir := fs.String("schemas", "schemas", "directory of fact-extraction sidecar configs")
	session := fs.String("session", "", "session id (default: random)")
	mode := fs.String("mode", "live", "live | record | replay (docs/design.md section 8.1)")
	fixturesDir := fs.String("fixtures", "fixtures", "fixture directory for record/replay")
	if err := fs.Parse(args); err != nil {
		return err
	}
	if *mode != "live" && *mode != "record" && *mode != "replay" {
		return fmt.Errorf("invalid --mode %q", *mode)
	}
	if len(upstreams) == 0 && *mode != "replay" {
		return fmt.Errorf("at least one --upstream is required (except in replay mode)")
	}
	key := []byte(os.Getenv("VOUCH_HMAC_KEY"))
	if len(key) == 0 {
		return fmt.Errorf("VOUCH_HMAC_KEY is not set; receipts must be signed")
	}
	if *session == "" {
		*session = "s-" + randomHex(8)
	}

	schemas, err := extract.LoadDir(*schemasDir)
	if err != nil {
		return err
	}
	rlog, err := store.Open(filepath.Join(*receiptsDir, "receipts.jsonl"))
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
		for name, toolsResult := range tools {
			ups = append(ups, &proxy.Upstream{
				Name:   name,
				Client: &fixture.Replayer{Upstream: name, Store: fixStore, Tools: toolsResult},
			})
		}
	default: // live, record
		for _, cmd := range upstreams {
			u, err := proxy.Spawn(cmd)
			if err != nil {
				return err
			}
			defer u.Close()
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
		Key:       key,
		SessionID: *session,
		Clock:     clk,
	}
	fmt.Fprintf(os.Stderr, "vouch proxy: mode %s, session %s, %d upstream(s), receipts in %s\n",
		*mode, *session, len(ups), *receiptsDir)
	return srv.Run()
}

func randomHex(n int) string {
	b := make([]byte, n)
	if _, err := rand.Read(b); err != nil {
		panic(err)
	}
	return hex.EncodeToString(b)
}
