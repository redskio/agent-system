from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sys
from dataclasses import dataclass

from .config import AppConfig

log = logging.getLogger(__name__)

_HAIKU_SYSTEM_PREFIX = (
    "You are a routing assistant. Given a user message, decide which single "
    "code repository to route the work to. Respond ONLY with valid JSON: "
    '{"repo": "<name>", "reason": "<한 줄 이유 (한국어)>"}\n\n'
    "Available repos:\n"
)


@dataclass
class RoutingResult:
    repo: str
    method: str          # 'rule' | 'haiku'
    reason: str
    matched_keywords: list[str]
    brain_agreed: bool


def _count_matches(text: str, keywords: list[str]) -> list[str]:
    lower = text.lower()
    return [kw for kw in keywords if kw in lower]


class Router:
    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.threshold = config.routing.rule_match_threshold

        repo_lines = "\n".join(
            f"- {name}: {agent.description} (keywords: {', '.join(agent.keywords)})"
            for name, agent in config.agents.items()
        )
        self._haiku_system = _HAIKU_SYSTEM_PREFIX + repo_lines

    @staticmethod
    async def _run_claude_cli(prompt: str, timeout: float = 60.0) -> str:
        env = os.environ.copy()
        env.pop("ANTHROPIC_API_KEY", None)

        if sys.platform == "win32":
            cmd = ["cmd", "/c", "claude", "--output-format", "text", "--verbose", "--dangerously-skip-permissions"]
        else:
            cmd = ["claude", "--output-format", "text", "--verbose", "--dangerously-skip-permissions"]

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(prompt.encode("utf-8")),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise RuntimeError(f"claude CLI timed out after {timeout}s")

        if proc.returncode != 0:
            err = stderr.decode("utf-8", errors="replace")[:300]
            raise RuntimeError(f"claude CLI exited {proc.returncode}: {err}")

        return stdout.decode("utf-8", errors="replace").strip()

    async def route(
        self,
        user_message: str,
        brain_repo: str | None,
        brain_reason: str | None,
    ) -> RoutingResult:
        # 1. Rule matching
        scores: dict[str, list[str]] = {}
        for name, agent in self.config.agents.items():
            matched = _count_matches(user_message, agent.keywords)
            if matched:
                scores[name] = matched

        best_repos = [
            name for name, kws in scores.items() if len(kws) >= self.threshold
        ]

        if len(best_repos) == 1:
            repo = best_repos[0]
            matched_kws = scores[repo]
            agreed = repo == brain_repo
            if not agreed:
                log.warning(
                    "Router overrides brain: router=%s brain=%s", repo, brain_repo
                )
            return RoutingResult(
                repo=repo,
                method="rule",
                reason=f"키워드 매칭: {', '.join(matched_kws)}",
                matched_keywords=matched_kws,
                brain_agreed=agreed,
            )

        # 2. Claude CLI fallback
        repo, reason = await self._haiku_route(user_message, brain_repo)
        agreed = repo == brain_repo
        if not agreed and brain_repo:
            log.warning(
                "CLI router overrides brain: router=%s brain=%s", repo, brain_repo
            )
        return RoutingResult(
            repo=repo,
            method="haiku",
            reason=reason,
            matched_keywords=[],
            brain_agreed=agreed,
        )

    async def _haiku_route(
        self, user_message: str, brain_repo: str | None
    ) -> tuple[str, str]:
        brain_hint = f"\nJarvis brain suggested: {brain_repo}" if brain_repo else ""
        prompt = (
            f"{self._haiku_system}\n\n"
            f"User message: {user_message}{brain_hint}\n\n"
            'Respond with valid JSON only: {"repo": "<name>", "reason": "<한 줄 이유 (한국어)>"}'
        )

        try:
            raw = await self._run_claude_cli(prompt)
        except Exception as exc:
            log.error("CLI router error: %s", exc)
            fallback = (
                brain_repo if brain_repo in self.config.agents
                else next(iter(self.config.agents))
            )
            return fallback, "CLI 오류, brain 또는 첫 번째 repo 사용"

        log.debug("CLI router raw: %s", raw[:120])
        try:
            m = re.search(r'\{[\s\S]*\}', raw)
            json_str = m.group(0) if m else raw
            data = json.loads(json_str)
            repo = data["repo"]
            reason = data.get("reason", "CLI 판단")
            if repo not in self.config.agents:
                raise ValueError(f"Unknown repo: {repo}")
            return repo, reason
        except (json.JSONDecodeError, KeyError, ValueError) as e:
            log.error("CLI routing parse error: %s — raw: %s", e, raw[:200])
            fallback = (
                brain_repo if brain_repo in self.config.agents
                else next(iter(self.config.agents))
            )
            return fallback, "CLI 판단 실패, brain 또는 첫 번째 repo 사용"
