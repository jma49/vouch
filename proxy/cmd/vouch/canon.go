package main

import (
	"bufio"
	"encoding/base64"
	"flag"
	"fmt"
	"io"
	"os"

	"github.com/jma49/vouch/proxy/internal/receipt"
)

// runCanon prints the vouch canonical JSON form (docs/canonical-json.md)
// of a document on stdin, so anyone can reproduce a receipt digest.
//
// With --lines it canonicalizes many documents in one process, for the
// cross-language differential test (docs/roadmap.md Phase 4): each input
// line is one document in base64, and each output line is its canonical
// form in base64, or "!" and the reason it was rejected. base64 keeps
// newlines and invalid UTF-8 inside a document from breaking the framing.
func runCanon(args []string) error {
	fs := flag.NewFlagSet("canon", flag.ExitOnError)
	lines := fs.Bool("lines", false, "one base64 document per line in, one base64 result or !error per line out")
	if err := fs.Parse(args); err != nil {
		return err
	}
	if fs.NArg() != 0 {
		return fmt.Errorf("usage: vouch canon [--lines] < input")
	}
	if *lines {
		return canonLines(os.Stdin, os.Stdout)
	}
	raw, err := io.ReadAll(os.Stdin)
	if err != nil {
		return fmt.Errorf("canon: read: %w", err)
	}
	out, err := receipt.Canonicalize(raw)
	if err != nil {
		return err
	}
	_, err = fmt.Printf("%s\n", out)
	return err
}

func canonLines(r io.Reader, w io.Writer) error {
	in := bufio.NewScanner(r)
	in.Buffer(make([]byte, 0, 64*1024), 64<<20)
	out := bufio.NewWriter(w)
	for in.Scan() {
		raw, err := base64.StdEncoding.DecodeString(in.Text())
		if err != nil {
			return fmt.Errorf("canon: line is not base64: %w", err)
		}
		if c, err := receipt.Canonicalize(raw); err != nil {
			fmt.Fprintf(out, "!%v\n", err)
		} else {
			fmt.Fprintln(out, base64.StdEncoding.EncodeToString(c))
		}
	}
	if err := in.Err(); err != nil {
		return fmt.Errorf("canon: read: %w", err)
	}
	return out.Flush()
}
