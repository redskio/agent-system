"""
Jarvis Multi-Agent System — Phase 1 MVP
Entrypoint: python main.py
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from pathlib import Path
from typing import Any

import httpx

from src.config import load_config
from src.db import init_db, cleanup_stale_tasks, add_conversation, get_recent_history, get_channel_history, create_task, complete_task, fail_task, get_repo_session, get_running_tasks, set_repo_session, pause_for_ask, get_waiting_ask_by_thread, mark_ask_resumed
from src.jarvis_brain import JarvisBrain
from src.router import Router
from src.agent_runner import AgentRunner
from src.reporter import Reporter
from src.slack_listener import SlackListener
from src.logger import TaskLogger, log_path, brief_path, session_log, LOGS_DIR

# ── Logging setup ───────────────────────────────────────────────────────────
# Python logs → jarvis.log (keeps session.log clean for structured events)
_log_file = LOGS_DIR / "jarvis.log"
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    handlers=[logging.FileHandler(_log_file, encoding="utf-8")],
)

_NOISY_LOGGERS = (
    "slack_bolt", "slack_bolt.AsyncApp", "slack_bolt.adapter",
    "slack_sdk", "slack_sdk.web", "slack_sdk.socket_mode",
    "aiosqlite", "asyncio", "websockets", "urllib3",
)


def _suppress_noisy_loggers() -> None:
    for name in _NOISY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)


_suppress_noisy_loggers()  # initial suppression (handles asyncio/aiosqlite at startup)
log = logging.getLogger("jarvis")


_RESULT_EXTENSIONS = {".pptx", ".pdf", ".png", ".jpg", ".xlsx", ".csv", ".md", ".html"}


def _extract_urls(text: str, domains: list[str]) -> list[str]:
    """Extract URLs matching given domains from text."""
    import re
    found = re.findall(r'https?://[^\s\)\]>\"\']+', text)
    return [u for u in found if any(d in u for d in domains)]


def _detect_output_files(agent_path: str) -> list[str]:
    """Detect newly created/modified output files in the agent's directory via git."""
    import subprocess
    try:
        result = subprocess.run(
            ["git", "diff", "--name-only", "HEAD"],
            cwd=agent_path, capture_output=True, text=True, timeout=10,
        )
        # Also check untracked files
        untracked = subprocess.run(
            ["git", "ls-files", "--others", "--exclude-standard"],
            cwd=agent_path, capture_output=True, text=True, timeout=10,
        )
        files = (result.stdout + untracked.stdout).strip().splitlines()
        output = []
        for f in files:
            p = Path(agent_path) / f
            if p.suffix.lower() in _RESULT_EXTENSIONS and p.exists() and p.stat().st_size < 50_000_000:
                output.append(str(p))
        return output[:3]
    except Exception:
        return []


