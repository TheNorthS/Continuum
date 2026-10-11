import { expect, test } from 'vitest';
import { SSEParser } from './sse';

test('handles fragmented events, heartbeats, CRLF and multiple events per packet', () => {
  const parser = new SSEParser();
  expect(parser.push(': heartbeat\r\n\r\nevent: st')).toEqual([]);
  expect(parser.push('ep\r\ndata: {"label":"检索"}\r\n\r')).toEqual([]);
  expect(parser.push('\nevent: result\ndata: {"answer":"完成"}\n\n')).toEqual([
    { event: 'step', data: '{"label":"检索"}' },
    { event: 'result', data: '{"answer":"完成"}' },
  ]);
});

test('joins multi-line data and preserves incomplete frames', () => {
  const parser = new SSEParser();
  expect(parser.push('data: one\ndata: two\n\ndata: thr')).toEqual([{ event: 'message', data: 'one\ntwo' }]);
  expect(parser.push('ee\n\n')).toEqual([{ event: 'message', data: 'three' }]);
});
