"""Fixed-destination model/feedback relays and the Work loopback bridge.

Runs in a trusted container. Credentials and the append-only request log are
not mounted in work. No harness patches or inferred/reconstructed input turns.
"""
import argparse
import socket
import ssl
import http.client
from http.server import BaseHTTPRequestHandler
import json
import os
from pathlib import Path
import socketserver
import threading
import time

AUDIT_PATH = Path('/audit/requests.jsonl')
# Byte budget is independent of the model's token context window.
MAX_REQUEST_BYTES = int(os.environ.get('GAMEBENCH_RELAY_MAX_REQUEST_BYTES', 256 * 1024 * 1024))
if MAX_REQUEST_BYTES <= 0:
    raise ValueError('GAMEBENCH_RELAY_MAX_REQUEST_BYTES must be positive')


def consume_sse(pending, chunks):
    """Parse complete SSE lines and stop at the protocol terminator, not EOF."""
    while b'\n' in pending:
        line, pending = pending.split(b'\n', 1)
        if line.startswith(b'data:'):
            event = line[5:].strip()
            if event == b'[DONE]':
                return pending, True
            if event:
                data = json.loads(event)
                chunks.append(data)
                if isinstance(data, dict) and data.get('type') in ('response.completed', 'response.failed', 'response.incomplete', 'message_stop'):
                    return pending, True
    return pending, False


def assemble_anthropic(chunks):
    message = {'role': 'assistant', 'content': ''}
    output = {'message': message, 'raw_events': chunks}
    blocks = {}
    usage = {}
    stop = None
    for event in chunks:
        kind = event.get('type')
        if kind in ('message_start', 'message'):
            native = event.get('message', event)
            output.update(model=native.get('model'), request_id=native.get('id'))
            usage.update(native.get('usage') or {})
            if kind == 'message':
                blocks = dict(enumerate(native.get('content', [])))
                stop = native.get('stop_reason')
        elif kind == 'content_block_start':
            blocks[event['index']] = dict(event['content_block'])
        elif kind == 'content_block_delta':
            block = blocks[event['index']]
            for key in ('text', 'thinking', 'signature', 'partial_json'):
                if key in event['delta']:
                    block[key] = block.get(key, '') + event['delta'][key]
        elif kind == 'message_delta':
            stop = event.get('delta', {}).get('stop_reason', stop)
            usage.update(event.get('usage') or {})
        elif kind == 'error':
            output['error'] = event.get('error', 'anthropic_error')
    content = [dict(blocks[k]) for k in sorted(blocks)]
    calls = []
    for block in content:
        if block['type'] == 'text':
            message['content'] += block.get('text', '')
        elif block['type'] == 'thinking':
            message['reasoning_content'] = message.get('reasoning_content', '') + block.get('thinking', '')
        elif block['type'] == 'tool_use':
            if 'partial_json' in block:
                block['input'] = json.loads(block.pop('partial_json'))
            calls.append({'id': block['id'], 'type': 'function', 'function': {
                'name': block['name'], 'arguments': json.dumps(block.get('input', {}))}})
    if calls:
        message['tool_calls'] = calls
    output.update(usage=usage, anthropic_content=content, stop_reason=stop)
    if stop:
        output['finish_reason'] = {'end_turn': 'stop', 'stop_sequence': 'stop',
                                   'tool_use': 'tool_calls', 'max_tokens': 'length'}.get(stop, stop)
    return output


def assemble(chunks):
    if any(c.get('type') in ('message_start', 'message', 'content_block_start', 'message_stop') for c in chunks if isinstance(c, dict)):
        return assemble_anthropic(chunks)
    terminal = next((c.get('response') for c in reversed(chunks) if isinstance(c, dict) and c.get('type') in ('response.completed','response.failed','response.incomplete')), None)
    if terminal is not None:
        message = {'role': 'assistant', 'content': ''}
        calls = []
        for item in terminal.get('output', []):
            if item.get('type') == 'message':
                message['content'] += ''.join(c.get('text','') for c in item.get('content',[]) if c.get('type') == 'output_text')
            elif item.get('type') == 'function_call':
                calls.append({'id':item['call_id'],'type':'function','function':{'name':item['name'],'arguments':item['arguments']}})
        if calls: message['tool_calls'] = calls
        result = {'message':message,'model':terminal.get('model'),'usage':terminal.get('usage'),'request_id':terminal.get('id'),'responses_output':terminal.get('output'),'reasoning':terminal.get('reasoning'),'raw_events':chunks}
        if terminal.get('status') == 'completed': result['finish_reason'] = 'tool_calls' if calls else 'stop'
        else: result['error'] = terminal.get('error') or terminal.get('incomplete_details') or terminal.get('status')
        return result
    message = {'role': 'assistant', 'content': ''}
    tools = {}
    output = {'message': message}
    for data in chunks:
        if not isinstance(data, dict):
            output['error'] = 'invalid_upstream_json'
            continue
        for field in ('model', 'usage'):
            if data.get(field) is not None:
                output[field] = data[field]
        if data.get('id'):
            output['request_id'] = data['id']
        if data.get('error'):
            output['error'] = data['error']
        for choice in data.get('choices') or []:
            if choice.get('index', 0) != 0:
                continue
            delta = choice.get('delta') or choice.get('message') or {}
            for field in ('content', 'reasoning_content'):
                if isinstance(delta.get(field), str):
                    message[field] = message.get(field, '') + delta[field]
            for call in delta.get('tool_calls') or []:
                index = call.get('index', len(tools))
                target = tools.setdefault(index, {'index': index, 'type': 'function',
                                                  'function': {'name': '', 'arguments': ''}})
                if call.get('id'):
                    target['id'] = call['id']
                for field in ('name', 'arguments'):
                    value = (call.get('function') or {}).get(field)
                    if isinstance(value, str):
                        target['function'][field] += value
            if choice.get('finish_reason') is not None:
                output['finish_reason'] = choice['finish_reason']
    if tools:
        message['tool_calls'] = [tools[index] for index in sorted(tools)]
    return output


