from __future__ import annotations

import asyncio
import json as _json
import logging
import sys
import time
from dataclasses import dataclass

from .config import AppConfig, AgentConfig
from .logger import TaskLogger

log = logging.getLogger(__name__)

_MAX_CAPTURE_BYTES = 50_000  # keep last N bytes of stdout for summary


@dataclass
class RunResult:
    exit_code: int
    stdout: str
    stderr: str
    duration_s: float
    success: bool
    session_id: str | None = None
    is_convo: bool = False  # True when agent signals ##CONVO## — lightweight chat mode


class AgentRunner:
    def __init__(self, config: AppConfig) -> None:
        self.config = config

    async def run(
        self,
        agent: AgentConfig,
        prompt: str,
        task_logger: TaskLogger,
        session_id: str | None = None,
        extra_env: dict[str, str] | None = None,
        on_start: "asyncio.coroutines.CoroutineType | None" = None,
        on_report: "asyncio.coroutines.CoroutineType | None" = None,
        on_stream: "asyncio.coroutines.CoroutineType | None" = None,
    ) -> RunResult:
        cmd = self._build_command(agent, session_id)

        import os
        env = os.environ.copy()
        # Never pass ANTHROPIC_API_KEY — claude CLI uses OAuth (claude.ai subscription).
        # Any API key in env (even a valid one) causes API-key auth which fails 401.
        env.pop("ANTHROPIC_API_KEY", None)
        if extra_env:
            env.update(extra_env)

        await task_logger.write_agent_prompt(prompt)

        log.info("Spawning subprocess: %s", " ".join(cmd))
        t_start = time.monotonic()

        kwargs: dict = dict(
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            cwd=agent.path,
            limit=4 * 1024 * 1024,  # 4MB — default 64KB too small for large Notion/MCP JSON responses
        )
        if sys.platform == "win32":
            kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW

        proc = await asyncio.create_subprocess_exec(*cmd, **kwargs)

        # Write prompt via stdin to avoid Windows cmd.exe quote mangling
        assert proc.stdin
        proc.stdin.write(prompt.encode("utf-8"))
        await proc.stdin.drain()
        proc.stdin.close()

        await task_logger.write_subprocess_start(cmd, proc.pid)
        if on_start:
            await on_start(proc.pid)

        stdout_chunks: list[str] = []
        stderr_chunks: list[str] = []
        _session: list[str] = []      # [0] = session_id once captured
        _result_text: list[str] = []  # [0] = full result from stream-json result event
        _last_activity = [time.monotonic()]  # mutable for closure
        _is_convo = [False]           # [0] = True when agent outputs ##CONVO##

        async def watchdog() -> None:
            """Heartbeat every 5min of silence. Kill process if running > MAX_RUNTIME."""
            HEARTBEAT_INTERVAL = 300   # 5 min between heartbeats
            MAX_RUNTIME = 5400         # 90 min hard limit
            _last_heartbeat = [time.monotonic()]
            while True:
                await asyncio.sleep(60)
                if proc.returncode is not None:
                    break
                elapsed = time.monotonic() - t_start
                silent_for = time.monotonic() - _last_activity[0]
                since_last_hb = time.monotonic() - _last_heartbeat[0]

                # Hard kill after 90 minutes
                if elapsed >= MAX_RUNTIME:
                    log.warning("Agent exceeded max runtime (%.0fs), killing process", elapsed)
                    if on_report:
                        await on_report(f"⚠️ 최대 실행 시간(90분) 초과로 강제 종료합니다. 작업을 다시 지시해주세요.")
                    try:
                        proc.kill()
                    except Exception:
                        pass
                    break

                # Heartbeat
                if silent_for >= HEARTBEAT_INTERVAL and since_last_hb >= HEARTBEAT_INTERVAL and on_report:
                    mins = int(elapsed // 60)
                    await on_report(f"⏳ 작업 중... ({mins}분 경과)")
                    _last_heartbeat[0] = time.monotonic()

        async def drain_stdout() -> None:
            assert proc.stdout
            async for raw_line in proc.stdout:
                _last_activity[0] = time.monotonic()
                decoded = raw_line.decode("utf-8", errors="replace")
                stripped = decoded.strip()

                # Skip empty / whitespace-only lines (common with --verbose output)
                if not stripped:
                    continue

                # Try to parse as stream-json event
                event: dict | None = None
                if stripped.startswith("{"):
                    try:
                        event = _json.loads(stripped)
                    except _json.JSONDecodeError:
                        # --verbose can emit non-JSON diagnostic lines starting with '{'.
                        # Log at debug level and fall through to plain-text handling.
                        log.debug("stream-json: skipping non-JSON line: %.120s", stripped)

                if event is not None:
                    # Capture session_id from any event that carries it
                    if (sid := event.get("session_id")) and not _session:
                        _session.append(sid)

                    # Rate limit detection — notify Slack only on actual rejection
                    if event.get("type") == "rate_limit_event":
                        info = event.get("rate_limit_info", {})
                        resets_at = info.get("resetsAt")
                        limit_type = info.get("rateLimitType", "unknown")
                        overage = info.get("overageStatus", "")
                        if overage == "rejected":
                            import datetime as _dt
                            reset_str = ""
                            if resets_at:
                                reset_kst = _dt.datetime.fromtimestamp(resets_at, tz=_dt.timezone(
                                    _dt.timedelta(hours=9)
                                ))
                                reset_str = f" (리셋: {reset_kst.strftime('%H:%M')} KST)"
                            log.warning("Rate limit rejected: %s", limit_type)
                            if on_report and not _is_convo[0]:
                                msg = f"☕ 잠깐 숨 고르는 중{reset_str} — 잠시 후 자동으로 이어갑니다"
                                await on_report(msg)

                    etype = event.get("type")

                    if etype == "assistant":
                        for block in event.get("message", {}).get("content", []):
                            btype = block.get("type")

                            if btype == "text":
                                text: str = block["text"]
                                stdout_chunks.append(text)
                                await task_logger.write_stdout(text)
                                # ##CONVO## on any line → flag this as conversational response
                                for text_line in text.splitlines():
                                    if text_line.strip() == "##CONVO##":
                                        _is_convo[0] = True
                                # ##SLACK## lines → on_report, but suppress in convo mode
                                if not _is_convo[0]:
                                    for text_line in text.splitlines():
                                        s = text_line.strip()
                                        if s.startswith("##SLACK##") and on_report:
                                            msg = s[len("##SLACK##"):].strip()
                                            if msg:
                                                await on_report(msg)
                                # Non-##SLACK##/##CONVO## text → on_stream
                                # Suppress on_stream in convo mode (final text sent after run)
                                if on_stream and not _is_convo[0]:
                                    visible = "\n".join(
                                        l for l in text.splitlines()
                                        if not l.strip().startswith("##SLACK##")
                                        and l.strip() != "##CONVO##"
                                    ).strip()
                                    if len(visible) >= 10:
                                        await on_stream(visible)

                            elif btype == "tool_use":
                                name = block.get("name", "?")
                                inp = block.get("input") or {}
                                await task_logger.write_stdout(f"[tool_use:{name}]\n")
                                if on_stream:
                                    readable = AgentRunner._readable_tool_msg(name, inp)
                                    if readable:
                                        await on_stream(readable)

                    elif etype == "user":
                        # tool_result blocks arrive as user-role messages
                        for block in event.get("message", {}).get("content", []):
                            if block.get("type") == "tool_result":
                                raw = block.get("content", "")
                                if isinstance(raw, list):
                                    parts = [b.get("text", "") for b in raw if b.get("type") == "text"]
                                    content_str = " ".join(parts)
                                else:
                                    content_str = str(raw) if raw else ""
                                await task_logger.write_stdout(f"[tool_result:{content_str[:120]}]\n")
                        await task_logger.write_stdout(decoded)

                    elif etype == "result":
                        # Final complete text — more reliable than accumulated chunks
                        if full := event.get("result"):
                            _result_text.append(full)
                        await task_logger.write_stdout(decoded)

                    else:
                        # system/init events: just log
                        await task_logger.write_stdout(decoded)

                else:
                    # Plain-text fallback (non-JSON line, or --output-format text mode,
                    # or --verbose diagnostic output that isn't a JSON event).
                    # Only forward ##SLACK## lines to on_report; suppress verbose noise
                    # from on_stream so internal diagnostics don't leak to Slack.
                    stdout_chunks.append(decoded)
                    await task_logger.write_stdout(decoded)
                    if stripped.startswith("##SLACK##") and on_report:
                        message = stripped[len("##SLACK##"):].strip()
                        if message:
                            await on_report(message)
                    elif on_stream and len(stripped) >= 10 and not stripped.startswith(
                        ("[", "Debug", "Verbose", "Warning", "Error", "Note")
                    ):
                        await on_stream(stripped)

        async def drain_stderr() -> None:
            assert proc.stderr
            first = True
            async for line in proc.stderr:
                decoded = line.decode("utf-8", errors="replace")
                if first:
                    await task_logger.write_stderr_start()
                    first = False
                stderr_chunks.append(decoded)
                await task_logger.write_stderr(decoded)

        watchdog_task = asyncio.create_task(watchdog())
        await asyncio.gather(drain_stdout(), drain_stderr())
        watchdog_task.cancel()
        exit_code = await proc.wait()
        duration_s = time.monotonic() - t_start

        # Prefer the complete result text from the stream-json result event
        stdout_text = _result_text[0] if _result_text else "".join(stdout_chunks)
        stderr_text = "".join(stderr_chunks)

        # trim to last N bytes to avoid OOM on huge outputs
        if len(stdout_text) > _MAX_CAPTURE_BYTES:
            stdout_text = "...(truncated)...\n" + stdout_text[-_MAX_CAPTURE_BYTES:]

        # Auto-push to GitHub after successful task
        if exit_code == 0:
            await self._auto_push(agent)

        return RunResult(
            exit_code=exit_code,
            stdout=stdout_text,
            stderr=stderr_text,
            duration_s=duration_s,
            success=exit_code == 0,
            session_id=_session[0] if _session else None,
            is_convo=_is_convo[0],
        )

    async def _auto_push(self, agent: AgentConfig) -> None:
        """git add/commit/push any changes the agent made in its workspace."""
        import subprocess
        try:
            cwd = agent.path
            # Skip if not a git repo
            result = subprocess.run(
                ["git", "rev-parse", "--git-dir"],
                cwd=cwd, capture_output=True, timeout=10,
            )
            if result.returncode != 0:
                return
            # Stage all changes
            subprocess.run(["git", "add", "-A"], cwd=cwd, capture_output=True, timeout=30)
            # Commit only if there are staged changes
            diff = subprocess.run(
                ["git", "diff", "--cached", "--quiet"],
                cwd=cwd, capture_output=True, timeout=10,
            )
            if diff.returncode == 0:
                return  # nothing to commit
            subprocess.run(
                ["git", "commit", "-m", "chore: auto-commit by Jarvis agent"],
                cwd=cwd, capture_output=True, timeout=30,
            )
            subprocess.run(
                ["git", "push"],
                cwd=cwd, capture_output=True, timeout=60,
            )
            log.info("Auto-pushed changes for agent %s", agent.name)
        except Exception as exc:
            log.warning("Auto-push failed for %s: %s", agent.name, exc)

    @staticmethod
    def _readable_tool_msg(name: str, inp: dict) -> str:
        """Human-readable Korean description of what the agent is doing."""
        def _short(v: str, n: int = 40) -> str:
            return v[-n:] if len(v) > n else v

        if name == "Bash":
            cmd = inp.get("command", "")
            c = cmd.strip()
            if "python" in c or "pip" in c:
                parts = [p for p in c.split() if p.endswith(".py")]
                script = parts[0].split("/")[-1].split("\\")[-1] if parts else "스크립트"
                return f"🐍 스크립트 실행 중: `{script}`"
            if "git" in c:
                git_parts = c.split()
                action = git_parts[1] if len(git_parts) > 1 else "작업"
                return f"📦 Git {action} 중"
            if c.startswith("ls") or "ls " in c:
                return "📂 파일 목록 확인 중"
            if "find " in c or "glob" in c.lower():
                return "🔍 파일 검색 중"
            if "mkdir" in c or "cp " in c or "mv " in c or "rm " in c:
                return "🗂️ 파일 정리 중"
            if "curl" in c or "wget" in c:
                return "📡 데이터 다운로드 중"
            if "playwright" in c or "chromium" in c:
                return "🌐 웹 브라우저로 작업 중"
            if "convert" in c or "ffmpeg" in c or "magick" in c:
                return "🖼️ 이미지/미디어 변환 중"
            # complex pipe commands — show a clean short version
            first_cmd = c.split("|")[0].strip().split()[0] if c else ""
            return f"⚙️ 명령 실행 중: `{first_cmd}`" if first_cmd else ""
        if name == "Read":
            path = inp.get("file_path", "")
            fname = path.replace("\\", "/").split("/")[-1]
            return f"📄 파일 읽는 중: `{fname}`"
        if name in ("Write", "Edit"):
            path = inp.get("file_path", "")
            fname = path.replace("\\", "/").split("/")[-1]
            action = "저장" if name == "Write" else "수정"
            return f"✏️ 파일 {action} 중: `{fname}`"
        if name == "Glob":
            return f"🔍 파일 검색 중: `{inp.get('pattern', '')}`"
        if name == "Grep":
            return f"🔎 코드 검색 중: `{inp.get('pattern', '')[:40]}`"
        if name == "WebSearch":
            return f"🌐 검색 중: `{inp.get('query', '')[:50]}`"
        if name == "WebFetch":
            url = inp.get("url", "")
            domain = url.split("/")[2] if url.count("/") >= 2 else url
            return f"🌍 사이트 접근 중: `{domain}`"
        if name in ("TodoWrite", "TodoRead"):
            return ""  # skip — internal planning, not meaningful to user
        # MCP tools
        if "notion" in name.lower():
            return "📝 Notion에 업로드 중..."
        if "slack" in name.lower():
            return "💬 Slack 메시지 전송 중..."
        if "gdrive" in name.lower() or "google" in name.lower() or "drive" in name.lower():
            return "☁️ Google Drive에 업로드 중..."
        if "imagen" in name.lower() or "gemini" in name.lower():
            return "🎨 AI 이미지 생성 중..."
        if "unsplash" in name.lower():
            return "📸 사진 검색 중..."
        return ""

    @staticmethod
    def _format_tool_input(name: str, inp: dict) -> str:
        """Return a short Slack-safe description of the tool's key input."""
        def _q(v: str, limit: int = 60) -> str:
            return f": `{v[:limit]}`" if v else ""

        if name in ("Read", "Write", "Edit", "Glob"):
            return _q(inp.get("file_path") or inp.get("pattern") or "")
        if name == "Bash":
            return _q(inp.get("command", ""), 80)
        if name == "Grep":
            pat = inp.get("pattern", "")
            path = inp.get("path", "")
            suffix = f" in `{path}`" if path else ""
            return (f": `{pat[:50]}`{suffix}") if pat else ""
        if name in ("WebSearch",):
            return _q(inp.get("query", ""))
        if name in ("WebFetch",):
            return _q(inp.get("url", ""))
        # Generic fallback: first string value
        for v in inp.values():
            if isinstance(v, str) and v:
                return _q(v)
        return ""

    @staticmethod
    def _build_command(
        agent: AgentConfig,
        session_id: str | None,
    ) -> list[str]:
        # Prompt is passed via stdin — no -p flag, avoids Windows cmd.exe quote mangling
        claude_args = ["--output-format", "stream-json", "--verbose", "--dangerously-skip-permissions"]
        if agent.model:
            claude_args += ["--model", agent.model]
        if session_id:
            claude_args += ["--resume", session_id]

        if sys.platform == "win32":
            return ["cmd", "/c", "claude"] + claude_args
        return ["claude"] + claude_args

    def get_project_path(self, agent: AgentConfig) -> str:
        return agent.path
