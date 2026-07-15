import re
import secrets


class ProviderUnavailable(RuntimeError):
    """A distributed compute node could not complete a requested operation."""


def safe_name(user_id: str, display_name: str) -> str:
    """Create a provider-safe, non-guessable VM directory/domain name."""

    clean = (
        re.sub(r"[^a-z0-9-]+", "-", display_name.lower().replace("_", "-")).strip("-")[:28] or "vm"
    )
    return f"localec2-{user_id[:8]}-{clean}-{secrets.token_hex(2)}"
