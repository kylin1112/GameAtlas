"""Reuse the tested Pi provider/compaction helpers without importing Harbor."""
import json
import shlex
import subprocess
from typing import Any


class Agent:
    _RUN_ID_ENV = "HARBOR_PI_RUN_ID"
    def __init__(self, config):
        self.config = config
        self._reasoning = config.get('thinking', 'off') != 'off'
        self._vision = config.get('vision', True)
        self._api = config.get('api', 'openai-completions')
        self._premature_stop_guard = True
        self._thinking_level_map = config.get('thinking_level_map')
        if self._thinking_level_map is None and config.get('thinking') == 'xhigh':
            self._thinking_level_map = {'xhigh': 'xhigh'}
        self._reasoning_history_field = config.get('reasoning_history_field')
        self._chat_template_kwargs = config.get('chat_template_kwargs')
        self._compaction_reserve_tokens = config.get('reserve_tokens', 36864)
        self._compaction_keep_recent_tokens = config.get('keep_recent_tokens', 20000)
        self._usage_compaction_watermark_tokens = config['context_window'] - self._compaction_reserve_tokens
        self._retry_max_retries = 2
        self._retry_base_delay_ms = 2000
        self._provider_timeout_ms = config.get('request_timeout_sec', 600) * 1000
        self._provider_max_retries = 0
        self._provider_max_retry_delay_ms = 60000

    def run(self, environment, instruction, timeout, *, resume=False):
        config, folder = self.config, environment.output / 'agent'
        (folder / 'provider.ts').write_text(self._extension_source(config['model']))
        (folder / 'instruction.txt').write_text(instruction)
        settings = {'retry': self._retry_settings(), 'compaction': self._compaction_settings()}
        (folder / 'settings.json').write_text(json.dumps(settings))
        bridge = '' if resume else 'python3 /usr/local/bin/gamebench-llm-bridge bridge >/tmp/llm-bridge.log 2>&1 &'
        environment.exec('set -e; test "$(/opt/node/bin/pi --version)" = "0.84.2"; '
                         'mkdir -p "$HOME/.pi/agent"; '
                         'cp /logs/agent/settings.json "$HOME/.pi/agent/settings.json"; '
                         + bridge)
        environment.exec('python3 -c ' + shlex.quote(
            'import socket,time\n'
            'for i in range(50):\n'
            ' try:\n  s=socket.create_connection(("127.0.0.1",8780),.2);s.close();break\n'
            ' except OSError:time.sleep(.1)\n'
            'else:raise RuntimeError("LLM bridge unavailable")'))
        variables = {
            'HARBOR_PI_RUN_ID': environment.name,
            'HARBOR_PI_PROVIDER': 'gamebench', 'HARBOR_PI_PROVIDER_DISPLAY_NAME': 'GameBench',
            'HARBOR_PI_BASE_URL': ('http://127.0.0.1:8780' if self._api == 'anthropic-messages'
                                   else 'http://127.0.0.1:8780/v1'),
            'HARBOR_PI_API_KEY': 'relay-managed',
            'HARBOR_PI_CONTEXT_WINDOW': str(config['context_window']),
            'HARBOR_PI_MAX_TOKENS': str(config['max_tokens']),
        }
        command = ('/opt/node/bin/pi --print --mode json --session-dir /logs/agent/sessions '
                   '--extension /logs/agent/provider.ts --provider gamebench --model '
                   + shlex.quote(config['model']) + ' --thinking ' + shlex.quote(config.get('thinking', 'off'))
                   + ' "$(cat /logs/agent/instruction.txt)" > /logs/agent/pi.txt 2>&1')
        if resume:
            command = command.replace('--print --mode json', '--print --mode json --continue', 1)
        try:
            result = environment.exec(command, timeout=timeout, env=variables, check=False, interruptible=True)
            terminal = 'finished' if result.returncode == 0 else 'agent_error'
        except subprocess.TimeoutExpired:
            terminal = 'timeout'
        finally:
            # Stop detached tool children too, before snapshotting the final candidate.
            environment.exec(self._cleanup_run_command(environment.name), timeout=20)
        environment.exec(self._usage_summary_command(), timeout=30, check=False)
        validation = environment.exec(self._completion_validator_command(), check=False)
        if terminal == 'finished' and validation.returncode:
            terminal = 'agent_error'
        return terminal


    def _retry_settings(self) -> dict[str, Any]:
        settings: dict[str, Any] = {
            "enabled": True,
            "maxRetries": (
                5 if self._retry_max_retries is None else self._retry_max_retries
            ),
            "baseDelayMs": (
                5000
                if self._retry_base_delay_ms is None
                else self._retry_base_delay_ms
            ),
        }
        provider: dict[str, int] = {}
        if self._provider_timeout_ms is not None:
            provider["timeoutMs"] = self._provider_timeout_ms
        if self._provider_max_retries is not None:
            provider["maxRetries"] = self._provider_max_retries
        if self._provider_max_retry_delay_ms is not None:
            provider["maxRetryDelayMs"] = self._provider_max_retry_delay_ms
        if provider:
            settings["provider"] = provider
        return settings


    def _compaction_settings(self) -> dict[str, Any]:
        return {
            "enabled": True,
            "reserveTokens": self._compaction_reserve_tokens,
            "keepRecentTokens": self._compaction_keep_recent_tokens,
        }


    def _extension_source(self, model_id: str) -> str:
        request_hook = ""
        chat_template = getattr(self, "_chat_template_kwargs", None)
        if chat_template is not None:
            request_hook = f'''
  pi.on("before_provider_request", (event) => ({{
    ...(event.payload as Record<string, unknown>),
    chat_template_kwargs: {json.dumps(chat_template)},
  }}));
'''
        reasoning = "true" if self._reasoning else "false"
        inputs = '["text", "image"]' if self._vision else '["text"]'
        registered_api = repr(self._api)
        imports = 'import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";'
        thinking_level_map = getattr(self, "_thinking_level_map", None)
        thinking_level_map_line = (
            f"thinkingLevelMap: {json.dumps(thinking_level_map)},"
            if thinking_level_map is not None
            else ""
        )
        guard = ""
        history_field = getattr(self, "_reasoning_history_field", None)
        wrap_stream = self._premature_stop_guard or bool(history_field)
        normalize_history = ""
        if history_field:
            # Pi replays reasoning under the response field's name. Some
            # gateways return `reasoning` but accept only `reasoning_content`.
            normalize_history = f"""
          for (const part of m?.content || []) {{
            if (part.type === "thinking") {{
              part.thinkingSignature = {json.dumps(history_field)};
            }}
          }}
"""
        if wrap_stream:
            registered_api = repr("harbor-guarded-stream")
            imports += """
import {
  createAssistantMessageEventStream,
  streamSimple,
} from "@earendil-works/pi-ai";"""
            guard = f"""
  // pi-ai can emit stopReason="stop", usage=0 when an unhealthy upstream
  // closes early. Some variants also carry a response id or a truncated
  // reasoning fragment, but still contain no text, image, or tool call the
  // agent can act on. Convert that sentinel into an error *before* the agent
  // loop sees it. This preserves
  // pi's native exponential backoff, retry cap, and auto_retry telemetry;
  // importantly, it does not add fabricated user messages to the session.
  function __guardUsageTotal(u) {{
    u = u || {{}};
    return (
      (u.input || 0) + (u.output || 0) + (u.cacheRead || 0) + (u.cacheWrite || 0)
    ) || u.totalTokens || 0;
  }}
  function __guardHasActionableContent(m) {{
    const parts = Array.isArray(m.content) ? m.content : [];
    return parts.some((c) =>
      c.type === "toolCall" || c.type === "tool_use" || c.type === "image" ||
      (c.type === "text" && String(c.text || "").trim().length > 0)
    );
  }}
  // Bound only the outgoing context. Session/trajectory images remain intact.
  // Keep the newest 50 images across user and tool-result messages; preserve
  // all message/tool IDs, text, ordering, and reasoning signatures.
  function __limitRequestImages(context) {{
    let remaining = 50;
    const messages = [...context.messages];
    for (let i = messages.length - 1; i >= 0; i--) {{
      const message = messages[i];
      if (!Array.isArray(message.content)) continue;
      let changed = false;
      const content = [...message.content];
      for (let j = content.length - 1; j >= 0; j--) {{
        if (content[j]?.type !== "image") continue;
        if (remaining > 0) {{ remaining--; continue; }}
        changed = true;
        content[j] = {{ type: "text", text:
          "[Older image omitted from this model request to respect the 50-image limit. " +
          "The original image remains in the session; read the source file again if needed.]" }};
      }}
      if (changed) messages[i] = {{ ...message, content }};
    }}
    return {{ ...context, messages }};
  }}
  function __guardedStream(model, context, options) {{
    const guarded = createAssistantMessageEventStream();
    const upstreamModel = {{ ...model, api: {self._api!r} }};
    const upstream = streamSimple(upstreamModel, __limitRequestImages(context), options);
    (async () => {{
      for await (const event of upstream) {{
        if (event.type === "done") {{
          const m = event.message;
{normalize_history}
          const syntheticEmptyStop =
            {str(self._premature_stop_guard).lower()} &&
            m && m.stopReason === "stop" && __guardUsageTotal(m.usage) === 0 &&
            !__guardHasActionableContent(m);
          if (syntheticEmptyStop) {{
            m.stopReason = "error";
            m.errorMessage =
              "SyntheticEmptyCompletionError: upstream stream ended without " +
              "actionable content or token usage";
            guarded.push({{ type: "error", reason: "error", error: m }});
            guarded.end();
            return;
          }}
        }}
        guarded.push(event);
      }}
      guarded.end();
    }})();
    return guarded;
  }}
"""
        return f"""
{imports}

export default function(pi: ExtensionAPI) {{
{request_hook}
  // Pi 0.84 still checks threshold auto-compaction after agent_end / before a
  // new user prompt. A single --print agent loop can contain hundreds of tool
  // turns. At the watermark, end the current internal loop, but do not queue
  // the hidden continuation until agent_end: Pi drains messages queued during
  // message_end before it emits agent_end, which would bypass its post-run
  // compaction check. An agent_end continuation stays queued while Pi performs
  // its synchronous native threshold compact, then keeps print mode alive.
  // Calling ctx.compact() here would be fire-and-forget, so print mode could
  // dispose its runtime before the asynchronous compact completed.
  const __usageCompactionWatermark = {self._usage_compaction_watermark_tokens};
  let __usageCompactionPending = false;
  let __usageCompactionQueued = false;
  pi.on("session_compact", () => {{
    __usageCompactionPending = false;
    __usageCompactionQueued = false;
  }});
  pi.on("agent_end", () => {{
    if (!__usageCompactionPending) return;
    __usageCompactionPending = false;
    pi.sendMessage({{
      customType: "gamebench-compaction-resume",
      content:
        "Context compaction completed. Continue the current task from the " +
        "summary and live workspace. Preserve the canonical baseline, best " +
        "score/checkpoint, submission history, remaining quota, failed " +
        "hypotheses, and best-known final state.",
      display: false,
    }}, {{ triggerTurn: true, deliverAs: "followUp" }});
  }});
  pi.on("message_end", (event) => {{
    const message: any = event.message;
    if (
      message?.role !== "assistant" || message.stopReason === "error" ||
      message.stopReason === "aborted" || __usageCompactionQueued
    ) return;
    const usage = message.usage || {{}};
    const providerInput =
      Number(usage.input || 0) + Number(usage.cacheRead || 0);
    if (providerInput < __usageCompactionWatermark) return;
    __usageCompactionPending = true;
    __usageCompactionQueued = true;
    return {{
      message: {{
        ...message,
        content: [],
        stopReason: "stop",
      }},
    }};
  }});

  pi.registerProvider(process.env.HARBOR_PI_PROVIDER!, {{
    name: process.env.HARBOR_PI_PROVIDER_DISPLAY_NAME!,
    baseUrl: process.env.HARBOR_PI_BASE_URL!,
    apiKey: "HARBOR_PI_API_KEY",
    api: {registered_api},
    {"streamSimple: __guardedStream," if wrap_stream else ""}
    models: [{{
      id: {model_id!r},
      name: {model_id!r},
      reasoning: {reasoning},
      {thinking_level_map_line}
      input: {inputs},
      cost: {{ input: 0, output: 0, cacheRead: 0, cacheWrite: 0 }},
      contextWindow: Number(process.env.HARBOR_PI_CONTEXT_WINDOW || "262144"),
      maxTokens: Number(process.env.HARBOR_PI_MAX_TOKENS || "65536"),
      compat: {{
        supportsDeveloperRole: false,
        maxTokensField: "max_tokens",
        forceAdaptiveThinking: {str(self._api == "anthropic-messages").lower()}
      }}
    }}]
  }});
{guard}}}
""".strip()


    @staticmethod
    def _completion_validator_command() -> str:
        validate = """import json
import pathlib
import sys

messages = []
for line in pathlib.Path("/logs/agent/pi.txt").read_text().splitlines():
    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        continue
    message = event.get("message", {})
    if event.get("type") == "message_end" and message.get("role") == "assistant":
        messages.append(message)

message = messages[-1] if messages else {}
reason = message.get("stopReason")
error = message.get("errorMessage")
parts = message.get("content") or []
actionable = any(
    part.get("type") in {"toolCall", "tool_use", "image"}
    or (part.get("type") == "text" and str(part.get("text") or "").strip())
    for part in parts
    if isinstance(part, dict)
)
print("Pi terminal state:", reason, error or "")
sys.exit(0 if reason == "stop" and actionable else 1)
"""
        return f"python3 -c {shlex.quote(validate)}"


    @staticmethod
    def _usage_summary_command() -> str:
        summarize = """import json
import pathlib

source = pathlib.Path("/logs/agent/pi.txt")
target = pathlib.Path("/logs/agent/pi-usage-summary.json")
totals = {key: 0 for key in ("input", "output", "cacheRead", "cacheWrite", "totalTokens")}
max_request_input = 0
request_count = 0
compaction_count = 0
if source.is_file():
    for line in source.read_text().splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") == "compaction_end" and not event.get("aborted"):
            compaction_count += 1
        message = event.get("message") or {}
        if event.get("type") != "message_end" or message.get("role") != "assistant":
            continue
        usage = message.get("usage") or {}
        request_count += 1
        for key in totals:
            value = usage.get(key, 0)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                totals[key] += int(value)
        request_input = int(usage.get("input", 0) or 0) + int(usage.get("cacheRead", 0) or 0)
        max_request_input = max(max_request_input, request_input)
payload = {
    "schema_version": 1,
    "request_count": request_count,
    "cumulative_tokens": totals,
    "max_single_request_input_tokens": max_request_input,
    "compaction_count": compaction_count,
}
target.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\\n")
"""
        return f"python3 -c {shlex.quote(summarize)}"


    @classmethod
    def _cleanup_run_command(cls, run_id: str) -> str:
        """Terminate every process that inherited one Pi invocation's run id.

        Harbor enforces an agent timeout by cancelling its host-side
        ``docker compose exec`` client. Docker does not forward that
        cancellation to the process tree already running in the container,
        and Pi tools may create their own process groups. An inherited,
        per-invocation environment marker remains visible in ``/proc`` even
        after such children are reparented, so it is safer than PID/PGID-only
        cleanup.
        """
        cleanup = f"""import json
import os
import signal
import time

marker = {f"{cls._RUN_ID_ENV}={run_id}"!r}.encode()
self_pid = os.getpid()

def marked_pids():
    found = []
    for entry in os.scandir("/proc"):
        if not entry.name.isdecimal():
            continue
        pid = int(entry.name)
        if pid in (1, self_pid):
            continue
        try:
            environ = open(f"/proc/{{pid}}/environ", "rb").read().split(b"\\0")
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            continue
        if marker in environ:
            found.append(pid)
    return found

terminated = marked_pids()
for pid in terminated:
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass

deadline = time.monotonic() + 5.0
remaining = marked_pids()
while remaining and time.monotonic() < deadline:
    time.sleep(0.05)
    remaining = marked_pids()

for pid in remaining:
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass

deadline = time.monotonic() + 2.0
remaining = marked_pids()
while remaining and time.monotonic() < deadline:
    time.sleep(0.05)
    remaining = marked_pids()

print(json.dumps({{
    "run_id": {run_id!r},
    "terminated_pids": terminated,
    "remaining_pids": remaining,
}}, sort_keys=True))
raise SystemExit(1 if remaining else 0)
"""
        return f"python3 -c {shlex.quote(cleanup)}"
