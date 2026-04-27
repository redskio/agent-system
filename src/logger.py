from __future__ import annotations

import asyncio
import threading
from datetime import datetime, timezone, timedelta
from pathlib import Path

LOGS_DIR = Path(__file__).parent.parent / "logs"
LOGS_DIR.mkdir(exist_ok=True)

SESSION_LOG = LOGS_DIR / "session.log"
_session_lock = threading.Lock()  # thread lock avoids asyncio event-loop dependency

_KST = timezone(timedelta(hours=9))


def _now_kst() -> str:
    return datetime.now(_KST).strftime("%H:%M:%S")


def _now_kst_full() -> str:
    return datetime.now(_KST).isoformat(timespec="seconds")


async def session_log(event: str, **fields: object) -> None:
    """Append one structured line to session.log for every notable event."""
    parts = [f"[{_now_kst()}] {event}"]
    for k, v in fields.items():
        val = str(v)
        if "\n" in val:
            val = val.replace("\n", " ↵ ")
        if len(val) > 300:
            val = val[:300] + "…"
        parts.append(f"  {k}: {val}")
    line = "\n".join(parts) + "\n\n"
    try:
        with _session_lock:
            with SESSION_LOG.open("a", encoding="utf-8") as f:
                f.write(line)
    except Exception as exc:  # noqa: BLE001
        import sys
        print(f"[session_log ERROR] {exc}", file=sys.stderr, flush=True)


BRIEFS_DIR = LOGS_DIR / "briefs"
BRIEFS_DIR.mkdir(exist_ok=True)


def log_path(task_id: str) -> Path:
    return LOGS_DIR / f"{task_id}.log"


def brief_path(task_id: str) -> Path:
    return BRIEFS_DIR / f"{task_id}_brief.md"


class TaskLogger:
    def __init__(self, task_id: str, repo: str, user_request: str) -> None:
        self.task_id = task_id
        self.path = log_path(task_id)
        self._lock = asyncio.Lock()

        header = (
            f"=== TASK {task_id} ===\n"
            f"Created: {_now_kst_full()}\n"
            f"Repo: {repo}\n"
            f"User request: {user_request}\n\n"
        )
        self.path.write_text(header, encoding="utf-8")

    async def write_routing(
        self,
        method: str,
        reason: str,
        brain_decision: str,
        router_decision: str,
        matched_keywords: list[str] | None = None,
    ) -> None:
        kw_str = f" (matched keywords: {', '.join(matched_keywords)})" if matched_keywords else ""
        block = (
            "--- Routing ---\n"
            f"Method: {method}\n"
            f"Reason: {reason}\n"
            f"Jarvis brain decision: {brain_decision}\n"
            f"Router decision: {router_decision}{kw_str}\n\n"
        )
        await self._append(block)

    async def write_agent_prompt(self, prompt: str) -> None:
        block = f"--- Agent prompt ---\n{prompt}\n\n"
        await self._append(block)

    async def write_subprocess_start(self, command: list[str], pid: int) -> None:
        cmd_str = " ".join(f'"{a}"' if " " in a else a for a in command)
        block = (
            "--- Subprocess ---\n"
            f"Command: {cmd_str}\n"
            f"PID: {pid}\n"
            f"Started: {_now_kst_full()}\n\n"
            "--- stdout ---\n"
        )
        await self._append(block)

    async def write_stdout(self, line: str) -> None:
        await self._append(line)

    async def write_stderr_start(self) -> None:
        await self._append("\n--- stderr ---\n")

    async def write_stderr(self, line: str) -> None:
        await self._append(line)

    async def write_result(
        self, exit_code: int, duration_s: float, summary: str
    ) -> None:
        block = (
            "\n--- Result ---\n"
            f"Exit code: {exit_code}\n"
            f"Completed: {_now_kst_full()}\n"
            f"Duration: {duration_s:.0f}s\n\n"
            "--- Jarvis summary ---\n"
            f"{summary}\n"
        )
        await self._append(block)

    async def write_error(self, message: str) -> None:
        block = f"\n--- Error ---\n{message}\n"
        await self._append(block)

    async def write_task_brief(
        self,
        repo: str,
        user_request: str,
        agent_prompt: str,
        routing_reason: str,
        history_summary: str | None = None,
    ) -> Path:
        """Write a structured task brief MD file for the sub-agent to reference."""
        path = brief_path(self.task_id)
        lines = [
            f"# Task Brief — {self.task_id}",
            f"**Created:** {_now_kst_full()}",
            f"**Repo:** {repo}",
            "",
            "## 재우의 요청",
            user_request,
            "",
        ]
        if routing_reason:
            lines += ["## 라우팅 근거", routing_reason, ""]
        if history_summary:
            lines += ["## 최근 대화 맥락 (요약)", history_summary, ""]
        lines += [
            "## 에이전트 지시사항",
            agent_prompt,
            "",
            "## 글로벌 규칙 (MANDATORY)",
            "- 결과물 파일(PPT 슬라이드, Notion 페이지, 문서 등) 내용에 에이전트 페르소나/이름/캐치프레이즈 절대 포함 금지.",
            "- 예: 슬라이드에 'HULK가 만든 강의자료', Notion에 'I am Iron Man' 등 일절 금지.",
            "- ##SLACK## 보고에서는 페르소나 사용 가능. 결과물 내용에서만 금지.",
            "",
        ]
        content = "\n".join(lines)
        async with self._lock:
            path.write_text(content, encoding="utf-8")
        return path

    async def _append(self, text: str) -> None:
        async with self._lock:
            with self.path.open("a", encoding="utf-8") as f:
                f.write(text)
