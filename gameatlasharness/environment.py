"""Local Docker lifecycle. No platform service or host-wide cleanup."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time
import threading
import fcntl
import tomllib

MODULES = Path(__file__).parent
STOP = threading.Event()


class DockerError(RuntimeError):
    pass


def docker(*args, timeout=60, check=True, env=None):
    result = subprocess.run(['docker', *map(str, args)], capture_output=True,
                            text=True, timeout=timeout, env=env)
    if check and result.returncode:
        # Never interpolate argv or raw stderr: either can contain credentials.
        raise DockerError(f'Docker {args[0]} failed (exit {result.returncode})')
    return result


def image_id(image):
    return json.loads(docker('image', 'inspect', image).stdout)[0]['Id']


def fingerprint(task):
    digest = hashlib.sha256()
    for path in sorted(task.rglob('*')):
        if '__pycache__' in path.relative_to(task).parts:
            continue
        if path.is_symlink():
            raise ValueError(f'Task symlinks are not supported: {path.name}')
        if path.is_file():
            digest.update(str(path.relative_to(task)).encode() + b'\0')
            digest.update(bytes([bool(path.stat().st_mode & 0o111)]))
            with path.open('rb') as handle:
                if path.stat().st_size < 1024 and handle.readline(64).rstrip() == b'version https://git-lfs.github.com/spec/v1':
                    raise ValueError(f'Missing Git LFS asset: {path}. Run git lfs pull.')
                handle.seek(0)
                while block := handle.read(1024 * 1024):
                    digest.update(block)
    return digest.hexdigest()


def task_images(task, spec, digest):
    prefix = f'gamebench-local/{task.name}'
    work = f'{prefix}:work-{digest}'
    separate = spec.get('verifier', {}).get('environment_mode') == 'separate'
    return {'agent': work, 'verifier': f'{prefix}:verifier-{digest}' if separate else work}


def prepare(config):
    """One-time setup, using public sources; never overwrite an existing runtime."""
    lock_path = Path.home() / '.cache/gamebench/setup.lock'
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        volume = config['runtime_volume']
        if docker('volume', 'inspect', volume, check=False).returncode:
            runtime_image = 'gamebench-core/pi-runtime:0.84.2'
            print('Preparing Pi 0.84.2 runtime (first run only)…', flush=True)
            docker('build', '--platform', 'linux/amd64', '-f', MODULES / 'runtime.Dockerfile',
                   '-t', runtime_image, MODULES, timeout=1800)
            docker('volume', 'create', volume)
            try:
                docker('run', '--rm', '--platform', 'linux/amd64', '--network', 'none',
                       '-v', volume + ':/runtime', runtime_image, timeout=180)
            except Exception:
                docker('volume', 'rm', volume, check=False)
                raise
        for name in config['tasks']:
            task = Path(config['tasks_dir']) / name
            spec = tomllib.loads((task / 'task.toml').read_text())
            refs = task_images(task, spec, fingerprint(task))
            for role, ref in refs.items():
                if role == 'verifier' and ref == refs['agent']:
                    continue
                if docker('image', 'inspect', ref, check=False).returncode == 0:
                    continue
                context = task / ('environment' if role == 'agent' else 'tests')
                if not (context / 'Dockerfile').is_file():
                    raise ValueError(f'Missing {role} Dockerfile: {context}')
                print(f'Building {name} {role} image', flush=True)
                settings = spec['environment'] if role == 'agent' else spec['verifier']['environment']
                subprocess.run(['docker', 'build', '--platform', 'linux/amd64', '-t', ref,
                                str(context)], check=True, timeout=settings.get('build_timeout_sec', 7200))



class Environment:
    def __init__(self, task, config, output, name, runtime):
        self.task, self.config, self.output = task, config, output
        self.name, self.runtime = name, runtime
        self.containers, self.volumes = [], []
        self.cid = None

    def volume(self, suffix):
        name = self.name + '-' + suffix
        docker('volume', 'create', '--label', 'gamebench.run=' + self.name, name)
        self.volumes.append(name)
        return name

    def start(self):
        self.socket_volume = self.volume('sockets')
        env = self.config['environment']
        args = ['create', '--name', self.name, '--label', 'gamebench.run=' + self.name,
                '--label', 'com.docker.compose.service=main', '--label',
                'com.docker.compose.project.working_dir=' + str(self.task / 'environment'),
                '--network', 'none', '--read-only', '--cap-drop', 'ALL',
                '--security-opt', 'no-new-privileges', '--pids-limit', '512',
                '--cpus', str(env['cpus']), '--memory', str(env['memory_mb']) + 'm',
                '--user', '1000:1000', '--workdir', '/app/workspace',
                '--entrypoint', '/bin/sh']
        for target, size in [('/tmp', '2g'), ('/run', '64m'),
                             ('/home/agent', '512m'), ('/var/tmp', '256m')]:
            args += ['--tmpfs', f'{target}:rw,nosuid,nodev,size={size},mode=1777']
        from .rendering import TASKS
        for suffix, target in [('workspace', '/app/workspace'), ('output', '/app/output')]:
            volume = self.volume(suffix)
            if self.task.name in TASKS:
                # Explicit initializer below owns copy-up. Docker would otherwise
                # reset ownership of an empty output volume from the image.
                args += ['--mount', f'type=volume,source={volume},target={target},volume-nocopy']
            else:
                args += ['-v', volume + ':' + target]
        args += ['-v', self.runtime + ':/opt/node:ro',
                 '-v', self.socket_volume + ':/run/gamebench:ro']
        for source, target in [(MODULES / 'submit_score.py', '/usr/local/bin/submit_score'),
                               (MODULES / 'transport.py', '/usr/local/bin/gamebench-llm-bridge'),
                               (self.output / 'task-id', '/etc/gamebench-autoresearch-task'),
                               (self.output / 'agent', '/logs/agent')]:
            args += ['-v', str(source) + ':' + target + ('' if target == '/logs/agent' else ':ro')]
        values = dict(env.get('env', {}), HOME='/home/agent',
                      PATH='/opt/node/bin:/usr/local/bin:/usr/bin:/bin',
                      GAMEBENCH_VERIFY_SOCKET='/run/gamebench/submit.sock')
        for key, value in values.items():
            args += ['-e', str(key) + '=' + str(value)]
        from .rendering import TASKS
        if self.task.name in TASKS:
            # Legacy expert images contain root-owned workspace files. Initialize
            # only this run's named volumes; do not mutate the image or host data.
            volumes = [v for v in self.volumes if v.endswith(('-workspace', '-output'))]
            init_name = self.name + '-initialize'
            self.containers.append(init_name)
            init = ['run', '--name', init_name, '--rm', '--network', 'none', '--cap-drop', 'ALL',
                    '--cap-add', 'CHOWN', '--cap-add', 'FOWNER', '--user', '0:0',
                    '--entrypoint', '/bin/sh']
            for volume in volumes:
                target = '/app/workspace' if volume.endswith('-workspace') else '/app/output'
                init += ['-v', volume + ':' + target]
            docker(*init, env['docker_image'], '-c',
                   'chown -R 1000:1000 /app/workspace /app/output', timeout=180)
        self.containers.append(self.name)
        self.cid = docker(*args, env['docker_image'], '-c', 'while :; do sleep 3600; done').stdout.strip()
        docker('start', self.cid)
        self.exec('test -w /app/workspace && test -w /app/output && test -w /logs/agent')
        return self.cid

    def exec(self, command, timeout=60, user='1000:1000', env=None, check=True, interruptible=False):
        args = ['exec', '--user', user]
        for key in env or {}:
            args += ['-e', key]  # value is inherited, never placed in command-line arguments
        args += [self.cid, '/bin/bash', '-c', command]
        if interruptible:
            process = subprocess.Popen(['docker', *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                       text=True, env=dict(os.environ, **(env or {})))
            deadline = time.monotonic() + timeout
            try:
                while True:
                    if STOP.is_set():
                        raise InterruptedError('Run interrupted')
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise subprocess.TimeoutExpired('docker exec', timeout)
                    try:
                        out, err = process.communicate(timeout=min(1, remaining))
                        return subprocess.CompletedProcess(['docker', 'exec'], process.returncode, out, err)
                    except subprocess.TimeoutExpired:
                        pass
            finally:
                if process.poll() is None:
                    process.kill()
                process.communicate()
        return docker(*args, timeout=timeout, check=check,
                      env=dict(os.environ, **(env or {})))

    def relay(self, suffix, variables, audit=False):
        name = self.name + '-' + suffix
        args = ['run', '-d', '--user', '0:0', '--name', name, '--label', 'gamebench.run=' + self.name,
                '--add-host', 'host.docker.internal:host-gateway',
                '--read-only', '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges',
                '--memory', '256m', '--pids-limit', '128',
                '-v', self.socket_volume + ':/run/gamebench',
                '-v', str(MODULES / 'transport.py') + ':/relay.py:ro', '--entrypoint', 'python3']
        if audit:
            args += ['-v', str(self.output / 'audit') + ':/audit']
        for key in variables:
            args += ['-e', key]
        self.containers.append(name)
        docker(*args, self.config['environment']['docker_image'], '/relay.py', suffix,
               env=dict(os.environ, **variables))
        socket_name = 'llm.sock' if audit else 'submit.sock'
        for _ in range(60):
            if self.exec('test -S /run/gamebench/' + socket_name, check=False).returncode == 0:
                return
            if docker('inspect', '-f', '{{.State.Running}}', name).stdout.strip() != 'true':
                raise DockerError(f'{suffix} relay exited')
            time.sleep(.2)
        raise DockerError(f'{suffix} relay readiness timeout')

    def inspect(self):
        record = json.loads(docker('inspect', self.cid).stdout)[0]
        return {'state': record['State'], 'image': record['Image'],
                'network': record['HostConfig']['NetworkMode'],
                'memory': record['HostConfig']['Memory'],
                'readonly': record['HostConfig']['ReadonlyRootfs']}

    def close(self):
        errors = []
        for name in reversed(self.containers):
            try:
                result = docker('rm', '-f', name, check=False)
                if result.returncode and docker('inspect', name, check=False).returncode == 0:
                    errors.append(name)
            except Exception:
                errors.append(name)
        for name in reversed(self.volumes):
            try:
                if docker('volume', 'rm', name, check=False).returncode:
                    errors.append(name)
            except Exception:
                errors.append(name)
        return errors
