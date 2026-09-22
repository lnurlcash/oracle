"""Generates a fresh oracle keypair for local setup - `uv run python -m
app.gen_keypair`. Dev-grade only: prints the secret key to stdout. A real
deployment's long-term key should never pass through a step like this -
see DESIGN.md's own "Key management" section."""

from app.crypto.oracle import generate_keypair

if __name__ == "__main__":
    secret_hex, pubkey_hex = generate_keypair()
    print(f"ORACLE_SECRET_KEY_HEX={secret_hex}")
    print(f"# oracle pubkey: {pubkey_hex}")