class Jarvis:
    def __init__(self) -> None:
        self.config = load_config()
        self.brain = JarvisBrain(self.config)
        self.router = Router(self.config)
        self.runner = AgentRunner(self.config)
        self.reporter = Reporter(self.config)

    async def handle_message(self, event: dict[str, Any]) -> None:
        user_text: str = event.get("text", "").strip()
        channel: str = event.get("channel", "")
        thread_ts: str | None = event.get("thread_ts")  # None when top-level channel message
        user_orig_thread: str | None = thread_ts
        message_ts: str | None = event.get("ts")

        # Download Slack-private files and pass local paths to brain/agents
        files: list[dict] = event.get("files", [])
        if files:
            uploads_dir = Path(__file__).parent / "uploads"
            uploads_dir.mkdir(exist_ok=True)
            file_lines = []
            async with httpx.AsyncClient() as client:
                for f in files:
                    name = f.get("name", "file")
                    mimetype = f.get("mimetype", "")
                    url = f.get("url_private_download") or f.get("url_private", "")
                    if not url:
                        continue
                    try:
                        resp = await client.get(
                            url,
                            headers={"Authorization": f"Bearer {self.config.slack_bot_token}"},
                            follow_redirects=True,
                            timeout=30.0,
                        )
                        resp.raise_for_status()
                        local_path = uploads_dir / name
                        local_path.write_bytes(resp.content)
                        file_lines.append(f"[첨부파일: {name} ({mimetype}) 로컬경로={local_path}]")
                        log.info("Downloaded Slack file: %s → %s", name, local_path)
                    except Exception as exc:
                        log.warning("Failed to download Slack file %s: %s", name, exc)
                        file_lines.append(f"[첨부파일: {name} ({mimetype}) 다운로드 실패]")
            if file_lines:
                user_text = (user_text + "\n" + "\n".join(file_lines)).strip()

        if not user_text:
            return

        # ── Direct channel routing — skip Brain if channel matches agent's direct_channel ──
        direct_agent_name: str | None = None
        for _aname, _agent in self.config.agents.items():
            if _agent.direct_channel and _agent.direct_channel == channel:
                direct_agent_name = _aname
                break

        log.info("Message from %s: %s", channel, user_text[:120])
        await session_log("USER", channel=channel, text=user_text)

        # ACK — react immediately so user knows message was received
        if message_ts:
            await self.reporter.react(channel, message_ts, "eyes")

        # Check if this message is a response to a ##ASK## (waiting_for_ask task)
        if thread_ts:
            waiting_ask = await get_waiting_ask_by_thread(thread_ts)
            if waiting_ask:
                await self._resume_after_ask(waiting_ask, user_text, channel, thread_ts)
                await add_conversation(role="user", content=user_text, slack_channel=channel, slack_thread_ts=thread_ts, slack_message_ts=message_ts)
                return

        # Check if this message is a supplement to a running task (thread interrupt)
        running_tasks = await get_running_tasks()
        if thread_ts:
            for rt in running_tasks:
                if rt.get("slack_thread_ts") == thread_ts:
                    # User is replying in a running task's thread — treat as supplement
                    await self.reporter.send_chat(
                        channel,
                        f"💬 메모 추가됨 — 진행 중인 작업에 반영할게요: _{user_text[:100]}_",
                        thread_ts,
                    )
                    # Append supplement to task brief for context
                    from src.logger import brief_path as _brief_path
                    bp = _brief_path(rt["task_id"])
                    if bp.exists():
                        async with __import__('aiofiles').open(bp, "a", encoding="utf-8") as f:
                            await f.write(f"\n## 추가 지시 (실행 중 수신)\n{user_text}\n")
                    await add_conversation(role="user", content=user_text, slack_channel=channel, slack_thread_ts=thread_ts, slack_message_ts=message_ts)
                    return

        # (2) Save user message
        await add_conversation(
            role="user",
            content=user_text,
            slack_channel=channel,
            slack_thread_ts=thread_ts,
            slack_message_ts=message_ts,
        )

        # If message came from an agent's direct channel, skip Brain and route directly
        if direct_agent_name:
            await self._handle_direct_agent_message(
                agent_name=direct_agent_name,
                user_text=user_text,
                channel=channel,
                thread_ts=thread_ts,
                message_ts=message_ts,
            )
            return

        # (3) Jarvis brain decides — channel-scoped history
        history = await get_recent_history(self.config.jarvis_brain.history_window, slack_channel=channel)
        brain_history = history[:-1]

        running_tasks = await get_running_tasks()
        brain_result = await self.brain.process(user_text, brain_history, running_tasks)

        await session_log(
            "BRAIN",
            decision=brain_result.type,
            repo=brain_result.repo or "-",
            routing_reason=brain_result.routing_reason or "-",
            user_facing_summary=brain_result.user_facing_summary or "-",
            error=brain_result.error_detail or "-",
        )

        # Surface brain errors to control channel
        if brain_result.error_detail:
            await self.reporter.send_chat(
                self.config.slack.control_channel,
                f"⚠️ *BRAIN ERROR*\n```{brain_result.error_detail}```",
            )

        # (4) Chat — reply in thread if user was in a thread, else channel
        if brain_result.type == "chat":
            reply = brain_result.chat_response or "..."
            reply_thread = event.get("thread_ts")
            await session_log("BRAIN→CHAT", reply=reply)
            await self.reporter.send_chat(channel, reply, reply_thread)
            await add_conversation(
                role="assistant",
                content=reply,
                slack_channel=channel,
                slack_thread_ts=thread_ts,
            )
            return

        # (5) Delegation path
        routing = await self.router.route(
            user_message=user_text,
            brain_repo=brain_result.repo,
            brain_reason=brain_result.routing_reason,
        )
        repo_name = routing.repo
        agent = self.config.agents[repo_name]

        await session_log(
            "ROUTER",
            method=routing.method,
            repo=repo_name,
            reason=routing.reason,
            brain_suggested=brain_result.repo or "-",
            keywords=",".join(routing.matched_keywords) if routing.matched_keywords else "-",
        )

        agent_prompt = brain_result.agent_prompt or user_text
        task_id = str(uuid.uuid4())
        task_log = TaskLogger(task_id, repo_name, user_text)

        await task_log.write_routing(
            method=routing.method,
            reason=routing.reason,
            brain_decision=brain_result.repo or "?",
            router_decision=repo_name,
            matched_keywords=routing.matched_keywords or None,
        )

        # Build context summary from history for the brief
        history_summary: str | None = None
        if history:
            recent = history[-4:]
            lines = []
            for msg in recent:
                role_label = "재우" if msg["role"] == "user" else "Jarvis"
                content = msg["content"] if isinstance(msg["content"], str) else str(msg["content"])
                lines.append(f"{role_label}: {content[:200]}")
            history_summary = "\n".join(lines)

        brief_file = await task_log.write_task_brief(
            repo=repo_name,
            user_request=user_text,
            agent_prompt=agent_prompt,
            routing_reason=routing.reason,
            history_summary=history_summary,
        )
        # Prepend brief file reference so agent can read full context if needed
        brief_ref = f"[태스크 브리프 파일: {brief_file}]\n\n"
        if not agent_prompt.startswith(brief_ref):
            agent_prompt = brief_ref + agent_prompt

        log_file = str(log_path(task_id))

        await create_task(
            task_id=task_id,
            repo=repo_name,
            user_request=user_text,
            agent_prompt=agent_prompt,
            routing_reason=routing.reason,
            routing_method=routing.method,
            slack_channel=channel,
            slack_thread_ts=thread_ts,
            log_path=log_file,
        )

        await session_log(
            "AGENT_DISPATCH",
            task_id=task_id,
            repo=repo_name,
            cwd=agent.path,
            prompt_preview=agent_prompt[:200],
        )

        # Reports follow the user's channel. Fall back to agent.report_channel (scheduled tasks)
        # or control_channel if neither is available.
        report_channel = channel or agent.report_channel or self.config.slack.control_channel

        # (6) Start notification — reply in user's thread if they're in one, else create new thread
        task_thread_ts = await self.reporter.send_task_start(
            repo=repo_name,
            emoji=agent.emoji,
            routing_reason=routing.reason,
            user_request=user_text,
            agent_prompt=agent_prompt,
            log_path=log_file,
            thread_ts=user_orig_thread,
            user_facing_summary=brain_result.user_facing_summary,
        )

        # (7) Run sub-agent
        session_id = await get_repo_session(repo_name)

        async def _on_agent_start(pid: int) -> None:
            import re as _re
            await session_log("AGENT_START", repo=repo_name, pid=pid, task_id=task_id)
            catchphrase = ""
            if agent.persona:
                m = _re.search(r"Catchphrase:\s*['\"](.+?)['\"]", agent.persona)
                if m:
                    catchphrase = f"_{m.group(1)}_\n"
            start_msg = f"{agent.emoji} {catchphrase}{repo_name.upper()} 작업 시작합니다."
            await self.reporter.send_chat(report_channel, start_msg, task_thread_ts)

        async def _on_agent_report(message: str) -> None:
            await session_log("AGENT_REPORT", repo=repo_name, message=message)
            await self.reporter.send_chat(report_channel, message, task_thread_ts)

        async def _on_agent_stream(text: str) -> None:
            await self.reporter.send_stream_event(report_channel, task_thread_ts, text)

        result = await self.runner.run(
            agent=agent,
            prompt=agent_prompt,
            task_logger=task_log,
            session_id=session_id,
            on_start=_on_agent_start,
            on_report=_on_agent_report,
            on_stream=_on_agent_stream,
        )

        # Save session_id so next call to this agent resumes the conversation
        if result.session_id:
            await set_repo_session(repo_name, result.session_id)

        # Check for ##ASK## — agent is requesting user input mid-task
        ask_lines = [l.strip() for l in result.stdout.splitlines() if l.strip().startswith("##ASK##")]
        if ask_lines and result.success:
            question = ask_lines[-1][len("##ASK##"):].strip()
            await pause_for_ask(task_id, question, result.session_id or "", task_thread_ts or "")
            await session_log("AGENT_ASK", repo=repo_name, task_id=task_id, question=question)
            await self.reporter.send_chat(report_channel, f"❓ *{repo_name.upper()}* 질문:\n{question}", task_thread_ts)
            return

        # ##CONVO## — agent flagged this as conversational, not a task
        if result.is_convo and result.success:
            convo_text = "\n".join(
                l for l in result.stdout.splitlines()
                if l.strip() != "##CONVO##" and not l.strip().startswith("##SLACK##")
            ).strip()
            if convo_text:
                await self.reporter.send_chat(report_channel, convo_text, task_thread_ts or user_orig_thread)
            await complete_task(task_id, convo_text[:200] if convo_text else "대화 완료")
            await add_conversation(role="assistant", content=convo_text, slack_channel=channel, slack_thread_ts=task_thread_ts)
            return

        # (9/10) Report outcome
        if result.success:
            summary = await self.brain.summarize_result(
                raw_output=result.stdout,
                user_request=user_text,
                repo=repo_name,
                persona=agent.persona,
            )
            await session_log(
                "AGENT_DONE",
                repo=repo_name,
                task_id=task_id,
                exit_code=result.exit_code,
                duration=f"{result.duration_s:.1f}s",
                summary=summary,
            )
            await task_log.write_result(result.exit_code, result.duration_s, summary)
            await complete_task(task_id, summary)
            await self.reporter.send_task_complete(
                repo=repo_name,
                emoji=agent.emoji,
                summary=summary,
                log_path=log_file,
                thread_ts=task_thread_ts,
                persona=agent.persona,
                duration_s=result.duration_s,
                channel=report_channel,
            )
            await add_conversation(
                role="assistant",
                content=summary,
                slack_channel=channel,
                slack_thread_ts=task_thread_ts,
            )
            # Post final result to results channel (files + links only)
            output_files = _detect_output_files(agent.path)
            notion_links = _extract_urls(result.stdout, domains=["notion.so", "notion.site"])
            drive_links = _extract_urls(result.stdout, domains=["docs.google.com", "drive.google.com"])
            await self.reporter.send_result(
                repo=repo_name,
                emoji=agent.emoji,
                user_request=user_text,
                summary=summary,
                output_files=output_files,
                extra_links=notion_links + drive_links,
            )
        else:
            last_output = (result.stdout + result.stderr)[-500:].strip()
            failure_reason = f"exit code {result.exit_code}"
            await session_log(
                "AGENT_FAILED",
                repo=repo_name,
                task_id=task_id,
                exit_code=result.exit_code,
                duration=f"{result.duration_s:.1f}s",
                last_output=last_output,
            )
            await task_log.write_result(result.exit_code, result.duration_s, failure_reason)
            await task_log.write_error(last_output)
            await fail_task(task_id, failure_reason)
            await self.reporter.send_task_failed(
                repo=repo_name,
                emoji=agent.emoji,
                reason=failure_reason,
                last_output=last_output,
                log_path=log_file,
                thread_ts=task_thread_ts,
                channel=report_channel,
            )
            failure_msg = f"{agent.emoji} {repo_name} 작업 실패했어요. 상세 로그 확인해주세요."
            await add_conversation(
                role="assistant",
                content=failure_msg,
                slack_channel=channel,
                slack_thread_ts=thread_ts,
            )


    async def _handle_direct_agent_message(
        self,
        agent_name: str,
        user_text: str,
        channel: str,
        thread_ts: str | None,
        message_ts: str | None,
    ) -> None:
        """Direct channel: skip Brain, route straight to named agent.
        No task_start notification — agent's ##CONVO## or ##SLACK## determines response style."""
        import uuid as _uuid
        agent = self.config.agents[agent_name]
        task_id = str(_uuid.uuid4())
        task_log = TaskLogger(task_id, agent_name, user_text)
        log_file = str(log_path(task_id))
        report_channel = channel

        await create_task(
            task_id=task_id,
            repo=agent_name,
            user_request=user_text,
            agent_prompt=user_text,
            routing_reason="direct_channel",
            routing_method="direct",
            slack_channel=channel,
            slack_thread_ts=thread_ts,
            log_path=log_file,
        )

        session_id = await get_repo_session(agent_name)
        # For direct channel, thread_ts anchors the reply thread
        reply_thread = thread_ts

        async def _on_report(message: str) -> None:
            await self.reporter.send_chat(report_channel, message, reply_thread)

        async def _on_stream(text: str) -> None:
            await self.reporter.send_stream_event(report_channel, reply_thread, text)

        result = await self.runner.run(
            agent=agent,
            prompt=user_text,
            task_logger=task_log,
            session_id=session_id,
            on_report=_on_report,
            on_stream=_on_stream,
        )

        if result.session_id:
            await set_repo_session(agent_name, result.session_id)

        # ##ASK## — agent needs Boss input before continuing
        ask_lines = [l.strip() for l in result.stdout.splitlines() if l.strip().startswith("##ASK##")]
        if ask_lines and result.success:
            question = ask_lines[-1][len("##ASK##"):].strip()
            await pause_for_ask(task_id, question, result.session_id or "", reply_thread or "")
            await self.reporter.send_chat(report_channel, f"❓ {question}", reply_thread)
            return

        # ##CONVO## — lightweight conversational response, no task banners
        if result.is_convo and result.success:
            convo_text = "\n".join(
                l for l in result.stdout.splitlines()
                if l.strip() != "##CONVO##" and not l.strip().startswith("##SLACK##")
            ).strip()
            if convo_text:
                await self.reporter.send_chat(report_channel, convo_text, reply_thread)
            await complete_task(task_id, convo_text[:200] if convo_text else "대화 완료")
            await add_conversation(role="assistant", content=convo_text, slack_channel=channel, slack_thread_ts=reply_thread)
            return

        # Task mode — full completion flow
        if result.success:
            summary = await self.brain.summarize_result(
                raw_output=result.stdout,
                user_request=user_text,
                repo=agent_name,
                persona=agent.persona,
            )
            await complete_task(task_id, summary)
            await self.reporter.send_task_complete(
                repo=agent_name, emoji=agent.emoji, summary=summary,
                log_path=log_file, thread_ts=reply_thread, persona=agent.persona,
                duration_s=result.duration_s, channel=report_channel,
            )
            await add_conversation(role="assistant", content=summary, slack_channel=channel, slack_thread_ts=reply_thread)
            output_files = _detect_output_files(agent.path)
            notion_links = _extract_urls(result.stdout, domains=["notion.so", "notion.site"])
            drive_links = _extract_urls(result.stdout, domains=["docs.google.com", "drive.google.com"])
            await self.reporter.send_result(
                repo=agent_name, emoji=agent.emoji, user_request=user_text,
                summary=summary, output_files=output_files,
                extra_links=notion_links + drive_links,
            )
        else:
            last_output = (result.stdout + result.stderr)[-500:].strip()
            await fail_task(task_id, f"exit code {result.exit_code}")
            await self.reporter.send_task_failed(
                repo=agent_name, emoji=agent.emoji, reason=f"exit code {result.exit_code}",
                last_output=last_output, log_path=log_file, thread_ts=reply_thread, channel=report_channel,
            )

    async def _resume_after_ask(
        self,
        waiting_task: dict,
        user_answer: str,
        channel: str,
        thread_ts: str | None,
    ) -> None:
        """Resume an agent that paused with ##ASK## after receiving user's answer."""
        task_id = waiting_task["task_id"]
        repo_name = waiting_task["repo"]
        agent = self.config.agents.get(repo_name)
        if not agent:
            return

        ask_session_id = waiting_task.get("ask_session_id") or ""
        ask_thread_ts = waiting_task.get("ask_thread_ts") or thread_ts
        report_channel = channel

        await mark_ask_resumed(task_id)
        await self.reporter.send_chat(report_channel, f"💬 답변 수신 — {repo_name.upper()} 작업 재개합니다.", ask_thread_ts)

        task_log = TaskLogger(task_id, repo_name, waiting_task.get("user_request", ""))
        log_file = str(log_path(task_id))

        resume_prompt = (
            f"Boss의 답변: {user_answer}\n\n"
            f"이전에 네가 물었던 질문: {waiting_task.get('ask_question', '')}\n\n"
            "위 답변을 반영해서 작업을 계속 진행해줘."
        )

        async def _on_report(message: str) -> None:
            await self.reporter.send_chat(report_channel, message, ask_thread_ts)

        async def _on_stream(text: str) -> None:
            await self.reporter.send_stream_event(report_channel, ask_thread_ts, text)

        result = await self.runner.run(
            agent=agent,
            prompt=resume_prompt,
            task_logger=task_log,
            session_id=ask_session_id or None,
            on_report=_on_report,
            on_stream=_on_stream,
        )

        if result.session_id:
            await set_repo_session(repo_name, result.session_id)

        # Handle chained ##ASK##
        ask_lines = [l.strip() for l in result.stdout.splitlines() if l.strip().startswith("##ASK##")]
        if ask_lines and result.success:
            question = ask_lines[-1][len("##ASK##"):].strip()
            await pause_for_ask(task_id, question, result.session_id or "", ask_thread_ts or "")
            await self.reporter.send_chat(report_channel, f"❓ *{repo_name.upper()}* 추가 질문:\n{question}", ask_thread_ts)
            return

        if result.success:
            summary = await self.brain.summarize_result(
                raw_output=result.stdout,
                user_request=waiting_task.get("user_request", ""),
                repo=repo_name,
                persona=agent.persona,
            )
            await complete_task(task_id, summary)
            await self.reporter.send_task_complete(
                repo=repo_name, emoji=agent.emoji, summary=summary,
                log_path=log_file, thread_ts=ask_thread_ts, persona=agent.persona,
                duration_s=result.duration_s, channel=report_channel,
            )
            await add_conversation(role="assistant", content=summary, slack_channel=channel, slack_thread_ts=ask_thread_ts)
            output_files = _detect_output_files(agent.path)
            notion_links = _extract_urls(result.stdout, domains=["notion.so", "notion.site"])
            drive_links = _extract_urls(result.stdout, domains=["docs.google.com", "drive.google.com"])
            await self.reporter.send_result(
                repo=repo_name, emoji=agent.emoji,
                user_request=waiting_task.get("user_request", ""),
                summary=summary, output_files=output_files,
                extra_links=notion_links + drive_links,
            )
        else:
            last_output = (result.stdout + result.stderr)[-500:].strip()
            await fail_task(task_id, f"exit code {result.exit_code}")
            await self.reporter.send_task_failed(
                repo=repo_name, emoji=agent.emoji, reason=f"exit code {result.exit_code}",
                last_output=last_output, log_path=log_file,
                thread_ts=ask_thread_ts, channel=report_channel,
            )


