from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path

from slack_sdk.web.async_client import AsyncWebClient
from slack_sdk.errors import SlackApiError

from .config import AppConfig

log = logging.getLogger(__name__)


def _log_file_url(log_path: str) -> str:
    # Convert Windows path to file:// URL
    p = Path(log_path).as_posix()
    if not p.startswith("/"):
        p = "/" + p
    return f"file://{p}"


class Reporter:
    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.client = AsyncWebClient(token=config.slack_bot_token)
        self.control_channel = config.slack.control_channel
        # Deduplication: last streamed text per (channel, thread_ts)
        self._last_stream: dict[tuple[str, str | None], str] = {}

    async def send(
        self,
        channel: str,
        text: str,
        thread_ts: str | None = None,
    ) -> str | None:
        try:
            kwargs: dict = {"channel": channel, "text": text}
            if thread_ts is not None:
                kwargs["thread_ts"] = thread_ts
            resp = await self.client.chat_postMessage(**kwargs)
            return resp.get("ts")
        except SlackApiError as e:
            log.error("Slack send failed: %s", e)
            return None

    async def send_task_start(
        self,
        repo: str,
        emoji: str,
        routing_reason: str,
        user_request: str,
        agent_prompt: str,
        log_path: str,
        thread_ts: str | None = None,
        user_facing_summary: str | None = None,
        started_at: str | None = None,
    ) -> str | None:
        from datetime import datetime, timezone, timedelta
        kst = timezone(timedelta(hours=9))
        started_at = started_at or datetime.now(kst).strftime("%H:%M")
        brief = user_facing_summary or user_request[:120]
        text = (
            f"{emoji} *{repo.upper()}*에게 작업을 지시합니다.\n"
            f"지시받은 업무: {brief}\n"
            f"⏱️ 시작: {started_at} KST"
        )
        return await self.send(self.control_channel, text, thread_ts)

    async def send_task_complete(
        self,
        repo: str,
        emoji: str,
        summary: str,
        log_path: str,
        thread_ts: str | None = None,
        persona: str = "",
        duration_s: float | None = None,
        channel: str | None = None,
    ) -> str | None:
        opener = ""
        if persona:
            m = re.search(r"Catchphrase:\s*['\"](.+?)['\"]", persona)
            if m:
                opener = f"_{m.group(1)}_\n\n"
        duration_str = ""
        if duration_s is not None:
            mins, secs = divmod(int(duration_s), 60)
            duration_str = f"\n⏱️ 소요 시간: {mins}분 {secs}초" if mins else f"\n⏱️ 소요 시간: {secs}초"
        text = f"✅ {emoji} *{repo.upper()}* 작업 완료{duration_str}\n\n{opener}{summary}"
        return await self.send(channel or self.control_channel, text, thread_ts)

    async def send_task_failed(
        self,
        repo: str,
        emoji: str,
        reason: str,
        last_output: str,
        log_path: str,
        thread_ts: str | None = None,
        channel: str | None = None,
    ) -> str | None:
        text = (
            f"❌ *{emoji} {repo.upper()}* 작업 실패\n\n"
            f"*원인:* {reason}\n"
            f"*마지막 출력:* `{last_output[:300]}`\n\n"
            f"📄 상세 로그: `{_log_file_url(log_path)}`"
        )
        return await self.send(channel or self.control_channel, text, thread_ts)

    async def react(self, channel: str, message_ts: str, emoji: str) -> None:
        try:
            await self.client.reactions_add(channel=channel, timestamp=message_ts, name=emoji)
        except SlackApiError as e:
            log.debug("react failed (already exists?): %s", e)

    async def send_result(
        self,
        repo: str,
        emoji: str,
        user_request: str,
        summary: str,
        output_files: list[str] | None = None,
        extra_links: list[str] | None = None,
        skip_results_channel: bool = False,
    ) -> None:
        """Post final result (files + links only) to results channel."""
        if skip_results_channel:
            return

        task_one_liner = user_request[:80].replace("\n", " ")
        lines = [
            f"{emoji} *{repo.upper()}* 작업 완료",
            f"📋 {task_one_liner}",
        ]

        if extra_links:
            unique = list(dict.fromkeys(extra_links))[:3]
            for u in unique:
                lines.append(f"🔗 {u}")

        text = "\n".join(lines)
        await self.send(self.config.slack.results_channel, text)

        if output_files:
            for fpath in output_files[:3]:
                try:
                    import aiofiles
                    async with aiofiles.open(fpath, "rb") as f:
                        data = await f.read()
                    fname = fpath.replace("\\", "/").split("/")[-1]
                    await self.client.files_upload_v2(
                        channel=self.config.slack.results_channel,
                        filename=fname,
                        content=data,
                    )
                except Exception as exc:
                    log.warning("Failed to upload result file %s: %s", fpath, exc)

    async def send_system_start(self, agent_names: list[str]) -> None:
        agents_str = " · ".join(f"`{a}`" for a in agent_names)
        text = (
            f"⚡ *J.A.R.B.I.S. 온라인.*\n"
            f"모든 시스템 가동 완료. 에이전트 준비 중: {agents_str}\n"
            f"_\"At your service, Sir.\"_"
        )
        await self.send(self.control_channel, text)

    async def send_system_stop(self) -> None:
        text = (
            "🔴 *J.A.R.B.I.S. 오프라인.*\n"
            "모든 시스템 종료. 다음 재가동 시까지 대기합니다.\n"
            "_\"Good night, Sir.\"_"
        )
        await self.send(self.control_channel, text)

    async def send_chat(
        self,
        channel: str,
        text: str,
        thread_ts: str | None = None,
    ) -> str | None:
        return await self.send(channel, text, thread_ts)

    async def send_stream_event(
        self,
        channel: str,
        thread_ts: str | None,
        text: str,
    ) -> None:
        """Send one streaming event to a thread. Skips blanks, short texts, exact duplicates."""
        clean = text.strip()
        if len(clean) < 10:
            return
        key = (channel, thread_ts)
        if self._last_stream.get(key) == clean:
            return
        self._last_stream[key] = clean
        try:
            await self.send(channel, clean, thread_ts)
        except Exception as exc:
            log.debug("send_stream_event failed (best-effort): %s", exc)
