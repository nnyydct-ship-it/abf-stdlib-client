"""Offline integration checks for the free MCP reader; no production calls.

The loopback server exercises real urllib requests, response framing and the
MCP handshake. It never proxies traffic or invokes an x402 endpoint.
"""
import contextlib
import io
import json
import threading
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

import abf


def rpc_reply(request, result=None, error=None):
    out = {'jsonrpc': '2.0', 'id': request['id']}
    out['error' if error is not None else 'result'] = error if error is not None else result
    return out


def initialize(request, version='2025-03-26'):
    return rpc_reply(request, {'protocolVersion': version, 'capabilities': {'tools': {}},
                               'serverInfo': {'name': 'offline-fixture', 'version': '1'}})


def event(value, multiline=False):
    lines = json.dumps(value, indent=2 if multiline else None).splitlines()
    return ''.join('data: ' + line + '\r\n' for line in lines) + '\r\n'


@contextlib.contextmanager
def offline_server(replies):
    calls, failures = [], []
    queue = list(replies)

    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'

        def log_message(self, *args):
            pass

        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            calls.append({'path': self.path, 'body': request,
                          'headers': {k.lower(): v for k, v in self.headers.items()}})
            if not queue:
                failures.append('Unexpected extra request')
                status, headers, body = 500, {}, b'fixture exhausted'
            else:
                spec = queue.pop(0)
                try:
                    value = spec(request) if callable(spec) else spec
                    if isinstance(value, tuple):
                        status, headers, body = value
                    else:
                        status, headers, body = 200, {'Content-Type': 'application/json'}, json.dumps(value).encode()
                except Exception as exc:
                    failures.append(type(exc).__name__)
                    status, headers, body = 500, {}, b'fixture failed'
            if isinstance(body, str):
                body = body.encode()
            self.send_response(status)
            for k, v in headers.items():
                self.send_header(k, v)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Connection', 'close')
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    endpoint = 'http://127.0.0.1:%d/mcp' % server.server_port
    try:
        with mock.patch.object(abf, 'ENDPOINT', endpoint):
            transport = abf.HTTPTransport()
            # Local fixtures must not inherit a user's external proxy settings.
            transport.opener = urllib.request.build_opener(abf.NoRedirect, urllib.request.ProxyHandler({}))
            yield abf.MCPClient(transport), calls
        if failures:
            raise AssertionError(failures)
        if queue:
            raise AssertionError('Expected requests did not occur: %d' % len(queue))
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


