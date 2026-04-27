from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import aiosqlite

DB_PATH = Path(__file__).parent.parent / "jarvis.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS conversation_history (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  role TEXT NOT NULL,
  content TEXT NOT NULL,
  slack_channel TEXT,
  slack_thread_ts TEXT,
  slack_message_ts TEXT,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_conv_created ON conversation_history(created_at);

CREATE TABLE IF NOT EXISTS tasks (
  task_id TEXT PRIMARY KEY,
  repo TEXT NOT NULL,
  user_request TEXT NOT NULL,
  agent_prompt TEXT NOT NULL,
  routing_reason TEXT,
  routing_method TEXT,
  status TEXT NOT NULL,
  slack_channel TEXT,
  slack_thread_ts TEXT,
  log_path TEXT,
  result_summary TEXT,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  completed_at TIMESTAMP
);

CREATE TABLE IF NOT EXISTS repo_sessions (
  repo TEXT PRIMARY KEY,
  session_id TEXT,
  last_used_at TIMESTAMP
);
"""


async def init_db() -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(_SCHEMA)
        await db.commit()

    # Migration: add ask columns if not present
    async with aiosqlite.connect(DB_PATH) as db:
        for col, typedef in [
            ("ask_question", "TEXT"),
            ("ask_session_id", "TEXT"),
            ("ask_thread_ts", "TEXT"),
        ]:
            try:
                await db.execute(f"ALTER TABLE tasks ADD COLUMN {col} {typedef}")
                await db.commit()
            except Exception:
                pass  # Column already exists


async def cleanup_stale_tasks() -> int:
    """Mark any tasks still 'running' as failed — called on startup to clear orphans."""
    async with aiosqlite.connect(DB_PATH) as db:
        result = await db.execute(
            """UPDATE tasks SET status='failed',
               result_summary='stale — Jarvis restarted before completion',
               completed_at=CURRENT_TIMESTAMP
               WHERE status IN ('running', 'waiting_for_ask')"""
        )
        await db.commit()
        return result.rowcount


_CONTENT_LIMIT = 2000  # chars stored per turn — keeps history token-efficient

async def add_conversation(
    role: str,
    content: str,
    slack_channel: str | None = None,
    slack_thread_ts: str | None = None,
    slack_message_ts: str | None = None,
) -> None:
    if len(content) > _CONTENT_LIMIT:
        content = content[:_CONTENT_LIMIT] + " …[truncated]"
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT INTO conversation_history
               (role, content, slack_channel, slack_thread_ts, slack_message_ts)
               VALUES (?, ?, ?, ?, ?)""",
            (role, content, slack_channel, slack_thread_ts, slack_message_ts),
        )
        await db.commit()


async def get_recent_history(
    limit: int = 30,
    slack_channel: str | None = None,
) -> list[dict[str, Any]]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        if slack_channel:
            async with db.execute(
                """SELECT role, content FROM conversation_history
                   WHERE slack_channel = ?
                   ORDER BY created_at DESC LIMIT ?""",
                (slack_channel, limit),
            ) as cursor:
                rows = await cursor.fetchall()
        else:
            async with db.execute(
                """SELECT role, content FROM conversation_history
                   ORDER BY created_at DESC LIMIT ?""",
                (limit,),
            ) as cursor:
                rows = await cursor.fetchall()
    return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]


async def get_channel_history(
    slack_channel: str,
    limit: int = 10,
) -> list[dict[str, Any]]:
    """Fetch recent history for a specific channel — for cross-channel context injection."""
    return await get_recent_history(limit=limit, slack_channel=slack_channel)


async def create_task(
    task_id: str,
    repo: str,
    user_request: str,
    agent_prompt: str,
    routing_reason: str,
    routing_method: str,
    slack_channel: str | None,
    slack_thread_ts: str | None,
    log_path: str,
) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT INTO tasks
               (task_id, repo, user_request, agent_prompt, routing_reason,
                routing_method, status, slack_channel, slack_thread_ts, log_path)
               VALUES (?, ?, ?, ?, ?, ?, 'running', ?, ?, ?)""",
            (
                task_id, repo, user_request, agent_prompt, routing_reason,
                routing_method, slack_channel, slack_thread_ts, log_path,
            ),
        )
        await db.commit()


async def complete_task(task_id: str, result_summary: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """UPDATE tasks SET status='completed', result_summary=?,
               completed_at=CURRENT_TIMESTAMP WHERE task_id=?""",
            (result_summary, task_id),
        )
        await db.commit()


async def fail_task(task_id: str, result_summary: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """UPDATE tasks SET status='failed', result_summary=?,
               completed_at=CURRENT_TIMESTAMP WHERE task_id=?""",
            (result_summary, task_id),
        )
        await db.commit()


async def get_running_tasks() -> list[dict[str, Any]]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT task_id, repo, user_request, created_at, slack_thread_ts, slack_channel
               FROM tasks WHERE status='running'
               ORDER BY created_at DESC"""
        ) as cursor:
            rows = await cursor.fetchall()
    return [dict(r) for r in rows]


async def get_task(task_id: str) -> dict[str, Any] | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM tasks WHERE task_id=?", (task_id,)
        ) as cursor:
            row = await cursor.fetchone()
    return dict(row) if row else None


# Phase 4 hooks (schema pre-wired, logic deferred)
async def get_repo_session(repo: str) -> str | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT session_id FROM repo_sessions WHERE repo=?", (repo,)
        ) as cursor:
            row = await cursor.fetchone()
    return row["session_id"] if row else None


async def set_repo_session(repo: str, session_id: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT INTO repo_sessions (repo, session_id, last_used_at)
               VALUES (?, ?, CURRENT_TIMESTAMP)
               ON CONFLICT(repo) DO UPDATE SET
                 session_id=excluded.session_id,
                 last_used_at=CURRENT_TIMESTAMP""",
            (repo, session_id),
        )
        await db.commit()


async def pause_for_ask(
    task_id: str,
    ask_question: str,
    ask_session_id: str,
    ask_thread_ts: str,
) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """UPDATE tasks SET status='waiting_for_ask',
               ask_question=?, ask_session_id=?, ask_thread_ts=?
               WHERE task_id=?""",
            (ask_question, ask_session_id, ask_thread_ts, task_id),
        )
        await db.commit()


async def get_waiting_ask_by_thread(thread_ts: str) -> dict[str, Any] | None:
    """Return the first waiting_for_ask task in this thread, or None."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT task_id, repo, user_request, ask_question,
                      ask_session_id, ask_thread_ts, slack_channel
               FROM tasks
               WHERE status='waiting_for_ask' AND ask_thread_ts=?
               ORDER BY created_at DESC LIMIT 1""",
            (thread_ts,),
        ) as cursor:
            row = await cursor.fetchone()
    return dict(row) if row else None


async def mark_ask_resumed(task_id: str) -> None:
    """Mark a waiting_for_ask task back to 'running' when user answers."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE tasks SET status='running' WHERE task_id=?",
            (task_id,),
        )
        await db.commit()
