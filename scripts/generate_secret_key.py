#!/usr/bin/env python3
"""Generate a secure secret key for the gateway."""
import secrets
import sys
import os
from pathlib import Path

def generate_secret_key(length: int = 64) -> str:
    """Generate a cryptographically secure random secret key."""
    return secrets.token_urlsafe(length)

def update_env_file(secret_key: str, env_file: str = ".env") -> bool:
    """Update or create .env file with secret key."""
    env_path = Path(env_file)
    
    # Read existing .env if it exists
    lines = []
    key_updated = False
    
    if env_path.exists():
        with open(env_path, 'r') as f:
            lines = f.readlines()
        
        # Update existing SECRET_KEY
        for i, line in enumerate(lines):
            if line.startswith('SECRET_KEY=') or line.startswith('SECRET_KEY ='):
                lines[i] = f'SECRET_KEY={secret_key}\n'
                key_updated = True
                break
    
    # Add SECRET_KEY if not found
    if not key_updated:
        lines.append(f'\nSECRET_KEY={secret_key}\n')
    
    # Write back to file
    with open(env_path, 'w') as f:
        f.writelines(lines)
    
    return True

if __name__ == "__main__":
    key_length = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else 64
    
    # Check if we should update .env file
    if len(sys.argv) > 1 and sys.argv[1] == '--update-env':
        key = generate_secret_key()
        if update_env_file(key):
            print(f"✅ Secret key generated and updated in .env file", file=sys.stderr)
        else:
            print(f"⚠️  Secret key generated but failed to update .env file", file=sys.stderr)
            print(key)
    else:
        key = generate_secret_key(key_length)
        print(key)
        print(f"\nGenerated secret key (length: {len(key)})", file=sys.stderr)
        print("Add this to your .env file as SECRET_KEY", file=sys.stderr)
        print("\nOr run with --update-env to automatically update .env file:", file=sys.stderr)
        print("  python3 scripts/generate_secret_key.py --update-env", file=sys.stderr)