class MCPWireTests(unittest.TestCase):
    def test_json_handshake_notification_version_session_and_single_initialization(self):
        def first(request):
            return 200, {'Content-Type': 'application/json', 'Mcp-Session-Id': 'offline-session'}, json.dumps(initialize(request))

        fixtures = [first, (202, {}, b''),
                    lambda req: rpc_reply(req, {'tools': [{'name': 'list_bounties'}]}),
                    lambda req: rpc_reply(req, {'structuredContent': {'stats': {'items': 9}}})]
        with offline_server(fixtures) as (client, calls):
            self.assertEqual(client.tools()['tools'][0]['name'], 'list_bounties')
            self.assertEqual(client.call_tool('get_feed_stats'), {'stats': {'items': 9}})
            self.assertEqual([x['body']['method'] for x in calls],
                             ['initialize', 'notifications/initialized', 'tools/list', 'tools/call'])
            self.assertNotIn('id', calls[1]['body'])
            self.assertIsInstance(calls[0]['body']['params']['capabilities'], dict)
            self.assertTrue(calls[0]['body']['params']['clientInfo']['name'])
            for call in calls[1:]:
                self.assertEqual(call['headers']['mcp-protocol-version'], '2025-03-26')
                self.assertEqual(call['headers']['mcp-session-id'], 'offline-session')
                self.assertNotIn('payment-signature', call['headers'])
                self.assertNotIn('authorization', call['headers'])

    def test_sse_comments_notifications_unrelated_id_and_multiline_json(self):
        expected = {'items': [{'id': 'fixture', 'title': '合成任务', 'reward_basis': 'single_winner'}]}

        def stream(request):
            text = ': heartbeat\r\nevent: message\r\n'
            text += event({'jsonrpc': '2.0', 'method': 'notifications/message', 'params': {'level': 'info', 'data': 'fixture'}})
            text += event({'jsonrpc': '2.0', 'id': request['id'] + 1000, 'result': {'unrelated': True}})
            text += event(rpc_reply(request, {'content': [{'type': 'text', 'text': json.dumps(expected)}]}), True)
            return 200, {'Content-Type': 'text/event-stream; charset=utf-8'}, text

        with offline_server([initialize, (202, {}, b''), stream]) as (client, calls):
            self.assertEqual(client.call_tool('list_bounties'), expected)
            self.assertEqual(len(calls), 3)

    def test_json_batch_notification_does_not_hide_matching_reply(self):
        def batch(request):
            return [{'jsonrpc': '2.0', 'method': 'notifications/message', 'params': {}},
                    rpc_reply(request, {'tools': []})]
        with offline_server([initialize, (204, {}, b''), batch]) as (client, _):
            self.assertEqual(client.tools(), {'tools': []})

    def test_structured_content_preferred_over_text_json(self):
        result = {'structuredContent': {'items': [{'id': 'structured'}]},
                  'content': [{'type': 'text', 'text': '{"items":[{"id":"fallback"}]}'}]}
        with offline_server([initialize, (202, {}, b''), lambda req: rpc_reply(req, result)]) as (client, _):
            self.assertEqual(client.call_tool('list_bounties')['items'][0]['id'], 'structured')

    def test_text_json_fallback_skips_plain_text_and_non_text_blocks(self):
        result = {'content': [{'type': 'text', 'text': 'An explanatory prefix'},
                              {'type': 'image', 'data': '', 'mimeType': 'image/png'},
                              {'type': 'text', 'text': '{"stats":{"items":4}}'}]}
        with offline_server([initialize, (202, {}, b''), lambda req: rpc_reply(req, result)]) as (client, _):
            self.assertEqual(client.call_tool('get_feed_stats'), {'stats': {'items': 4}})

    def test_tool_error_wins_over_any_structured_content(self):
        result = {'isError': True, 'structuredContent': {'items': [{'id': 'must-not-use'}]},
                  'content': [{'type': 'text', 'text': 'Tool refused the request'}]}
        with offline_server([initialize, (202, {}, b''), lambda req: rpc_reply(req, result)]) as (client, _):
            with self.assertRaises(abf.ClientError) as caught:
                client.call_tool('list_bounties')
            self.assertEqual(caught.exception.code, 'tool_error')

    def test_unsupported_negotiated_version_stops_before_initialized_or_tools(self):
        with offline_server([lambda req: initialize(req, '2099-01-01')]) as (client, calls):
            with self.assertRaises(abf.ClientError) as caught:
                client.tools()
            self.assertEqual(caught.exception.code, 'unsupported_protocol')
            self.assertEqual(len(calls), 1)

    def test_http_402_and_429_stop_without_retry_or_payment(self):
        for status in (402, 429):
            with self.subTest(status=status):
                with offline_server([initialize, (202, {}, b''),
                    (status, {'Content-Type': 'application/json', 'Retry-After': '17'}, '{"error":"fixture"}')]) as (client, calls):
                    with self.assertRaises(abf.ClientError) as caught:
                        client.call_tool('list_bounties')
                    self.assertEqual(caught.exception.code, 'http_' + str(status))
                    self.assertEqual(caught.exception.retry_after, '17')
                    self.assertEqual(len(calls), 3)
                    self.assertTrue(all('payment-signature' not in c['headers'] for c in calls))

    def test_redirect_is_not_followed(self):
        with offline_server([(302, {'Location': '/not-the-mcp-endpoint'}, b'')]) as (client, calls):
            with self.assertRaises(abf.ClientError) as caught:
                client.tools()
            self.assertEqual(caught.exception.code, 'redirect_refused')
            self.assertEqual([c['path'] for c in calls], ['/mcp'])

    def test_rpc_error_is_not_successful_tool_data(self):
        with offline_server([initialize, (202, {}, b''),
             lambda req: rpc_reply(req, error={'code': -32602, 'message': 'Invalid fixture params'})]) as (client, _):
            with self.assertRaises(abf.ClientError) as caught:
                client.tools()
            self.assertEqual(caught.exception.code, 'rpc_-32602')

    def test_mismatched_and_duplicate_response_ids_rejected(self):
        for duplicate in (False, True):
            def answer(req):
                one = rpc_reply(req, {'tools': []})
                if duplicate:
                    return [one, one]
                one['id'] = req['id'] + 42
                return one
            with self.subTest(duplicate=duplicate):
                with offline_server([initialize, (202, {}, b''), answer]) as (client, _):
                    with self.assertRaises(abf.ClientError) as caught:
                        client.tools()
                    self.assertEqual(caught.exception.code, 'rpc_mismatch')

    def test_boolean_rpc_id_cannot_match_numeric_request_id(self):
        def boolean_id(req):
            answer = rpc_reply(req, {'protocolVersion': '2025-06-18'})
            answer['id'] = True
            return answer
        # JSON-RPC permits string/number/null IDs, not booleans. Python True==1
        # must not turn a malformed server envelope into our request's reply.
        with offline_server([boolean_id]) as (client, _):
            with self.assertRaises(abf.ClientError) as caught:
                client.rpc('initialize', {'protocolVersion': abf.VERSIONS[0], 'capabilities': {},
                                         'clientInfo': {'name': 'offline-fixture', 'version': '1'}})
            self.assertEqual(caught.exception.code, 'rpc_mismatch')

    def test_http_error_response_body_is_closed(self):
        for status in (402, 429):
            with self.subTest(status=status):
                body = io.BytesIO(b'fixture rejection')
                error = urllib.error.HTTPError(abf.ENDPOINT, status, 'fixture', {'Retry-After': '5'}, body)
                transport = abf.HTTPTransport()
                transport.opener = mock.Mock()
                transport.opener.open.side_effect = error
                with self.assertRaises(abf.ClientError):
                    transport.post({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'})
                self.assertEqual(transport.opener.open.call_count, 1)
                self.assertTrue(body.closed, 'HTTP error response leaked its body/socket')


class DiscoverySemanticsTests(unittest.TestCase):
    def test_limits_and_minimum_validate_before_any_network_use(self):
        parser = abf.build_parser()
        for n in ('1', '100'):
            self.assertEqual(parser.parse_args(['list', '--limit', n]).limit, int(n))
        for argv in (['list', '--limit', '0'], ['list', '--limit', '101'],
                     ['list', '--limit', 'not-a-number'], ['list', '--min-usd', 'nan'],
                     ['list', '--min-usd', 'inf'], ['list', '--min-usd', '-1']):
            with self.subTest(argv=argv), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as caught:
                    parser.parse_args(argv)
                self.assertEqual(caught.exception.code, 2)

    def test_incremental_cursor_and_filter_arguments_remain_explicit(self):
        class RecordingClient:
            def call_tool(self, name, args):
                self.name, self.args = name, args
                return {'items': [], 'has_more': True, 'next_since': '2026-11-02T10:00:00Z', 'next_after_id': 'opaque-next'}
        client = RecordingClient()
        args = abf.build_parser().parse_args(['new', '--since', '2026-11-01T10:00:00+00:00',
              '--after-id', 'opaque-input', '--limit', '7', '--source', 'direct', '--query', 'minimal client'])
        result = abf.compact(abf.run(args, client))
        self.assertEqual(client.name, 'get_new_bounties')
        self.assertEqual(client.args['since'], args.since)
        self.assertEqual(client.args['after_id'], 'opaque-input')
        self.assertEqual(client.args['limit'], 7)
        self.assertEqual(client.args['q'], 'minimal client')
        self.assertTrue(result['has_more'])
        self.assertEqual(result['next_after_id'], 'opaque-next')
        self.assertEqual(result['next_since'], '2026-11-02T10:00:00Z')

    def test_invalid_or_naive_since_stops_before_tool_call(self):
        for since in ('yesterday', '2026-11-02T10:00:00'):
            client = mock.Mock()
            args = abf.build_parser().parse_args(['new', '--since', since])
            with self.assertRaises(abf.ClientError) as caught:
                abf.run(args, client)
            self.assertEqual(caught.exception.code, 'invalid_since')
            client.call_tool.assert_not_called()

    def test_compact_preserves_pool_per_worker_and_unknown_reward_semantics(self):
        item = {'id': 'pool', 'title': 'One shared reward pool', 'reward': '0.20', 'currency': 'USDC',
                'est_usd': 0.20, 'network': 'base', 'reward_basis': 'shared_pool',
                'pool_amount': 0.20, 'per_worker_usd': None, 'requires': ['github'], 'slots_left': 0}
        source = {'items': [item], 'has_more': False, 'next_since': 'time', 'next_after_id': None}
        output = abf.compact(source)
        for key, value in item.items():
            self.assertEqual(output['items'][0][key], value)
        self.assertIsNone(output['items'][0]['per_worker_usd'])
        self.assertEqual(output['items'][0]['reward_basis'], 'shared_pool')
        self.assertEqual(output['items'][0]['slots_left'], 0)
        self.assertIsNone(output['next_after_id'])


if __name__ == '__main__':
    unittest.main()
