//go:build !unix

package store

import "os"

// lockFile is a no-op where flock is unavailable: two proxies on one log
// are not prevented there (#99).
func lockFile(*os.File) error { return nil }
