// Command vouch is the entry point for the vouch proxy and tooling.
//
// MVP surface (planned):
//
//	vouch proxy  --upstream <mcp-server> --receipts <dir>
//	vouch verify --answer <file> --receipts <dir>
package main

import (
	"fmt"
	"os"
)

const version = "0.0.1-dev"

func main() {
	if len(os.Args) > 1 && os.Args[1] == "version" {
		fmt.Println("vouch", version)
		return
	}
	fmt.Fprintln(os.Stderr, "vouch: MVP in progress; see docs/design.md")
	os.Exit(2)
}
