#!/usr/bin/env python3
"""
Agent System Setup Wizard
Run: python setup.py

Sets up the multi-agent AI system from scratch on a new machine.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

BASE_DIR = Path(__file__).parent.resolve()
TEMPLATES_DIR = BASE_DIR / "templates"
AGENTS_DIR = BASE_DIR / "templates" / "agents"


def _color(text: str, code: str) -> str:
    if sys.platform == "win32":
        return text
    return f"\033[{code}m{text}\033[0m"

def green(t): return _color(t, "32")
def yellow(t): return _color(t, "33")
def cyan(t): return _color(t, "36")
def bold(t): return _color(t, "1")
def red(t): return _color(t, "31")


def ask(prompt: str, default: str = "", secret: bool = False) -> str:
    """Prompt user for input with optional default."""
    hint = f" [{default}]" if default else ""
    try:
        if secret:
            import getpass
            val = getpass.getpass(f"  {prompt}{hint}: ")
        else:
            val = input(f"  {prompt}{hint}: ").strip()
        return val if val else default
    except (KeyboardInterrupt, EOFError):
        print("\nSetup cancelled.")
        sys.exit(0)


def ask_bool(prompt: str, default: bool = True) -> bool:
    hint = "[Y/n]" if default else "[y/N]"
    val = ask(f"{prompt} {hint}", "").lower()
    if not val:
        return default
    return val in ("y", "yes")


def section(title: str) -> None:
    print(f"\n{bold(cyan('─' * 50))}")
    print(f"  {bold(title)}")
    print(f"{bold(cyan('─' * 50))}")


def check_prerequisites() -> None:
    section("[1/7] Checking Prerequisites")
    errors = []

    # Python version
    if sys.version_info < (3, 10):
        errors.append(f"Python 3.10+ required (got {sys.version})")
    else:
        print(green(f"  ✓ Python {sys.version.split()[0]}"))

    # Node.js
    try:
        result = subprocess.run(["node", "--version"], capture_output=True, text=True)
        if result.returncode == 0:
            print(green(f"  ✓ Node.js {result.stdout.strip()}"))
        else:
            errors.append("Node.js not found (required for MCP servers)")
    except FileNotFoundError:
        errors.append("Node.js not found — install from https://nodejs.org")

    # git
    try:
        result = subprocess.run(["git", "--version"], capture_output=True, text=True)
        if result.returncode == 0:
            print(green(f"  ✓ {result.stdout.strip()}"))
        else:
            errors.append("git not found")
    except FileNotFoundError:
        errors.append("git not found — install from https://git-scm.com")

    # Claude Code CLI
    try:
        result = subprocess.run(["claude", "--version"], capture_output=True, text=True,
                                 shell=(sys.platform == "win32"))
        if result.returncode == 0:
            print(green(f"  ✓ Claude Code CLI found"))
        else:
            print(yellow("  ⚠ Claude Code CLI not found — agents won't run without it"))
    except FileNotFoundError:
        print(yellow("  ⚠ Claude Code CLI not found — install Claude Desktop and enable CLI"))

    if errors:
        print(red("\n  Prerequisites failed:"))
        for e in errors:
            print(red(f"    ✗ {e}"))
        sys.exit(1)


def collect_credentials() -> dict:
    section("[2/7] Credentials")
    print("  Enter your API keys and tokens.\n")

    cfg = {}
    cfg["ANTHROPIC_API_KEY"] = ask("Anthropic API Key (sk-ant-...)", secret=True)
    cfg["SLACK_BOT_TOKEN"] = ask("Slack Bot Token (xoxb-...)", secret=True)
    cfg["SLACK_APP_TOKEN"] = ask("Slack App Token (xapp-1-...)", secret=True)
    cfg["AUTHORIZED_USER_ID"] = ask("Your Slack User ID (UXXXXXXXXX)")
    return cfg


def collect_system_config() -> dict:
    section("[3/7] System Configuration")
    cfg = {}
    cfg["SYSTEM_NAME"] = ask("System name", "J.A.R.B.I.S")
    cfg["BOSS_NAME"] = ask("Your name (how agents refer to you)", "Boss")
    cfg["BOSS_ADDRESS"] = ask("How agents address you", "Boss")
    cfg["GITHUB_USER"] = ask("Your GitHub username (for agent repos)")
    return cfg


def collect_slack_channels() -> dict:
    section("[4/7] Slack Channels")
    print("  Create these channels in Slack first, then enter their IDs.")
    print("  (Channel ID starts with C, e.g. C0XXXXXXXXX)\n")

    cfg = {}
    cfg["SLACK_CONTROL_CHANNEL"] = ask("Control channel ID (#jarvis-control)", "#jarvis-control")
    cfg["SLACK_RESULTS_CHANNEL"] = ask("Results channel ID (#results)", "")
    cfg["SLACK_BRIEFING_CHANNEL"] = ask("Briefing channel ID (#briefing)", "")
    return cfg


def collect_external_services() -> dict:
    section("[5/7] External Services (Optional)")
    cfg = {}

    print("  Notion (for document uploads):")
    cfg["NOTION_API_TOKEN"] = ask("  Notion API Token (ntn_...)", "")
    if cfg["NOTION_API_TOKEN"]:
        cfg["NOTION_HUB_PAGE_ID"] = ask("  Notion Hub Page ID", "")
    else:
        cfg["NOTION_HUB_PAGE_ID"] = ""

    print("\n  Unsplash (for real photos in presentations):")
    cfg["UNSPLASH_ACCESS_KEY"] = ask("  Unsplash Access Key", "")

    print("\n  Google Gemini (for AI image generation):")
    cfg["GEMINI_API_KEY"] = ask("  Gemini API Key (AIzaSy...)", "")

    print("\n  Google Drive (for uploading presentations):")
    default_gdrive = str(Path.home() / ".claude" / "gdrive")
    cfg["GDRIVE_OAUTH_KEYS_PATH"] = ask(
        "  GCP OAuth keys path (gcp-oauth.keys.json)",
        str(Path(default_gdrive) / "gcp-oauth.keys.json")
    )
    cfg["GDRIVE_CREDENTIALS_PATH"] = ask(
        "  GDrive credentials path (.gdrive-server-credentials.json)",
        str(Path(default_gdrive) / ".gdrive-server-credentials.json")
    )
    return cfg


AGENT_DEFAULTS = {
    "friday": {
        "emoji": "💋",
        "name": "Friday",
        "repo": "friday",
        "catchphrase": "Right away, Boss. Already on it.",
        "persona_full": "Female AI assistant modeled after Iron Man's Friday. Professional, efficient, warm but precise. Slight Irish confidence.",
    },
    "hulk": {
        "emoji": "🦖",
        "name": "HULK",
        "repo": "hulk",
        "catchphrase": "HULK SMASH! ...but also, HULK CODE.",
        "persona_full": "Bruce Banner / The Hulk. Educational content powerhouse. Direct, powerful, surprisingly precise.",
    },
    "hawkeye": {
        "emoji": "🦅",
        "name": "Hawkeye",
        "repo": "hawkeye",
        "catchphrase": "I see everything. Nothing gets past Hawkeye.",
        "persona_full": "Clint Barton / Hawkeye. Precision researcher. Sharp, observant, direct.",
    },
    "tonystark": {
        "emoji": "🔴",
        "name": "Tony Stark",
        "repo": "tonystark",
        "catchphrase": "I am Iron Man.",
        "persona_full": "Tony Stark. Genius engineer. Slightly arrogant but always delivers. Casual, peer-to-peer tone.",
    },
    "strange": {
        "emoji": "🔮",
        "name": "Doctor Strange",
        "repo": "strange",
        "catchphrase": "I've calculated all possible outcomes.",
        "persona_full": "Doctor Strange. Cold, precise data analyst. Sees patterns others miss. Formal but personal tone.",
    },
    "pepper": {
        "emoji": "💄",
        "name": "Pepper",
        "repo": "pepper",
        "catchphrase": "I don't think you could tie your shoes without me.",
        "persona_full": "Pepper Potts. Polished, professional designer. Perfect aesthetic sense and execution.",
    },
    "nickfury": {
        "emoji": "🕶️",
        "name": "Nick Fury",
        "repo": "nickfury",
        "catchphrase": "I don't believe in coincidences. Every number tells a story.",
        "persona_full": "Nick Fury. Strategic director. Cold logic, no emotions. Numbers and strategy are his weapons.",
    },
    "rogers": {
        "emoji": "🛡️",
        "name": "Steve Rogers",
        "repo": "rogers",
        "catchphrase": "I can do this all day.",
        "persona_full": "Steve Rogers / Captain America. Principled, structured, team-first. Clear, direct communicator who keeps everyone aligned.",
    },
    "blackwidow": {
        "emoji": "🕸️",
        "name": "Black Widow",
        "repo": "blackwidow",
        "catchphrase": "I've got red in my ledger. Let me clear it.",
        "persona_full": "Natasha Romanoff / Black Widow. Sharp, analytical, direct. Cuts through noise to find what users actually need.",
    },
    "spiderman": {
        "emoji": "🕷️",
        "name": "Spider-Man",
        "repo": "spiderman",
        "catchphrase": "With great power comes great content.",
        "persona_full": "Peter Parker / Spider-Man. High-energy, Gen-Z sensibility, fast execution. Viral instincts and trend awareness.",
    },
    "thor": {
        "emoji": "⚡",
        "name": "Thor",
        "repo": "thor",
        "catchphrase": "Another deal for the worthy!",
        "persona_full": "Thor Odinson. Grand, persuasive, supremely confident. Commands attention and closes deals with sweeping conviction.",
    },
}


def collect_agent_personas() -> dict:
    section("[6/7] Agent Personas")
    print("  Each agent has a default Marvel character persona.")
    print("  You can keep defaults or customize each one.\n")

    personas = {}
    for agent_id, defaults in AGENT_DEFAULTS.items():
        print(f"\n  {defaults['emoji']} {agent_id.upper()} (default: {defaults['name']})")
        if ask_bool(f"    Keep default persona?", default=True):
            personas[agent_id] = defaults.copy()
        else:
            custom = {}
            custom["emoji"] = ask(f"    Emoji", defaults["emoji"])
            custom["name"] = ask(f"    Character name", defaults["name"])
            custom["repo"] = ask(f"    GitHub repo name", defaults["repo"])
            custom["catchphrase"] = ask(f"    Catchphrase", defaults["catchphrase"])
            custom["persona_full"] = ask(f"    Character description (1-2 sentences)", defaults["persona_full"])
            personas[agent_id] = custom

    return personas


def generate_files(creds: dict, sys_cfg: dict, channels: dict, services: dict, personas: dict) -> None:
    section("[7/7] Generating Configuration Files")

    base = str(BASE_DIR)
    github_user = sys_cfg.get("GITHUB_USER", "your-github-user")

    # Build substitution map
    subs = {
        "{{BASE_PATH}}": base,
        "{{GITHUB_USER}}": github_user,
        "{{BOSS_NAME}}": sys_cfg.get("BOSS_NAME", "Boss"),
        "{{BOSS_ADDRESS}}": sys_cfg.get("BOSS_ADDRESS", "Boss"),
        "{{SLACK_CONTROL_CHANNEL}}": channels.get("SLACK_CONTROL_CHANNEL", "#jarvis-control"),
        "{{SLACK_RESULTS_CHANNEL}}": channels.get("SLACK_RESULTS_CHANNEL", ""),
        "{{SLACK_BRIEFING_CHANNEL}}": channels.get("SLACK_BRIEFING_CHANNEL", ""),
        "{{NOTION_API_TOKEN}}": services.get("NOTION_API_TOKEN", ""),
        "{{NOTION_HUB_PAGE_ID}}": services.get("NOTION_HUB_PAGE_ID", ""),
        "{{UNSPLASH_ACCESS_KEY}}": services.get("UNSPLASH_ACCESS_KEY", ""),
        "{{GEMINI_API_KEY}}": services.get("GEMINI_API_KEY", ""),
        "{{GDRIVE_OAUTH_KEYS_PATH}}": services.get("GDRIVE_OAUTH_KEYS_PATH", "").replace("\\", "\\\\"),
        "{{GDRIVE_CREDENTIALS_PATH}}": services.get("GDRIVE_CREDENTIALS_PATH", "").replace("\\", "\\\\"),
    }

    # Add per-agent substitutions
    for agent_id, p in personas.items():
        key = agent_id.upper()
        subs[f"{{{{{key}_EMOJI}}}}"] = p.get("emoji", "🤖")
        subs[f"{{{{{key}_NAME}}}}"] = p.get("name", agent_id)
        subs[f"{{{{{key}_REPO}}}}"] = p.get("repo", agent_id)
        subs[f"{{{{{key}_CATCHPHRASE}}}}"] = p.get("catchphrase", "")
        subs[f"{{{{{key}_PERSONA}}}}"] = f"{p.get('name', agent_id)}. Catchphrase: '{p.get('catchphrase', '')}'"
        subs[f"{{{{{key}_PERSONA_FULL}}}}"] = p.get("persona_full", "")

    def substitute(text: str) -> str:
        for k, v in subs.items():
            text = text.replace(k, v)
        return text

    # .env
    env_content = f"""ANTHROPIC_API_KEY={creds.get('ANTHROPIC_API_KEY', '')}
