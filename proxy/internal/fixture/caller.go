package fixture

import (
	"encoding/json"
	"fmt"
	"time"
)

// caller matches proxy.Caller structurally; declared here to avoid an
// import cycle.
type caller interface {
	Call(method string, params any) (json.RawMessage, error)
	Notify(method string, params any) error
}

// Recorder wraps a live upstream caller and records tools/list and
// tools/call responses as fixtures (--mode=record).
type Recorder struct {
	Upstream string
	Inner    caller
	Store    *Store
	Now      func() time.Time
}

// NewRecorder wraps inner, recording into store under the upstream name.
func NewRecorder(upstream string, inner caller, store *Store, now func() time.Time) *Recorder {
	return &Recorder{Upstream: upstream, Inner: inner, Store: store, Now: now}
}

func (r *Recorder) Call(method string, params any) (json.RawMessage, error) {
	result, err := r.Inner.Call(method, params)
	if err != nil {
		return nil, err
	}
	switch method {
	case "tools/list":
		if err := r.Store.SaveTools(r.Upstream, result, r.Now()); err != nil {
			return nil, err
		}
	case "tools/call":
		tool, args, err := canonicalCallArgs(params)
		if err != nil {
			return nil, err
		}
		if err := r.Store.SaveCall(tool, args, result, r.Now()); err != nil {
			return nil, err
		}
	}
	return result, nil
}

func (r *Recorder) Notify(method string, params any) error {
	return r.Inner.Notify(method, params)
}

// Replayer serves a recorded upstream entirely from fixtures
// (--mode=replay). It never touches the network; a call with no
// fixture is an error, not a passthrough.
type Replayer struct {
	Upstream string
	Store    *Store
	Tools    json.RawMessage // recorded tools/list result
}

func (r *Replayer) Call(method string, params any) (json.RawMessage, error) {
	switch method {
	case "initialize":
		return json.RawMessage(`{"protocolVersion":"2025-06-18","capabilities":{"tools":{}},"serverInfo":{"name":"vouch-replay","version":"0.0.1-dev"}}`), nil
	case "tools/list":
		return r.Tools, nil
	case "tools/call":
		tool, args, err := canonicalCallArgs(params)
		if err != nil {
			return nil, err
		}
		result, ok, err := r.Store.LoadCall(tool, args)
		if err != nil {
			return nil, err
		}
		if !ok {
			return nil, fmt.Errorf("replay: no fixture for tool %s args %s (run --mode=record first)", tool, args)
		}
		return result, nil
	default:
		return nil, fmt.Errorf("replay: method %s not recorded", method)
	}
}

func (r *Replayer) Notify(string, any) error { return nil }
