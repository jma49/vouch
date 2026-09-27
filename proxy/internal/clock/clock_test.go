package clock

import (
	"testing"
	"time"
)

// TestStopwatch pins #102: the wall clock's stopwatch is monotonic and
// the logical clock's reports nothing, so replays stay identical.
func TestStopwatch(t *testing.T) {
	elapsed := (&Wall{}).Stopwatch()
	time.Sleep(5 * time.Millisecond)
	if d := elapsed(); d < 5*time.Millisecond {
		t.Fatalf("wall stopwatch: %v", d)
	}
	if d := (&Logical{Epoch: time.Unix(0, 0)}).Stopwatch()(); d != 0 {
		t.Fatalf("logical stopwatch: %v", d)
	}
}
