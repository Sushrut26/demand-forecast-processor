"""
Generate an Ed25519 signing keypair for licensing.

Run this ONCE per product. Afterwards:
  - Set the PRIVATE key as the LICENSE_SIGNING_KEY environment variable
    wherever tools/generate_license.py runs (your machine, keep it secret —
    anyone with it can mint valid license keys for this product).
  - Paste the PUBLIC key into app/license_manager.py's LicenseManager.
    PUBLIC_KEY_PEM (or set LICENSE_PUBLIC_KEY when testing) BEFORE building or
    distributing the desktop app. The public key is safe to embed in the
    shipped app/exe — it can only verify tokens, not create them.

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
    print("\n--- PUBLIC KEY: paste into app/license_manager.py PUBLIC_KEY_PEM ---\n")
    print(public_pem)
    print("--- PRIVATE KEY: set as LICENSE_SIGNING_KEY, keep secret, never ship ---\n")
    print(private_pem)
    print("=" * 70)
    print("Tip: to store as a single-line env var, replace newlines with \\n,")
    print("or base64-encode the PEM — both formats are accepted by the app.")
    print("=" * 70)


if __name__ == "__main__":
    main()