class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    lock = threading.Lock()


class ModelHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def reject_request(self, status, code, message):
        # Close without reading a rejected body; never treat its bytes as another request.
        body = json.dumps({'error': {'type': 'invalid_request_error', 'code': code,
                                   'message': message}}).encode()
        self.close_connection = True
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Connection', 'close')
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        expected_path = {'openai-responses': '/v1/responses', 'anthropic-messages': '/v1/messages'}.get(os.environ.get('GAMEBENCH_RELAY_API'), '/v1/chat/completions')
        if self.path != expected_path:
            self.send_error(403)
            return
        try:
            if self.headers.get('Transfer-Encoding'):
                raise ValueError('Content-Length is required; chunked requests are not supported')
            length = int(self.headers.get('Content-Length', '0'))
            if length <= 0:
                raise ValueError('Content-Length must be positive')
            if length > MAX_REQUEST_BYTES:
                self.reject_request(413, 'request_body_too_large',
                    f'Relay request body is {length} bytes; limit is {MAX_REQUEST_BYTES} bytes. '
                    'Configure GAMEBENCH_RELAY_MAX_REQUEST_BYTES on the trusted relay.')
                return
            body = self.rfile.read(length)
            if len(body) != length:
                raise ValueError('Incomplete request body')
            payload = json.loads(body)
        except (ValueError, UnicodeDecodeError):
            self.reject_request(400, 'invalid_request_body', 'Invalid or incomplete JSON request body')
            return
        started = time.monotonic()
        chunks = []
        failure = None
        tls = os.environ.get('GAMEBENCH_RELAY_TLS', '1') == '1'
        connection = http.client.HTTPSConnection if tls else http.client.HTTPConnection
        conn = connection(os.environ['GAMEBENCH_RELAY_HOST'],
                          port=int(os.environ.get('GAMEBENCH_RELAY_PORT', '443' if tls else '80')),
                          timeout=600)
        pending = b''
        response_body = bytearray()
        recorded = False

        def record():
            nonlocal recorded
            if recorded:
                return
            try:
                output = assemble(chunks)
            except Exception as error:
                # A parsing error must not silently erase an entire request.
                # Keep raw upstream chunks for diagnosis, not fabricated text.
                output = {'error': 'audit_parse_' + type(error).__name__,
                          'raw_chunks': chunks}
            output['duration_ms'] = round((time.monotonic() - started) * 1000, 2)
            if failure:
                output['error'] = failure
            elif not output.get('finish_reason') and not output.get('error'):
                output['error'] = 'missing_finish_reason'
            with self.server.lock:
                with AUDIT_PATH.open('a') as handle:
                    json.dump({'input': payload, 'output': output}, handle, ensure_ascii=False)
                    handle.write('\n')
            recorded = True

        try:
            auth = os.environ.get('GAMEBENCH_RELAY_AUTH_HEADER', 'authorization')
            headers = {auth: ('Bearer ' if auth == 'authorization' else '') + os.environ['LLM_PROXY_API_KEY'],
                       'Content-Type': 'application/json', 'Connection': 'close'}
            if os.environ.get('GAMEBENCH_RELAY_API') == 'anthropic-messages':
                headers['anthropic-version'] = '2023-06-01'
                if self.headers.get('anthropic-beta'):
                    headers['anthropic-beta'] = self.headers['anthropic-beta']
            conn.request('POST', os.environ.get('GAMEBENCH_RELAY_UPSTREAM_PATH', self.path), body, headers)
            response = conn.getresponse()
            self.send_response(response.status)
            content_type = response.getheader('Content-Type', 'application/json')
            self.send_header('Content-Type', content_type)
            self.send_header('Connection', 'close')
            self.end_headers()
            streaming = 'text/event-stream' in content_type
            if response.status >= 400:
                failure = f'upstream_http_{response.status}'
            done = False
            while data := response.read1(65536):
                if streaming:
                    pending, done = consume_sse(pending + data, chunks)
                    if done:
                        # Persist before Pi sees [DONE] and the trial collects
                        # logs. Some upstream connections stay open afterwards.
                        record()
                else:
                    response_body.extend(data)
                self.wfile.write(data)
                self.wfile.flush()
                if done:
                    break
            if not streaming and response_body:
                chunks.append(json.loads(response_body))
            if streaming and not done and not failure:
                failure = 'incomplete_upstream_stream'
        except Exception as error:
            # Avoid recording exception strings containing upstream credentials.
            failure = type(error).__name__
        finally:
            conn.close()
            record()
            self.close_connection = True



