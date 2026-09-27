// Package clock isolates time reads behind an interface so replay runs
// can substitute a logical clock (docs/design.md section 8.2). No
// time.Now() in business logic.
package clock

import (
	"sync/atomic"
	"time"
)

// Clock provides wall time, a monotonically increasing logical tick,
// and a stopwatch for durations.
type Clock interface {
	Now() time.Time
	Tick() int64
	// Stopwatch starts timing and returns a function reporting the time
	// elapsed since. Durations come from the monotonic clock, never from
	// differences of wall times, which step with NTP (#102).
	Stopwatch() func() time.Duration
}

// Wall is the live clock: real UTC time plus an in-process tick counter.
type Wall struct {
	tick atomic.Int64
}

func (w *Wall) Now() time.Time { return time.Now().UTC() }
func (w *Wall) Tick() int64    { return w.tick.Add(1) }

// Stopwatch measures with time.Now's monotonic reading, which .UTC()
// in Now strips.
func (w *Wall) Stopwatch() func() time.Duration {
	start := time.Now()
	return func() time.Duration { return time.Since(start) }
}

// Logical is a replay clock: wall time is frozen at a fixture-derived
// epoch and only the tick advances. Runs replay identically regardless
// of when they execute.
type Logical struct {
	Epoch time.Time
	tick  atomic.Int64
}

func (l *Logical) Now() time.Time { return l.Epoch }
func (l *Logical) Tick() int64    { return l.tick.Add(1) }

// Stopwatch reports no elapsed time: replay has no latency to measure,
// and a measured one would make runs differ.
func (l *Logical) Stopwatch() func() time.Duration {
	return func() time.Duration { return 0 }
}
