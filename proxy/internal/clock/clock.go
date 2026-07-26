// Package clock isolates time reads behind an interface so replay runs
// can substitute a logical clock (docs/design.md section 8.2). No
// time.Now() in business logic.
package clock

import (
	"sync/atomic"
	"time"
)

// Clock provides wall time and a monotonically increasing logical tick.
type Clock interface {
	Now() time.Time
	Tick() int64
}

// Wall is the live clock: real UTC time plus an in-process tick counter.
type Wall struct {
	tick atomic.Int64
}

func (w *Wall) Now() time.Time { return time.Now().UTC() }
func (w *Wall) Tick() int64    { return w.tick.Add(1) }

// Logical is a replay clock: wall time is frozen at a fixture-derived
// epoch and only the tick advances. Runs replay identically regardless
// of when they execute.
type Logical struct {
	Epoch time.Time
	tick  atomic.Int64
}

func (l *Logical) Now() time.Time { return l.Epoch }
func (l *Logical) Tick() int64    { return l.tick.Add(1) }
