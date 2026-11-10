#!/usr/bin/env python3
"""Generate a secure secret key for the gateway."""
import secrets
import sys
import os
from pathlib import Path

def generate_secret_key(length: int = 64) -> str:
    """Generate a cryptographically secure random secret key."""
    return secrets.token_urlsafe(length)

def set_env_value(name: str, value: str, env_file: str = ".env") -> bool:
    """Set ``name=value`` in the .env file, replacing an existing line or adding one."""
    env_path = Path(env_file)
    lines = env_path.read_text().splitlines(keepends=True) if env_path.exists() else []
    for i, line in enumerate(lines):
        if line.startswith(f"{name}=") or line.startswith(f"{name} ="):
            lines[i] = f"{name}={value}\n"
            break
    else:
        lines.append(f"\n{name}={value}\n")
    env_path.write_text("".join(lines))
    return True


def update_env_file(secret_key: str, env_file: str = ".env") -> bool:
    """Update or create .env file with secret key."""
    return set_env_value("SECRET_KEY", secret_key, env_file)


if __name__ == "__main__":
    key_length = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else 64
    
    # --update-env writes SECRET_KEY into .env; add --admin-token to set ADMIN_TOKEN as well.
    if "--update-env" in sys.argv[1:]:
        key = generate_secret_key()
        if update_env_file(key):
            print("✅ Secret key generated and updated in .env file", file=sys.stderr)
            if "--admin-token" in sys.argv[1:] and set_env_value("ADMIN_TOKEN", generate_secret_key()):
                print("✅ Admin token generated and updated in .env file", file=sys.stderr)
        else:
            print("⚠️  Secret key generated but failed to update .env file", file=sys.stderr)
            print(key)
    else:
        key = generate_secret_key(key_length)
        print(key)
        print(f"\nGenerated secret key (length: {len(key)})", file=sys.stderr)
        print("Add this to your .env file as SECRET_KEY", file=sys.stderr)
        print("\nOr run with --update-env to automatically update .env file:", file=sys.stderr)
        print("  python3 scripts/generate_secret_key.py --update-env --admin-token", file=sys.stderr)