async def _validate_startup_token(reporter: Reporter, control_channel: str) -> None:
    """Validate OAuth token immediately on startup.
    Non-blocking — Jarvis continues regardless, but sends Slack alert if token is stale."""
    log.info("Validating OAuth token on startup…")
    try:
        await JarvisBrain._run_claude_cli("1+1=?", timeout=30.0)
        log.info("Startup OAuth token validation OK")
    except Exception as exc:
        log.warning("Startup OAuth token validation FAILED: %s", exc)
        try:
            await reporter.send_chat(
                control_channel,
                "⚠️ *OAuth 토큰 만료 — Friday 세션에서 토큰 갱신 필요*\n"
                "Jarvis가 시작됐지만 Brain CLI 인증에 실패했습니다.\n"
                "Friday 세션에서 OAuth 토큰 갱신 스크립트를 즉시 실행해주세요.\n"
                f"```{str(exc)[:200]}```",
            )
        except Exception as rep_exc:
            log.error("Failed to send startup token alert: %s", rep_exc)


async def _keep_warm_loop(reporter: Reporter, control_channel: str) -> None:
    """Ping claude CLI every 90 min to keep the Desktop OAuth bridge alive.
    Token refresh is handled by the 'jarvis-token-refresh' scheduled task (runs hourly).
    This loop is ping-only — it does NOT write the session file.
    Sends Slack alert to control channel if ping fails (token expired or bridge broken)."""
    INTERVAL = 90 * 60  # 90 minutes

    while True:
        await asyncio.sleep(INTERVAL)
        import os as _os
        env = {k: v for k, v in _os.environ.items() if k != "ANTHROPIC_API_KEY"}
        # Inject freshest session token so the ping itself can authenticate
        session_env = JarvisBrain._load_session_env()
        if session_env:
            env.update(session_env)
        cmd = ["cmd", "/c", "claude", "-p", "--model", "claude-haiku-4-5-20251001",
               "--output-format", "text", "--dangerously-skip-permissions"]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                creationflags=0x08000000,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(b"1+1=?"), timeout=30.0)
            if proc.returncode == 0:
                log.info("Keep-warm ping OK")
            else:
                out_text = stdout.decode("utf-8", errors="replace")[:200]
                err_text = stderr.decode("utf-8", errors="replace")[:200]
                log.warning("Keep-warm ping FAILED (exit %d) — stdout: %s | stderr: %s",
                            proc.returncode, out_text, err_text)
                try:
                    await reporter.send_chat(
                        control_channel,
                        "⚠️ *Keep-warm 토큰 만료 감지*\n"
                        "Brain CLI 인증 실패 — `jarvis-token-refresh` 스케줄 태스크 또는 Friday 세션 갱신을 확인해주세요.\n"
                        f"```exit {proc.returncode}: {(out_text or err_text)[:150]}```",
                    )
                except Exception:
                    pass
        except Exception as exc:
            log.warning("Keep-warm ping exception: %s", exc)
            try:
                await reporter.send_chat(
                    control_channel,
                    f"⚠️ *Keep-warm 오류*\nBrain CLI ping 실패: `{str(exc)[:150]}`",
                )
            except Exception:
                pass  # Don't let reporter failure kill the loop


