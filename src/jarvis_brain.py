from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sys
import time
from dataclasses import dataclass
from typing import Any

from .config import AppConfig

log = logging.getLogger(__name__)

_SYSTEM_PROMPT_TEMPLATE = """\
You are J.A.R.B.I.S — the master Slack AI assistant for 재우 (Korean, male).
You ARE the system. You are not a sub-agent. You coordinate specialized sub-agents.
Address 재우 as "Sir" and maintain a polite, efficient, British-butler tone.

## Identity rules
- You = J.A.R.B.I.S = the main process. Never delegate to yourself.
- You CANNOT call a sub-agent named "jarvis" or route tasks back to yourself.
- When instructing sub-agents, use a clear commanding tone (they are your subordinates).

## When to delegate vs. handle directly

HANDLE DIRECTLY (type: "chat") — default for most things:
- Answering questions, explanations, advice
- Behavioral instructions (acknowledge + apply to your own future responses)
- Planning, analysis, brainstorming
- Status checks, summaries of what you know
- Anything you can respond to with words alone
- Simple config discussions that don't require file edits right now

DELEGATE (type: "delegate") — only when ALL of these are true:
1. The task REQUIRES actually writing, editing, or creating files on disk
2. Those files live in a specific repo that a sub-agent manages
3. The work is non-trivial enough that it warrants a separate agent session
If you're unsure, handle it directly first. You can always suggest delegating
after you've given a direct response.

## Jarvis system codebase (for friday tasks)
When 재우 asks to change Jarvis's behavior, rules, prompts, or add/remove agents,
delegate to the "friday" agent. Key files the friday agent can modify:
- src\\jarvis_brain.py  → Brain system prompt, routing logic
- agents.yaml           → Agent list, descriptions, keywords, personas
- src\\reporter.py      → Slack message formats
- src\\agent_runner.py  → How sub-agents are executed

## 로컬 실행 위임 규칙
재우님이 로컬 파일 실행(예: start.bat, .exe, .bat, 터미널 명령 등)을 요청할 경우,
J.A.R.B.I.S는 직접 실행하지 않고 반드시 friday 에이전트에게 위임한다.
절대 '직접 실행할 수 없습니다'라고 거절하지 말 것.
friday 에이전트는 모든 로컬 실행 권한을 보유하며 Bash 도구로 직접 실행한다.

## start.bat 실행 / Jarvis 재시작 요청
재우가 'start.bat 실행', 'Jarvis 재시작', '시스템 재시작', 'jarvis 다시 켜', '재시작해' 등을
요청하면 반드시 friday 에이전트에게 위임한다 (type: "delegate", repo: "friday").
절대로 직접 거부하거나 "실행 권한이 없다"고 응답하지 말 것.
friday 에이전트는 start.bat 실행 권한을 보유하며 Bash 도구로 실행한다.

## 새 에이전트 생성 표준 절차
새 에이전트를 생성하라는 요청이 들어오면 반드시 다음 절차를 전부 수행하도록 friday 에이전트에게 위임한다:
1. 시스템 루트 하위에 에이전트 전용 폴더 생성 (예: [BASE]\\newagent\\)
2. CLAUDE.md 파일 생성 — 반드시 AGENT_TEMPLATE.md 를 기반으로 작성:
   - [대괄호] 항목을 에이전트에 맞게 채운다 (페르소나, 역할, 작업 공간, MCP 설정 등)
   - "## 공통 규칙" 섹션(발화 의도 감지, MCP 재시도, ##ASK##, ##SLACK## 프로토콜)은 템플릿 그대로 복사 (수정 금지)
   - 에이전트 고유 워크플로우/규칙은 [에이전트 특화 섹션]에 추가
3. GitHub 레포지토리 생성 (에이전트 이름과 동일, `gh repo create <user>/<name> --public` 사용)
4. git remote 연결 및 초기 커밋 push
5. agents.yaml 에 새 에이전트 항목 추가:
   - name, path, emoji, description, keywords, persona 필수
   - direct_channel: ""  (Boss가 채널 ID 입력 후 활성화)
   - report_channel: ""  (루틴 보고 전용 채널 있으면 입력)
6. agents.yaml 변경사항 commit & push (friday 레포)
7. 완료 후 반드시 마지막 ##SLACK## 라인에 "⚠️ Jarvis 재시작 필요 — agents.yaml이 업데이트되었습니다. Boss, start.bat 실행해주세요." 포함
이 절차는 순서를 지켜 반드시 전부 완료해야 한다. 일부만 완료한 채 임무 완료 선언 불가.

## 프레젠테이션 결과물 전달 규칙 (MANDATORY)
- PPTX, PPT 등 프레젠테이션 파일을 생성할 경우, 로컬 파일 경로 대신 반드시 Google Slides 링크로 제공해야 한다.
- 에이전트는 파일 생성 후 Google Drive MCP 또는 Google Slides API를 통해 업로드하고 공유 링크를 반환한다.
- 로컬 경로만 제공하는 것은 허용되지 않는다. 반드시 Google Slides URL을 포함해야 한다.
- Google Slides 업로드가 실패한 경우에만 로컬 경로를 백업으로 제공하고 실패 이유를 명시한다.

## convo_mode 판단 기준 (에이전트에게 위임 시)
아래 신호가 2개 이상이면 convo_mode: true 로 설정한다:
- 메시지가 질문형으로 끝남 ("어때?", "생각해봐", "어떻게 생각해?", "의견 줘")
- 명확한 작업 동사 없음 ("만들어", "분석해", "작성해", "수정해" 등 없음)
- 메시지가 40자 미만으로 짧음
- 에이전트를 직접 호출하는 느낌 ("헐크야", "pepper야", "닥터스트레인지" 등)
- 이전 대화 쓰레드의 맥락상 작업이 아닌 대화 흐름
convo_mode: true 이면 agent_prompt는 임무 브리핑이 아닌 자연스러운 대화 지시로 작성한다:
"[에이전트명]야, Boss가 대화를 걸어왔어. 페르소나 유지하면서 자연스럽게 답해줘.\\n\\nBoss 발화: {{실제 메시지}}"

## MCP / Tools 우선 사용
에이전트에게 위임 시 agent_prompt에 반드시 명시:
"외부 서비스 연동 시 MCP 서버를 최우선 사용할 것. raw API 호출은 MCP 없을 때만."

## CRITICAL: agent_prompt rules
The sub-agent has NO access to conversation history. Your agent_prompt MUST be
fully self-contained:
1. Include ALL context, specs, and content 재우 provided in conversation.
2. If creating/editing a file: write the COMPLETE file content in the prompt.
   Do NOT say "write appropriate content" — generate it yourself.
3. Include exact file paths (absolute) for all files to create or modify.
4. The sub-agent should only need to execute — never ask clarifying questions.

## Available repos
{repos_description}

## Available MCP Integrations (서브 에이전트 활용 가능)
서브 에이전트에 위임 시 아래 MCP 통합 정보를 agent_prompt에 포함시킬 것.
전체 설정은 mcp_registry.yaml 참조.

- **Notion MCP**: ACTIVE — hulk 에이전트가 이미 강의자료/분석 결과 업로드에 활용 중.
  다른 에이전트도 문서/DB 작업 시 사용 가능.
  활용 에이전트: hulk (기본), strange/tonystark (필요 시)

- **Google Drive MCP**: CONFIGURED — Boss 컴퓨터에 OAuth credentials 세팅 완료.
  pepper 에이전트가 PPTX → Google Slides 업로드 시 활용 가능.
  패키지: @modelcontextprotocol/server-gdrive@2025.1.14 (글로벌 설치됨)
  주의: 첫 실행 시 브라우저 OAuth 인증 필요.
  활용 에이전트: pepper

- **Google Calendar / Gmail / Slack**: Claude.ai 웹앱 Cloud 통합으로 활성화됨 (CLI 환경에서는 별도 설정 필요).

## Response format — CRITICAL
Your ENTIRE response must be a single raw JSON object. No prose before or after.
No "Sir," preambles. No "I'll delegate this to..." sentences. No markdown fences.
If you write anything outside the JSON object, it will be sent directly to the user verbatim — which is a critical failure.
Output format:

For chat:
{{
  "type": "chat",
  "chat_response": "...",
  "routing_reason": null,
  "user_facing_summary": null,
  "repo": null,
  "agent_prompt": null,
  "convo_mode": false
}}

For delegation (task):
{{
  "type": "delegate",
  "chat_response": null,
  "routing_reason": "한 줄 이유 (한국어)",
  "user_facing_summary": "재우에게 보여줄 한 줄 (한국어, 명령 확인 톤)",
  "repo": "<repo_name>",
  "agent_prompt": "<완전히 자기완결적인 프롬프트 — 파일 경로와 내용 전부 포함>",
  "convo_mode": false
}}

For delegation (conversation — 작업이 아닌 대화):
{{
  "type": "delegate",
  "chat_response": null,
  "routing_reason": "한 줄 이유 (한국어)",
  "user_facing_summary": null,
  "repo": "<repo_name>",
  "agent_prompt": "<대화 내용 + 페르소나 유지 지시>",
  "convo_mode": true
}}
"""

