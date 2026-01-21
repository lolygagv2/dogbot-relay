#!/usr/bin/env python3
"""
CLI script to register a device in the WIM-Z Relay Server.

Usage:
    python register_device.py <device_id> <device_secret>

Example:
    python register_device.py wimz_robot_01 FO3cc1LZj2I6sfLBy41NhlfyGD_Rd-ttDzBAOjZ_FNo
"""
import hashlib
import hmac
import sys


def generate_signature(device_id: str, device_secret: str) -> str:
    """Generate HMAC-SHA256 signature for device authentication."""
    message = device_id.encode()
    signature = hmac.new(
        device_secret.encode(),
        message,
        hashlib.sha256
    ).hexdigest()
    return signature


def main():
    if len(sys.argv) < 3:
        print("Usage: python register_device.py <device_id> <device_secret>")
        print("\nExample:")
        print("  python register_device.py wimz_robot_01 FO3cc1LZj2I6sfLBy41NhlfyGD_Rd-ttDzBAOjZ_FNo")
        sys.exit(1)

    device_id = sys.argv[1]
    device_secret = sys.argv[2]

    # Generate the expected signature
    signature = generate_signature(device_id, device_secret)

    print("\n" + "=" * 60)
    print("WIM-Z Device Registration Info")
    print("=" * 60)
    print(f"\nDevice ID: {device_id}")
    print(f"Device Secret: {device_secret}")
    print(f"\nExpected Signature (HMAC-SHA256):")
    print(f"  {signature}")

    print("\n" + "-" * 60)
    print("WebSocket Connection URL:")
    print("-" * 60)
    print(f"\nws://localhost:8000/ws/device?device_id={device_id}&sig={signature}")
    print(f"\nwss://api.wimz.io/ws/device?device_id={device_id}&sig={signature}")

    print("\n" + "-" * 60)
    print("IMPORTANT: Signature Format")
    print("-" * 60)
    print("""
The relay server computes signatures as:
    HMAC-SHA256(device_id, device_secret)

The message is ONLY the device_id - no timestamp included.
The signature should be lowercase hex (64 characters).

If your robot is including a timestamp in the HMAC message,
the signatures won't match.
""")

    print("-" * 60)
    print("To Pre-Register Device (run server first):")
    print("-" * 60)
    print(f"""
curl -X POST http://localhost:8000/api/device/register \\
  -H "Content-Type: application/json" \\
  -H "Authorization: HMAC-SHA256 {signature}" \\
  -d '{{"device_id": "{device_id}", "firmware_version": "1.0.0"}}'
""")


if __name__ == "__main__":
    main()
