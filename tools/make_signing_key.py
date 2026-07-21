"""
Generate an Ed25519 signing keypair for licensing.

Run once, then set the printed private key as the LICENSE_SIGNING_KEY
environment variable on your server (Render). Keep it secret — anyone with it
can mint license keys.

Usage:
    python tools/make_signing_key.py
"""

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


def main() -> None:
    private_key = Ed25519PrivateKey.generate()

    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("utf-8")

    public_pem = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode("utf-8")

    print("=" * 70)
    print("ED25519 LICENSE SIGNING KEYPAIR")
    print("=" * 70)
    print("\n--- PUBLIC KEY (for reference; the app derives this itself) ---\n")
    print(public_pem)
    print("--- PRIVATE KEY: set this as LICENSE_SIGNING_KEY (keep secret) ---\n")
    print(private_pem)
    print("=" * 70)
    print("Tip: to store as a single-line env var, replace newlines with \\n,")
    print("or base64-encode the PEM — both formats are accepted by the app.")
    print("=" * 70)


if __name__ == "__main__":
    main()
