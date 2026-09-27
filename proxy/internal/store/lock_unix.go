//go:build unix

package store

import (
	"errors"
	"fmt"
	"os"
	"syscall"
)

// lockFile takes an exclusive, non-blocking lock on the log for the life
// of the open file: two processes appending to one log would each chain
// entries to their own view of it and fork the chain (#99). The lock is
// released when the file is closed, or the process exits.
func lockFile(f *os.File) error {
	if err := syscall.Flock(int(f.Fd()), syscall.LOCK_EX|syscall.LOCK_NB); err != nil {
		if errors.Is(err, syscall.EWOULDBLOCK) {
			return fmt.Errorf("store: %s is open in another process; use one receipt log per proxy", f.Name())
		}
		return fmt.Errorf("store: lock %s: %w", f.Name(), err)
	}
	return nil
}