async def main() -> None:
    jarvis = Jarvis()
    await init_db()
    stale = await cleanup_stale_tasks()
    if stale:
        log.info("Cleaned up %d stale tasks on startup", stale)
    await session_log("SYSTEM_START", agents=",".join(jarvis.config.agents.keys()), stale_cleaned=stale)
    log.info("Jarvis starting up…")

    listener = SlackListener(
        config=jarvis.config,
        on_message=jarvis.handle_message,
    )
    # Re-apply after SlackListener/AsyncApp init (bolt sets its own DEBUG level internally)
    _suppress_noisy_loggers()

    await jarvis.reporter.send_system_start(list(jarvis.config.agents.keys()))

    # Validate OAuth token immediately — alert via Slack if stale on startup
    await _validate_startup_token(jarvis.reporter, jarvis.config.slack.control_channel)

    listen_task = asyncio.create_task(listener.start())
    warm_task = asyncio.create_task(
        _keep_warm_loop(jarvis.reporter, jarvis.config.slack.control_channel)
    )
    try:
        await listen_task
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        warm_task.cancel()
        await session_log("SYSTEM_STOP")
        await jarvis.reporter.send_system_stop()
        await listener.handler.close_async()
        log.info("Jarvis shut down cleanly")


if __name__ == "__main__":
    asyncio.run(main())