SLACK_BOT_TOKEN={creds.get('SLACK_BOT_TOKEN', '')}
SLACK_APP_TOKEN={creds.get('SLACK_APP_TOKEN', '')}
AUTHORIZED_USER_ID={creds.get('AUTHORIZED_USER_ID', '')}
"""
    (BASE_DIR / ".env").write_text(env_content, encoding="utf-8")
    print(green("  ✓ .env created"))

    # agents.yaml from template
    template_path = TEMPLATES_DIR / "agents.yaml.template"
    content = template_path.read_text(encoding="utf-8")
    (BASE_DIR / "agents.yaml").write_text(substitute(content), encoding="utf-8")
    print(green("  ✓ agents.yaml created"))

    # mcp_registry.yaml from template
    template_path = TEMPLATES_DIR / "mcp_registry.yaml.template"
    content = template_path.read_text(encoding="utf-8")
    (BASE_DIR / "mcp_registry.yaml").write_text(substitute(content), encoding="utf-8")
    print(green("  ✓ mcp_registry.yaml created"))

    # .mcp.json from template
    template_path = TEMPLATES_DIR / "mcp.json.template"
    content = template_path.read_text(encoding="utf-8")
    mcp_path = BASE_DIR / ".mcp.json"
    mcp_path.write_text(substitute(content), encoding="utf-8")
    print(green("  ✓ .mcp.json created"))

    # Create agent directories and CLAUDE.md files
    agent_dirs = ["friday", "hulk", "hawkeye", "tonystark", "strange", "pepper", "nickfury",
                  "rogers", "blackwidow", "spiderman", "thor"]
    for agent_id in agent_dirs:
        if agent_id == "friday":
            agent_path = BASE_DIR  # Friday lives in root
        else:
            agent_path = BASE_DIR / agent_id
            agent_path.mkdir(exist_ok=True)

        template_file = AGENTS_DIR / f"{agent_id}.md"
        if template_file.exists():
            content = template_file.read_text(encoding="utf-8")
            (agent_path / "CLAUDE.md").write_text(substitute(content), encoding="utf-8")
            print(green(f"  ✓ {agent_id}/CLAUDE.md created"))
        else:
            print(yellow(f"  ⚠ No template for {agent_id}"))

    print(green("\n  All configuration files generated!"))


def install_dependencies() -> None:
    print(f"\n{cyan('Installing Python dependencies...')}")
    result = subprocess.run([sys.executable, "-m", "pip", "install", "-r", "requirements.txt"],
                            capture_output=False)
    if result.returncode == 0:
        print(green("  ✓ Python dependencies installed"))
    else:
        print(yellow("  ⚠ Some dependencies failed — run: pip install -r requirements.txt"))


def print_next_steps(sys_cfg: dict, personas: dict) -> None:
    github_user = sys_cfg.get("GITHUB_USER", "your-github-user")
    friday_repo = personas.get("friday", {}).get("repo", "friday")

    print(f"""
{bold(green('='*50))}
{bold(green('  Setup Complete!'))}
{bold(green('='*50))}

