import os
from pathlib import Path
from dataclasses import dataclass, field

import yaml
from dotenv import dotenv_values

BASE_DIR = Path(__file__).parent.parent

# Read credentials directly from .env file — avoids conflict with shell env vars
_env = dotenv_values(BASE_DIR / ".env")


@dataclass
class AgentConfig:
    name: str
    path: str
    emoji: str
    description: str
    keywords: list[str]
    persona: str = ""
    report_channel: str = ""  # override channel for this agent's task reports
    direct_channel: str = ""
    model: str = ""  # override claude model for this agent (e.g. claude-opus-4-7)


@dataclass
class SlackConfig:
    control_channel: str
    results_channel: str = ""
    briefing_channel: str = ""
    verbose_channel: str | None = None


@dataclass
class RoutingConfig:
    rule_match_threshold: int = 2
    fallback_model: str = "claude-haiku-4-5"


@dataclass
class JarvisBrainConfig:
    model: str = "claude-sonnet-4-6"
    history_window: int = 30


@dataclass
class AppConfig:
    agents: dict[str, AgentConfig]
    slack: SlackConfig
    routing: RoutingConfig
    jarvis_brain: JarvisBrainConfig

    # env-sourced
    anthropic_api_key: str = ""
    slack_bot_token: str = ""
    slack_app_token: str = ""
    authorized_user_id: str = ""


def load_config(agents_yaml: str = str(BASE_DIR / "agents.yaml")) -> AppConfig:
    with open(agents_yaml, encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    agents: dict[str, AgentConfig] = {}
    for name, data in raw.get("agents", {}).items():
        path = data["path"]
        # Allow any absolute path — deployable version supports any install location
        if not os.path.isabs(path.replace("\\", "/")):
            raise ValueError(f"Agent path must be absolute: {path}")
        agents[name] = AgentConfig(
            name=name,
            path=path,
            emoji=data.get("emoji", "🤖"),
            description=data.get("description", ""),
            keywords=[k.lower() for k in data.get("keywords", [])],
            persona=data.get("persona", ""),
            report_channel=data.get("report_channel", ""),
            direct_channel=data.get("direct_channel", ""),
            model=data.get("model", ""),
        )

    slack_raw = raw.get("slack", {})
    slack = SlackConfig(
        control_channel=slack_raw.get("control_channel", "#jarvis-control"),
        results_channel=slack_raw.get("results_channel", ""),
        briefing_channel=slack_raw.get("briefing_channel", ""),
        verbose_channel=slack_raw.get("verbose_channel"),
    )

    routing_raw = raw.get("routing", {})
    routing = RoutingConfig(
        rule_match_threshold=routing_raw.get("rule_match_threshold", 2),
        fallback_model=routing_raw.get("fallback_model", "claude-haiku-4-5"),
    )

    brain_raw = raw.get("jarvis_brain", {})
    jarvis_brain = JarvisBrainConfig(
        model=brain_raw.get("model", "claude-sonnet-4-6"),
        history_window=brain_raw.get("history_window", 30),
    )

    return AppConfig(
        agents=agents,
        slack=slack,
        routing=routing,
        jarvis_brain=jarvis_brain,
        anthropic_api_key=_env.get("ANTHROPIC_API_KEY", ""),
        slack_bot_token=_env.get("SLACK_BOT_TOKEN", ""),
        slack_app_token=_env.get("SLACK_APP_TOKEN", ""),
        authorized_user_id=_env.get("AUTHORIZED_USER_ID", ""),
    )
