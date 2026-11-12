"""python -m gameatlasharness.run --config run.json [--check | --resume | --retry-failed]."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import fcntl
import hashlib
from http.server import ThreadingHTTPServer
import json
import math
import os
from pathlib import Path
import re
import secrets
import signal
import threading
import time
import tomllib
import traceback
from urllib.parse import urlsplit

from .agent import Agent
from .rendering import supported as rendering_supported, configuration as rendering_configuration
from .stages import stage_plan, execute_stages, DockerStageBackend
from .environment import Environment, docker, image_id, STOP, prepare, fingerprint, task_images
from .evaluator import coordinator, final_score, Handler, load_tasks

def save(path, value):
    temporary = path.with_suffix('.tmp')
    with temporary.open('w') as handle:
        json.dump(value, handle, indent=2, allow_nan=False)
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)


def load_config(path):
    config = json.loads(path.read_text())
    allowed = {'tasks_dir', 'tasks', 'output', 'concurrency', 'runtime_volume', 'model',
               'base_url', 'api_key_env', 'context_window', 'max_tokens', 'thinking',
               'vision', 'thinking_level_map', 'chat_template_kwargs', 'api', 'auth_header',
               'reasoning_history_field', 'reserve_tokens', 'keep_recent_tokens', 'request_timeout_sec'}
    unknown = set(config) - allowed
    if unknown:
        raise ValueError('Unknown config fields: ' + ', '.join(sorted(unknown)))
    for name in ('tasks_dir', 'output'):
        config[name] = str((path.parent / config[name]).resolve())
    for name, default in [('concurrency', 1), ('context_window', 262144), ('max_tokens', 65536)]:
        value = config.setdefault(name, default)
        if type(value) is not int or value <= 0:
            raise ValueError(f'{name} must be a positive integer')
    config.setdefault('api_key_env', 'OPENAI_API_KEY')
    if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', config['api_key_env']):
        raise ValueError('Invalid API key environment variable name')
    api = config.setdefault('api', 'openai-completions')
    if api not in {'openai-completions', 'openai-responses', 'anthropic-messages'}:
        raise ValueError('Unsupported model API')
    auth = config.setdefault('auth_header', 'x-api-key' if api == 'anthropic-messages' else 'authorization')
    if auth not in {'authorization', 'api-key', 'x-api-key'}:
        raise ValueError('Unsupported auth_header')
    if api != 'openai-completions' and (config.get('chat_template_kwargs') or config.get('reasoning_history_field')):
        raise ValueError('Chat-specific options require openai-completions')
    config.setdefault('thinking', 'off')
    if config['thinking'] not in {'off', 'minimal', 'low', 'medium', 'high', 'xhigh'}:
        raise ValueError('Use a Pi thinking level; map it to provider-specific levels explicitly')
    if not isinstance(config.get('model'), str) or not config['model'].strip():
        raise ValueError('model is required')
    url = urlsplit(config['base_url'])
    if url.scheme not in {'http', 'https'} or not url.hostname or url.username or url.password or url.query or url.fragment:
        raise ValueError('base_url must be an HTTP(S) URL without credentials/query/fragment')
    reserve = config.get('reserve_tokens', 36864)
    if not 0 < reserve < config['context_window'] or not 0 < config['max_tokens'] < config['context_window']:
        raise ValueError('Invalid context/output/compaction limits')
    if not 0 < config.get('keep_recent_tokens', 20000) < config['context_window'] - reserve:
        raise ValueError('Invalid compaction keep_recent_tokens')
    names = config.get('tasks')
    if names is None:
        names = []
        for task_file in sorted(Path(config['tasks_dir']).glob('*/task.toml')):
            spec = tomllib.loads(task_file.read_text())
            if spec.get('harness', {}).get('contract') == 'local-v1' or rendering_supported(task_file.parent, spec):
                names.append(task_file.parent.name)
        config['tasks'] = names
    if not isinstance(names, list) or not names:
        raise ValueError('No runnable tasks: maintainers must add reviewed local-v1 tasks, or set tasks explicitly')
    if any(not isinstance(n, str) or not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_.-]*', n) for n in names):
        raise ValueError('Invalid task name')
    if len(names) != len(set(names)):
        raise ValueError('tasks must not contain duplicates')
    config.setdefault('runtime_volume', 'gamebench-core-pi-0842-amd64')
    if not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_.-]*', config['runtime_volume']):
        raise ValueError('Invalid runtime_volume')
    return config


def preflight(config):
    root = Path(config['tasks_dir'])
    if root.name != 'tasks':
        raise ValueError('Task collection must be a directory named tasks')
    output = Path(config['output'])
    if output.is_relative_to(root):
        raise ValueError('Output must be outside tasks')
    specs, hashes, images, resources = {}, {}, {}, []
    scoring = load_tasks(root.parent, frozenset(config['tasks']))
    for name in config['tasks']:
        task = root / name
        spec = tomllib.loads((task / 'task.toml').read_text())
        render = rendering_supported(task, spec)
        if spec.get('steps'):
            if not render:
                raise ValueError(f'{name}: multi-stage tasks require a reviewed stage adapter; refusing to flatten stages')
            stage_plan(task, spec)
        if spec.get('harness', {}).get('contract') != 'local-v1' and not render:
            raise ValueError(f'{name}: review the local-v1 environment contract before enabling this task')
        if not spec.get('steps') and not (task / 'instruction.md').is_file():
            raise ValueError(f'{name}: missing instruction.md')
        if not scoring[name].strict_work_isolation:
            raise ValueError(f'{name}: strict_work_isolation is required')
        env = spec['environment']
        if env.get('gpus', 0) or env.get('mcp_servers'):
            raise ValueError(f'{name}: GPU/MCP environment not implemented')
        if env.get('workdir', '/app/workspace') != '/app/workspace':
            raise ValueError(f'{name}: expected /app/workspace')
        for value in (env['cpus'], env['memory_mb'], spec['agent']['timeout_sec'],
                      scoring[name].cpus, scoring[name].memory_mb,
                      scoring[name].apply_timeout_sec, scoring[name].score_timeout_sec):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                raise ValueError(f'{name}: invalid resource/time limit')
        hashes[name] = fingerprint(task)
        refs = task_images(task, spec, hashes[name])
        images[name] = {role: image_id(ref) for role, ref in refs.items()}
        resources.append(int(env['memory_mb']) + scoring[name].memory_mb + 512)
        specs[name] = spec
        specs[name]['environment']['docker_image'] = images[name]['agent']
    docker('volume', 'inspect', config['runtime_volume'])
    engine = json.loads(docker('info', '--format', '{{json .}}').stdout)
    needed = sum(sorted(resources, reverse=True)[:config['concurrency']]) * 1024**2
    if needed > engine['MemTotal'] * .9:
        raise ValueError('Configured concurrency exceeds Docker memory capacity including scoring; reduce concurrency')
    # Runtime must actually execute in each image (architecture and libc matter).
    for image in {s['environment']['docker_image'] for s in specs.values()}:
        result = docker('run', '--rm', '--network', 'none', '--entrypoint', '/bin/sh',
                        '-v', config['runtime_volume'] + ':/opt/node:ro', image,
                        '-c', 'export PATH=/opt/node/bin:$PATH; python3 --version && pi --version', timeout=90)
        if result.stdout.strip().splitlines()[-1] != '0.84.2':
            raise ValueError('Expected Pi 0.84.2 runtime')
    runtime_hash = docker('run', '--rm', '--network', 'none', '--entrypoint', '/bin/sh',
                          '-v', config['runtime_volume'] + ':/opt/node:ro', image,
                          '-c', 'find /opt/node -type f -exec sha256sum {} + | sort | sha256sum',
                          timeout=120).stdout.split()[0]
    code_hash = hashlib.sha256()
    for path in sorted(Path(__file__).parent.rglob('*.py')):
        if 'tests' not in path.parts:
            code_hash.update(str(path.relative_to(Path(__file__).parent)).encode() + path.read_bytes())
    return specs, {'config': config, 'task_sha256': hashes, 'images': images,
                   'docker_version': engine['ServerVersion'], 'pi_version': '0.84.2',
                   'runtime_sha256': runtime_hash, 'harness_sha256': code_hash.hexdigest()}


def trial(config, name, spec, folder, provenance):
    if STOP.is_set():
        return {'task': name, 'status': 'not_started'}
    folder.mkdir(parents=True, exist_ok=False)
    for child in ['agent', 'audit']:
        (folder / child).mkdir(mode=0o777)
        (folder / child).chmod(0o777)
    (folder / 'task-id').write_text(name + '\n')
    ident = 'gb-' + secrets.token_hex(8)
    task = Path(config['tasks_dir']) / name
    env = Environment(task, spec, folder, ident, config['runtime_volume'])
    receipt = {'task': name, 'status': 'starting', 'score': None, 'run_id': ident, 'started_at': time.time()}
    save(folder / 'result.json', receipt)
    server, server_thread, service = None, None, None
    phase = 'environment'
    try:
        if fingerprint(task) != provenance['task_sha256'][name]:
            raise RuntimeError('Task changed after preflight')
        cid = env.start()
        phase = 'coordinator'
        token = secrets.token_hex(32)
        service = coordinator(task.parent.parent, folder, name, token)
        from dataclasses import replace
        service.tasks[name] = replace(service.tasks[name], verifier_image=provenance['images'][name]['verifier'])
        server = ThreadingHTTPServer(('0.0.0.0', 0), Handler)
        server.coordinator = service
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        env.relay('submit', {
            'GAMEBENCH_RELAY_HOST': 'host.docker.internal',
            'GAMEBENCH_RELAY_HTTP_HOST': 'host.docker.internal',
            'GAMEBENCH_RELAY_PORT': str(server.server_port),
            'GAMEBENCH_RELAY_TLS': '0', 'LLM_PROXY_API_KEY': token})
        url = urlsplit(config['base_url'])
        env.relay('model', {
            'GAMEBENCH_RELAY_HOST': url.hostname,
            'GAMEBENCH_RELAY_PORT': str(url.port or (443 if url.scheme == 'https' else 80)),
            'GAMEBENCH_RELAY_TLS': str(int(url.scheme == 'https')),
            'GAMEBENCH_RELAY_API': config['api'],
            'GAMEBENCH_RELAY_AUTH_HEADER': config['auth_header'],
            'GAMEBENCH_RELAY_UPSTREAM_PATH': url.path.rstrip('/') + {
                'openai-completions': '/chat/completions',
                'openai-responses': '/responses',
                'anthropic-messages': '/messages'}[config['api']],
            'LLM_PROXY_API_KEY': os.environ[config['api_key_env']]}, audit=True)
        instruction = (task / 'instruction.md').read_text() if not spec.get('steps') else None
        receipt['status'] = 'running'
        save(folder / 'result.json', receipt)
        phase = 'agent'
        staged = None
        if spec.get('steps'):
            staged = execute_stages(stage_plan(task, spec), DockerStageBackend(
                config, task, spec, env, service, provenance['images'][name]['verifier']))
            terminal = staged['stages'][-1]['agent_status']
        else:
            if rendering_supported(task, spec):
                service.tasks[name] = rendering_configuration(service.tasks[name], folder / 'render-online')
            terminal = Agent(config).run(env, instruction, spec['agent']['timeout_sec'])
        audit = folder / 'audit/requests.jsonl'
        last_request = None
        if audit.exists():
            with audit.open() as handle:
                for line in handle:
                    last_request = json.loads(line)
        if terminal == 'finished' and (not last_request or last_request.get('output', {}).get('error')):
            terminal = 'agent_error'
        receipt['agent_status'] = terminal
        server.shutdown()
        # Do not race an online evaluation against the final snapshot/cleanup.
        deadline = time.monotonic() + service.tasks[name].apply_timeout_sec + service.tasks[name].score_timeout_sec + 60
        while any(s.in_flight for s in service._states.values()):
            if time.monotonic() > deadline:
                raise RuntimeError('Online evaluator did not settle before final scoring')
            time.sleep(.2)
        save(folder / 'container.json', env.inspect())
        phase = 'final_verifier'
        final = staged if staged is not None else final_score(task.parent.parent, folder, name, cid, provenance['images'][name]['verifier'])
        if fingerprint(task) != provenance['task_sha256'][name]:
            raise RuntimeError('Task changed during execution')
        receipt['score'] = final['score']
        receipt['status'] = {'finished': 'completed', 'timeout': 'timeout_scored',
                             'agent_error': 'agent_error_scored'}[terminal]
        receipt['valid_for_ranking'] = terminal in {'finished', 'timeout'} and (staged is None or staged['valid_for_ranking'])
        save(folder / 'final-score.json', final)
    except Exception as error:
        receipt.update(status='interrupted' if isinstance(error, InterruptedError) else 'infra_error', error_type=type(error).__name__,
                       error_category=getattr(error, 'category', None), error_phase=phase, valid_for_ranking=False)
        receipt['error_frames'] = [{'file': Path(f.filename).name, 'line': f.lineno, 'function': f.name}
                                   for f in traceback.extract_tb(error.__traceback__)]
        # Error messages may include provider secrets. Keep types/categories only.
        if env.cid:
            try:
                save(folder / 'container.json', env.inspect())
            except Exception:
                pass
    finally:
        if server:
            server.shutdown()
            server.server_close()
        if service:
            deadline = time.monotonic() + service.tasks[name].apply_timeout_sec + service.tasks[name].score_timeout_sec + 60
            while any(s.in_flight for s in service._states.values()) and time.monotonic() < deadline:
                time.sleep(.2)
            if any(s.in_flight for s in service._states.values()):
                receipt['evaluator_cleanup_pending'] = True
                receipt['valid_for_ranking'] = False
        if server_thread:
            server_thread.join(timeout=5)
        receipt['cleanup_remaining'] = env.close()
        receipt['finished_at'] = time.time()
        save(folder / 'result.json', receipt)
    return receipt


def main(argv=None):
    STOP.clear()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: STOP.set())
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--prepare', action='store_true', help='Prepare the pinned Pi runtime and build the selected task images; no model requests')
    parser.add_argument('--task', action='append', help='Run only this task (repeatable); default: all tasks in the config')
    parser.add_argument('--output', type=Path, help='Output directory, relative to the current directory')
    parser.add_argument('--check', action='store_true')
    parser.add_argument('--resume', action='store_true')
    parser.add_argument('--retry-failed', action='store_true')
    args = parser.parse_args(argv)
    config = load_config(args.config.resolve())
    if args.task:
        if not set(args.task) <= set(config['tasks']) or len(args.task) != len(set(args.task)):
            raise ValueError('--task must select unique tasks from the configured collection')
        config['tasks'] = args.task
    if args.output:
        config['output'] = str(args.output.resolve())
    if args.prepare:
        prepare(config)
    specs, manifest = preflight(config)
    if args.check or args.prepare:
        print(json.dumps({'preflight': 'passed', 'tasks': len(specs)}))
        return 130 if STOP.is_set() else 0
    if not os.environ.get(config['api_key_env']):
        raise ValueError('Set the configured API key environment variable')
    output = Path(config['output'])
    output.mkdir(parents=True, exist_ok=True)
    with (output / '.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        path = output / 'manifest.json'
        if path.exists():
            if not (args.resume or args.retry_failed):
                raise ValueError('Output exists; use --resume or a new output directory')
            if json.loads(path.read_text()) != manifest:
                raise ValueError('Configuration, task, image or runtime changed; use a new output directory')
        else:
            save(path, manifest)
        jobs = []
        for name in config['tasks']:
            attempts = sorted((output / name).glob('attempt-*'))
            if attempts:
                last = json.loads((attempts[-1] / 'result.json').read_text())
                if last['status'] in {'starting', 'running'} or last.get('cleanup_remaining') or last.get('evaluator_cleanup_pending'):
                    raise ValueError('Unfinished attempt: inspect/clean its recorded run_id before retrying')
                if not args.retry_failed or last.get('valid_for_ranking'):
                    continue
            jobs.append((name, output / name / f'attempt-{len(attempts)+1:03d}'))
        with ThreadPoolExecutor(max_workers=config['concurrency']) as executor:
            futures = [executor.submit(trial, config, n, specs[n], f, manifest) for n, f in jobs]
            failed = False
            for future in futures:
                result = future.result()
                failed |= not result.get('valid_for_ranking', False) or bool(result.get('cleanup_remaining'))
                print(json.dumps({k: result.get(k) for k in ['task', 'status', 'score', 'error_type']}), flush=True)
        return 130 if STOP.is_set() else (1 if failed else 0)


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (ValueError, FileNotFoundError, BlockingIOError) as error:
        raise SystemExit(str(error))