{bold('Next steps:')}

1. {bold('Create GitHub repos for each agent:')}
   gh repo create {github_user}/{friday_repo} --public
   gh repo create {github_user}/{personas.get('hulk', {}).get('repo', 'hulk')} --public
   gh repo create {github_user}/{personas.get('hawkeye', {}).get('repo', 'hawkeye')} --public
   gh repo create {github_user}/{personas.get('tonystark', {}).get('repo', 'tonystark')} --public
   gh repo create {github_user}/{personas.get('strange', {}).get('repo', 'strange')} --public
   gh repo create {github_user}/{personas.get('pepper', {}).get('repo', 'pepper')} --public
   gh repo create {github_user}/{personas.get('nickfury', {}).get('repo', 'nickfury')} --public
   gh repo create {github_user}/{personas.get('rogers', {}).get('repo', 'rogers')} --public
   gh repo create {github_user}/{personas.get('blackwidow', {}).get('repo', 'blackwidow')} --public
   gh repo create {github_user}/{personas.get('spiderman', {}).get('repo', 'spiderman')} --public
   gh repo create {github_user}/{personas.get('thor', {}).get('repo', 'thor')} --public

2. {bold('Initialize git in each agent directory:')}
   cd {BASE_DIR}
   git init && git remote add origin https://github.com/{github_user}/{friday_repo}.git
   git add -A && git commit -m "initial" && git push -u origin main

