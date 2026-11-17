"""
LICENSE KEY GENERATOR — DEMAND FORECAST PROCESSOR

Issues signed, time-limited license tokens for customers. Replaces the old
unsigned SHA-256 key scheme with real Ed25519-signed tokens matching what
desktop_app.py verifies.

Requires LICENSE_SIGNING_KEY (the PRIVATE key) to be set in the environment —
generate a keypair once with tools/make_signing_key.py, keep the private key
secret, and paste the PUBLIC key into desktop_app.py's PUBLIC_KEY_PEM (or set
it via the LICENSE_PUBLIC_KEY env var for local testing) before shipping.

Usage:
    export LICENSE_SIGNING_KEY="<private PEM>"
    python tools/generate_license.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings
from app.licensing import generate_token


def issue_key(customer_name: str, duration_days: int) -> None:
    token, issued, expires = generate_token(customer_name, duration_days)

    print("\n" + "=" * 70)
    print("LICENSE KEY GENERATED")
    print("=" * 70)
    print(f"\nCustomer Name:  {customer_name}")
    print(f"Issued:         {issued.strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"Expires:        {expires.strftime('%Y-%m-%d %H:%M UTC')}  ({duration_days} day(s))")
    print(f"\nSigned Token:\n{token}\n")

    safe_name = "".join(c if c.isalnum() or c in (" ", "_", "-") else "_" for c in customer_name)
    out_file = Path(f"LICENSE_{safe_name}.txt")
    out_file.write_text(
        "=" * 60 + "\n"
        "DEMAND FORECAST PROCESSOR - LICENSE KEY\n"
        + "=" * 60 + "\n\n"
        f"Customer Name:  {customer_name}\n"
        f"Issued:         {issued.strftime('%Y-%m-%d %H:%M UTC')}\n"
        f"Expires:        {expires.strftime('%Y-%m-%d %H:%M UTC')}  ({duration_days} day(s))\n\n"
        f"Signed Token:\n{token}\n\n"
        + "=" * 60 + "\n"
        "ACTIVATION INSTRUCTIONS\n"
        + "=" * 60 + "\n\n"
        "1. Run the Demand Forecast Processor application.\n"
        "2. When prompted, click 'Activate License' (or 'Renew / Activate License').\n"
        "3. Enter EXACTLY:\n\n"
        f"   Customer Name: {customer_name}\n"
        f"   License Key:   (paste the Signed Token above)\n\n"
        "4. Click 'Activate'.\n\n"
        "NOTE: This key expires automatically on the date above. Request a new\n"
        "key before then to keep working without interruption.\n"
        + "=" * 60 + "\n",
        encoding="utf-8",
    )

    with open("generated_licenses.log", "a", encoding="utf-8") as log:
        log.write(f"{issued.isoformat()} | {customer_name} | expires={expires.isoformat()}\n")

    print(f"Saved to: {out_file}")
    print("Logged in: generated_licenses.log")
    print("=" * 70)


def main() -> None:
    if not settings.LICENSE_SIGNING_KEY.strip():
        print(
            "WARNING: LICENSE_SIGNING_KEY is not set. A throwaway signing key is\n"
            "being used for this process only — the token generated below will NOT\n"
            "verify against the public key baked into any real build of the app.\n"
            "Generate and export a persistent key with tools/make_signing_key.py\n"
            "before issuing real licenses.\n"
        )

    print("=" * 70)
    print("LICENSE KEY GENERATOR - DEMAND FORECAST PROCESSOR")
    print("=" * 70)

    while True:
        print("\n" + "-" * 70)
        print("1. Generate license key")
        print("2. Exit")
        choice = input("\nChoice (1-2): ").strip()

        if choice == "1":
            customer_name = input("Customer name: ").strip()
            if not customer_name:
                print("Error: Customer name cannot be empty.")
                continue

            duration_raw = input(f"Duration in days [{settings.DEFAULT_LICENSE_DAYS}]: ").strip()
            try:
                duration_days = int(duration_raw) if duration_raw else settings.DEFAULT_LICENSE_DAYS
                if duration_days <= 0:
                    raise ValueError
            except ValueError:
                print("Error: Duration must be a positive whole number of days.")
                continue

            issue_key(customer_name, duration_days)

        elif choice == "2":
            print("Goodbye!")
            break
        else:
            print("Invalid choice.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nCancelled.")