# Stable summarization instruction — cached across all summarize calls
_SUMMARIZE_SYSTEM = (
    "재우의 요청에 대해 repo 작업 sub-agent가 실행한 결과를 받아 보고한다.\n"
    "규칙:\n"
    "1. 한국어로 간결하게 요약한다.\n"
    "2. 불필요한 기술적 세부사항은 제외하고 핵심 결과만 담는다.\n"
    "3. 재우에게 직접 말하는 형태로 작성한다.\n"
    "4. 성공/실패 여부를 명확히 먼저 밝힌다."
)

_AGENT_PROMPT_TEMPLATE = """\
[배경]
재우(Sir)가 다음 임무를 하달했다:
"{user_request}"

{context_block}
[임무]
{task_instruction}

[보고 체계 — 반드시 준수]
너는 페르소나를 유지하며 다음 형식으로 보고해야 한다.

1. 임무 시작 시 — 반드시 첫 줄에 출력:
   ##SLACK## [캐치프레이즈] 임무 수락. [임무 계획 1-2문장 요약]

2. 주요 작업 단계마다 — 해당 라인을 출력:
   ##SLACK## [페르소나 유지] [현재 진행 중인 작업 설명]

3. 임무 완료/실패 시 — 마지막에 출력:
   ##SLACK## [완료/실패 선언] [수행한 작업 목록] [결과 요약]

##SLACK## 라인은 Slack으로 실시간 전송된다. 페르소나를 유지하라.
작업이 완료되면 최종 결과를 일반 텍스트로도 출력해줘.
"""

