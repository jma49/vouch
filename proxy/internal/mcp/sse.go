package mcp

import (
	"bufio"
	"bytes"
	"errors"
	"fmt"
	"io"
)

// sseReader reads the JSON-RPC messages in a text/event-stream body, as
// MCP's Streamable HTTP transport sends them: one message in the data of
// each event. Event types, ids, retry hints, and comments are ignored;
// vouch does not resume streams.
type sseReader struct {
	r    *bufio.Reader
	data bytes.Buffer
}

func newSSEReader(r io.Reader) *sseReader {
	return &sseReader{r: bufio.NewReaderSize(r, 64<<10)}
}

// Next returns the next event's message, io.EOF at the end of the
// stream, or a *FrameError for an event whose data is not a message.
func (s *sseReader) Next() (*Message, error) {
	for {
		line, err := s.readLine()
		if err != nil {
			if errors.Is(err, io.EOF) && s.data.Len() > 0 {
				return s.dispatch() // a final event without its blank line
			}
			return nil, err
		}
		switch {
		case len(line) == 0: // end of event
			if s.data.Len() > 0 {
				return s.dispatch()
			}
		case line[0] == ':': // comment, often a keep-alive
		default:
			field, value, _ := bytes.Cut(line, []byte(":"))
			value = bytes.TrimPrefix(value, []byte(" "))
			if string(field) == "data" {
				if s.data.Len() > 0 {
					s.data.WriteByte('\n')
				}
				if s.data.Len()+len(value) > MaxFrame {
					s.data.Reset()
					return nil, &FrameError{Code: CodeInvalidRequest, Err: fmt.Errorf("%w of %d bytes", ErrFrameTooLarge, MaxFrame)}
				}
				s.data.Write(value)
			}
		}
	}
}

func (s *sseReader) dispatch() (*Message, error) {
	data := bytes.TrimSpace(s.data.Bytes())
	defer s.data.Reset()
	return decodeFrame(append([]byte(nil), data...))
}

// readLine returns one line without its terminator (LF, CRLF, or CR
// before LF, per the SSE spec's common forms).
func (s *sseReader) readLine() ([]byte, error) {
	line, err := s.r.ReadBytes('\n')
	if err != nil && (len(line) == 0 || !errors.Is(err, io.EOF)) {
		return nil, err
	}
	return bytes.TrimRight(line, "\r\n"), nil
}

// writeSSE writes one message as one event.
func writeSSE(w io.Writer, raw []byte) error {
	_, err := fmt.Fprintf(w, "event: message\ndata: %s\n\n", raw)
	return err
}
