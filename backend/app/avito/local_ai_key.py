"""Optional macOS local secret store; production stays environment-only."""
import subprocess
import sys
import os
import stat
from pathlib import Path

SERVICE = "com.satorna.avito.local.openai"
ACCOUNT = "avito-chat-size"
LOCAL_KEY_FILE = Path(__file__).resolve().parents[3] / ".local-preview" / ".env.avito-ai"


def _valid_key(value):
    return value.startswith("sk-") and 40 <= len(value) <= 512 and not any(char.isspace() for char in value)


def _local_file_key():
    # Local-only private configuration, never imported or shipped in a bundle.
    try:
        descriptor = os.open(LOCAL_KEY_FILE, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077 or info.st_size > 1024:
                return None
            content = stream.read(1025)
        if len(content) > 1024:
            return None
        lines = content.splitlines()
        values = []
        for line in lines:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            name, separator, value = line.partition("=")
            if separator and name.strip() == "OPENAI_API_KEY":
                values.append(value.strip().strip("\"'"))
            else:
                return None
        return values[0] if len(values) == 1 and _valid_key(values[0]) else None
    except (OSError, UnicodeError):
        return None


def avito_ai_key(settings):
    configured = settings.openai_api_key
    if configured:
        return configured
    if sys.platform != "darwin" or getattr(settings, "environment", "") != "local":
        return None
    file_key = _local_file_key()
    if file_key:
        return file_key
    try:
        result = subprocess.run(
            ["/usr/bin/security", "find-generic-password", "-s", SERVICE, "-a", ACCOUNT, "-w"],
            capture_output=True, text=True, timeout=10, check=False,
        )
        # Never log stdout, stderr or provider credential values.
        key = result.stdout.strip() if result.returncode == 0 else ""
        return key if _valid_key(key) else None
    except (OSError, subprocess.TimeoutExpired):
        return None