def forward_initial_http_request(
    client: socket.socket,
    upstream: socket.socket,
) -> bool:
    if HTTP_HOST is None:
        return True
    request = bytearray()
    while b"\r\n\r\n" not in request:
        chunk = client.recv(64 * 1024)
        if not chunk:
            return False
        request.extend(chunk)
        if len(request) > 256 * 1024:
            return False
    header_end = request.index(b"\r\n\r\n")
    lines = request[:header_end].split(b"\r\n")
    rewritten = [lines[0]]
    saw_host = False
    saw_connection = False
    saw_authorization = False
    for line in lines[1:]:
        name, separator, _ = line.partition(b":")
        lowered = name.strip().lower()
        if separator and lowered == b"host":
            rewritten.append(b"Host: " + HTTP_HOST.encode("ascii"))
            saw_host = True
        elif separator and lowered == b"connection":
            rewritten.append(b"Connection: close")
            saw_connection = True
        elif separator and AUTH_BEARER is not None and lowered == b"authorization":
            rewritten.append(b"Authorization: Bearer " + AUTH_BEARER.encode("ascii"))
            saw_authorization = True
        elif separator and AUTH_BEARER is not None and lowered == b"x-api-key":
            rewritten.append(b"x-api-key: " + AUTH_BEARER.encode("ascii"))
        elif separator and lowered != b"proxy-connection":
            rewritten.append(line)
    if not saw_host:
        rewritten.append(b"Host: " + HTTP_HOST.encode("ascii"))
    if not saw_connection:
        rewritten.append(b"Connection: close")
    if AUTH_BEARER is not None and not saw_authorization:
        rewritten.append(b"Authorization: Bearer " + AUTH_BEARER.encode("ascii"))
    upstream.sendall(
        b"\r\n".join(rewritten) + b"\r\n\r\n" + request[header_end + 4 :]
    )
    return True


def pump(source: socket.socket, destination: socket.socket) -> None:
    try:
        while True:
            payload = source.recv(64 * 1024)
            if not payload:
                break
            destination.sendall(payload)
    except OSError:
        pass
    finally:
        try:
            destination.shutdown(socket.SHUT_WR)
        except OSError:
            pass


HTTP_HOST = os.environ.get('GAMEBENCH_RELAY_HTTP_HOST')
AUTH_BEARER = os.environ.get('LLM_PROXY_API_KEY')


def bridge(client, mode):
    upstream = None
    try:
        if mode == 'bridge':
            upstream = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            upstream.settimeout(30)
            upstream.connect('/run/gamebench/llm.sock')
        else:
            host = os.environ['GAMEBENCH_RELAY_HOST']
            upstream = socket.create_connection((host, int(os.environ['GAMEBENCH_RELAY_PORT'])), timeout=30)
            if not forward_initial_http_request(client, upstream):
                return
        client.settimeout(None)
        upstream.settimeout(None)
        thread = threading.Thread(target=pump, args=(client, upstream), daemon=True)
        thread.start()
        pump(upstream, client)
        thread.join(timeout=1)
    except OSError:
        pass
    finally:
        client.close()
        if upstream is not None:
            upstream.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=['model', 'submit', 'bridge'])
    mode = parser.parse_args().mode
    if AUTH_BEARER is not None and (not AUTH_BEARER.isascii() or '\r' in AUTH_BEARER or '\n' in AUTH_BEARER):
        parser.error('API key must be a single ASCII header value')
    if mode == 'model':
        path = Path('/run/gamebench/llm.sock')
        path.unlink(missing_ok=True)
        Path('/audit').mkdir(exist_ok=True)
        AUDIT_PATH.touch(exist_ok=True)
        with Server(str(path), ModelHandler) as server:
            path.chmod(0o666)
            server.serve_forever()
        return
    if mode == 'bridge':
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(('127.0.0.1', 8780))
    else:
        path = Path('/run/gamebench/submit.sock')
        path.unlink(missing_ok=True)
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(str(path))
        path.chmod(0o666)
    with listener:
        listener.listen(32)
        while True:
            client, _ = listener.accept()
            threading.Thread(target=bridge, args=(client, mode), daemon=True).start()


if __name__ == '__main__':
    main()