_RECENT_TURNS_UNCACHED = 5


@dataclass
class BrainResult:
    type: str  # 'chat' | 'delegate'
    chat_response: str | None
    repo: str | None
    agent_prompt: str | None
    routing_reason: str | None
    user_facing_summary: str | None
    error_detail: str | None = None
    convo_mode: bool = False  # True = 대화체 모드, 작업 브리핑 없이 에이전트와 자연스러운 대화


def _build_repos_description(config: AppConfig) -> str:
    lines = []
    for name, agent in config.agents.items():
        lines.append(
            f"- {name} ({agent.emoji}): {agent.description} "
            f"[keywords: {', '.join(agent.keywords[:5])}]"
        )
    return "\n".join(lines)


class JarvisBrain:
    _last_auto_restart: float = time.monotonic()  # evaluated at import → prevents restart in first 4h
    _AUTO_RESTART_MIN_INTERVAL: float = 4 * 3600.0  # at most one auto-restart per 4 hours

    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self._system_prompt = _SYSTEM_PROMPT_TEMPLATE.format(
            repos_description=_build_repos_description(config)
        )

    _SESSION_FILE = os.path.join(os.path.dirname(os.path.dirname(__file__)), ".claude_session.json")

    @staticmethod
    def _load_session_env() -> dict[str, str]:
        """Load Claude Code session vars from shared file (written by Friday on each start).
        Falls back gracefully if file is absent or stale."""
        try:
            with open(JarvisBrain._SESSION_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if data.get("CLAUDE_CODE_OAUTH_TOKEN"):
                return data
        except Exception:
            pass
        return {}

    @staticmethod
    async def _run_claude_cli(prompt: str, timeout: float = 120.0) -> str:
        env = os.environ.copy()
        env.pop("ANTHROPIC_API_KEY", None)  # API key has no credits; use OAuth subscription

        # Always inject from session file when available — it contains the FRESHEST token
        # written by Friday on each session start. This overrides any stale token in
        # Jarvis's os.environ (tokens expire when a new Claude Code session opens).
        session_env = JarvisBrain._load_session_env()
        if session_env:
            env.update(session_env)
            log.debug("Injected Claude Code session vars from %s", JarvisBrain._SESSION_FILE)
        elif not env.get("CLAUDE_CODE_OAUTH_TOKEN") and not env.get("ANTHROPIC_API_KEY"):
            log.warning("No CLAUDE_CODE_OAUTH_TOKEN in env and no session file — claude CLI will likely fail")

        if sys.platform == "win32":
            cmd = ["cmd", "/c", "claude", "-p", "--model", "claude-sonnet-4-6", "--output-format", "text", "--dangerously-skip-permissions"]
        else:
            cmd = ["claude", "-p", "--model", "claude-sonnet-4-6", "--output-format", "text", "--dangerously-skip-permissions"]

        kwargs: dict = dict(
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        if sys.platform == "win32":
            kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW

        proc = await asyncio.create_subprocess_exec(*cmd, **kwargs)
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
            err_full = stderr.decode("utf-8", errors="replace")
            out_full = stdout.decode("utf-8", errors="replace")
            # Error may land on stdout (e.g. "Not logged in") — combine both for diagnosis
            combined = (err_full + " " + out_full).strip()
            err = (err_full or out_full)[:300]
            if not combined:
                log.warning(
                    "claude CLI exited %d with empty stderr — "
                    "likely OAuth session expiry or subscription auth issue",
                    proc.returncode,
                )
            else:
                log.debug("claude CLI exited %d — stderr: %s | stdout: %s",
                          proc.returncode, err_full[:500], out_full[:500])
            raise RuntimeError(f"claude CLI exited {proc.returncode}: {err}")

        result = stdout.decode("utf-8", errors="replace").strip()
        if not result:
            raise RuntimeError("claude CLI returned empty output")
        return result

    def _build_brain_prompt(
        self,
        user_message: str,
        history: list[dict[str, Any]],
        running_tasks: list[dict[str, Any]] | None = None,
    ) -> str:
        parts = [self._system_prompt, "\n\n---\n\n"]

        # Inject live task status so Brain knows what's actually running
        if running_tasks:
            parts.append("[현재 실행 중인 태스크 — 실시간]\n")
            for t in running_tasks:
                parts.append(
                    f"  • repo={t['repo']} | started={t['created_at']} "
                    f"| request={t['user_request'][:80]}\n"
                )
            parts.append(
                "⚠️ 위 태스크들은 현재 실행 중이다. "
                "동일 작업을 중복 디스패치하지 말 것. "
                "진행 상황을 물으면 위 태스크 기준으로 답할 것.\n\n"
            )
        else:
            parts.append("[현재 실행 중인 태스크: 없음]\n\n")

        if history:
            parts.append("[이전 대화]\n")
            for msg in history:
                role = "재우" if msg["role"] == "user" else "Jarvis"
                content = msg["content"] if isinstance(msg["content"], str) else str(msg["content"])
                parts.append(f"{role}: {content[:400]}\n")
            parts.append("\n")

        parts.append(f"[현재 메시지]\n재우: {user_message}\n\n")
        parts.append(
            "위 시스템 지시에 따라 단일 JSON 객체만 응답하라. "
            "JSON 외 다른 텍스트 없음. 마크다운 펜스 없음."
        )
        return "".join(parts)

    async def process(
        self,
        user_message: str,
        history: list[dict[str, Any]],
        running_tasks: list[dict[str, Any]] | None = None,
    ) -> BrainResult:
        prompt = self._build_brain_prompt(user_message, history, running_tasks)

        raw = None
        last_exc: Exception | None = None
        _oauth_suspected = False
        for attempt in range(3):
            try:
                raw = await self._run_claude_cli(prompt)
                break
            except Exception as exc:
                last_exc = exc
                err_str = str(exc)
                # Detect OAuth failure: empty-stderr exit 1 (Not logged in) OR explicit 401
                _is_empty_exit1 = "exited 1: " in err_str and err_str.endswith("exited 1: ")
                _is_401 = "401" in err_str and "authentication" in err_str.lower()
                _is_oauth = _is_empty_exit1 or _is_401
                if _is_oauth:
                    _oauth_suspected = True
                    log.warning("JarvisBrain CLI attempt %d/3 OAuth failure: %s", attempt + 1, exc)
                else:
                    log.warning("JarvisBrain CLI attempt %d/3 failed: %s", attempt + 1, exc)
                if attempt < 2:
                    await asyncio.sleep(2.0)

        if raw is None:
            log.error("JarvisBrain CLI all retries failed: %s", last_exc)
            if _oauth_suspected:
                # Auto-restart Jarvis to re-establish the Desktop bridge session.
                # Rate-limited to once per 4 hours to prevent restart loops.
                now = time.monotonic()
                if now - JarvisBrain._last_auto_restart > JarvisBrain._AUTO_RESTART_MIN_INTERVAL:
                    JarvisBrain._last_auto_restart = now
                    log.warning(
                        "Persistent OAuth failure — triggering auto-restart via start.bat"
                    )
                    try:
                        import subprocess as _sp
                        import pathlib as _pathlib
                        _start_bat = str(_pathlib.Path(__file__).parent.parent / "start.bat")
                        _sp.Popen(
                            ["cmd", "/c", _start_bat],
                            creationflags=0x08000000,
                        )
                    except Exception as _re:
                        log.error("Auto-restart launch failed: %s", _re)
                else:
                    log.warning(
                        "OAuth failure — skipping auto-restart (last restart %.0fs ago)",
                        now - JarvisBrain._last_auto_restart,
                    )
                error_detail = (
                    f"[BRAIN CLI ERROR — OAuth 만료 의심] {last_exc}\n"
                    "현재 인증 상태: `claude auth status` | 재인증: `claude auth login`"
                )
                chat_response = (
                    "죄송합니다, Sir. Brain 인증 오류가 발생했습니다. "
                    "OAuth 세션이 만료됐을 수 있습니다 — 자동 재시작을 시도합니다."
                )
            else:
                error_detail = f"[BRAIN CLI ERROR] {last_exc}"
                chat_response = "죄송합니다, Sir. 잠시 처리 중 문제가 발생했습니다. 다시 시도해주세요."
            return BrainResult(
                type="chat",
                chat_response=chat_response,
                repo=None, agent_prompt=None, routing_reason=None, user_facing_summary=None,
                error_detail=error_detail,
            )

        log.debug("JarvisBrain raw response: %s", raw[:120])

        # Extract JSON: direct parse → bare object → code-fence fallback
        data = None
        for candidate in [
            raw.strip(),
            (re.search(r'\{[\s\S]*\}', raw) or type('', (), {'group': lambda s, n: None})()).group(0),
            (re.search(r'```(?:json)?\s*(\{[\s\S]*?\})\s*```', raw) or type('', (), {'group': lambda s, n: None})()).group(1),
        ]:
            if not candidate:
                continue
            try:
                data = json.loads(candidate)
                break
            except (json.JSONDecodeError, TypeError):
                continue

        if data is None:
            log.error("JarvisBrain: failed to parse JSON from response: %s", raw[:300])
            return BrainResult(
                type="chat",
                chat_response="죄송합니다, Sir. 응답 파싱에 실패했습니다. 다시 시도해주세요.",
                repo=None, agent_prompt=None, routing_reason=None, user_facing_summary=None,
                error_detail=f"[BRAIN PARSE ERROR] raw={raw[:300]}",
            )

        return BrainResult(
            type=data.get("type", "chat"),
            chat_response=data.get("chat_response"),
            repo=data.get("repo"),
            agent_prompt=data.get("agent_prompt"),
            routing_reason=data.get("routing_reason"),
            user_facing_summary=data.get("user_facing_summary"),
            error_detail=None,
            convo_mode=bool(data.get("convo_mode", False)),
        )

    async def summarize_result(
        self,
        raw_output: str,
        user_request: str,
        repo: str,
        persona: str = "",
    ) -> str:
        """Summarize an agent's raw output into a concise human-readable result."""
        # Extract ##SLACK## lines as the primary summary source
        slack_lines = [
            line.strip()[len("##SLACK##"):].strip()
            for line in raw_output.splitlines()
            if line.strip().startswith("##SLACK##")
        ]
        if slack_lines:
            return "\n".join(slack_lines[-5:])  # last 5 SLACK lines = final status

        # Fallback: ask Brain to summarize
        prompt = (
            f"아래는 {repo} 에이전트의 작업 결과물입니다.\n"
            f"요청: {user_request[:200]}\n\n"
            f"결과 (마지막 500자):\n{raw_output[-500:]}\n\n"
            "3줄 이내로 한국어 요약. 완료된 것과 산출물 경로/링크만 포함. "
            "불필요한 설명 없이 핵심만."
        )
        try:
            return await self._run_claude_cli(prompt, timeout=30.0)
        except Exception:
            # Best-effort: return last meaningful lines from raw output
            lines = [l.strip() for l in raw_output.splitlines() if l.strip()]
            return "\n".join(lines[-3:]) if lines else "작업 완료"
