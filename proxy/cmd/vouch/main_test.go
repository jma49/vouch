package main

import (
	"os"
	"strings"
	"testing"

	"github.com/jma49/vouch/proxy/internal/proxy"
)

func TestParseHeaders(t *testing.T) {
	specs := []proxy.UpstreamSpec{{Name: "remote", Command: "https://example.test/mcp"}}
	t.Setenv("VOUCH_TEST_TOKEN", "s3cret")
	got, err := parseHeaders([]string{"remote=Authorization: env:VOUCH_TEST_TOKEN", "remote=X-Team:  data "}, specs)
	if err != nil {
		t.Fatal(err)
	}
	if got["remote"].Get("Authorization") != "s3cret" || got["remote"].Get("X-Team") != "data" {
		t.Fatalf("got %v", got)
	}
	// #101: once read, the credential leaves the environment spawned
	// upstreams inherit.
	if _, still := os.LookupEnv("VOUCH_TEST_TOKEN"); still {
		t.Fatal("env: credential still in the environment")
	}
	for _, bad := range []struct{ value, want string }{
		{"remote=Authorization", "want name=Header: value"},
		{"Authorization: x", "want name=Header: value"},
		{"other=A: b", `no upstream named "other"`},
		{"remote=A: env:VOUCH_TEST_UNSET", "is empty"},
	} {
		if _, err := parseHeaders([]string{bad.value}, specs); err == nil || !strings.Contains(err.Error(), bad.want) {
			t.Errorf("parseHeaders(%q) = %v, want %q", bad.value, err, bad.want)
		}
	}
}