3. {bold('Create Slack channels and update agents.yaml')} with channel IDs
   (direct_channel for each agent)

4. {bold('Start the system:')}
   python main.py
   # or double-click start.bat

5. {bold('Configure Claude Code:')}
   - Open Claude Desktop
   - Open Claude Code session pointed at {BASE_DIR}
   - The system engineer agent (Friday) will help with further setup

{bold('Slack channels to create:')}
  #jarvis-control  — system alerts and control
  #1-friday        — direct Friday channel
  #2-hulk          — direct Hulk channel
  (etc. for each agent)

{bold('Documentation:')} See AGENT_TEMPLATE.md to add new agents
""")


def main() -> None:
    print(f"""
{bold(cyan('='*50))}
{bold(cyan('   Agent System Setup Wizard'))}
{bold(cyan('='*50))}
Install directory: {BASE_DIR}

This wizard will configure your multi-agent AI system.
Press Ctrl+C at any time to cancel.
""")

    check_prerequisites()
    creds = collect_credentials()
    sys_cfg = collect_system_config()
    channels = collect_slack_channels()
    services = collect_external_services()
    personas = collect_agent_personas()
    generate_files(creds, sys_cfg, channels, services, personas)
    install_dependencies()
    print_next_steps(sys_cfg, personas)


if __name__ == "__main__":
    main()
