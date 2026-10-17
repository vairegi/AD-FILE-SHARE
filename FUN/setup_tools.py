"""Run on YOUR trusted computer, not in a Telegram chat.
python setup_tools.py key     -> generate a new Fernet key
python setup_tools.py session -> log into your own account and print StringSession
Never commit, share, or send the output to support chats. This script saves no files.
"""
import argparse
import asyncio
import getpass
import secrets


def generate_key():
    from cryptography.fernet import Fernet
    return Fernet.generate_key().decode()


async def create_session():
    from telethon import TelegramClient
    from telethon.sessions import StringSession
    api_id = int(input("Your API ID from my.telegram.org: ").strip())
    api_hash = getpass.getpass("Your API hash (hidden): ").strip()
    client = TelegramClient(StringSession(), api_id, api_hash)
    try:
        await client.start(phone=lambda: input("Your account phone number: ").strip(),
                           code_callback=lambda: getpass.getpass("Telegram login code (hidden): "),
                           password=lambda: getpass.getpass("Telegram 2FA password (if enabled): "))
        print("\nYour StringSession (keep secret, import only on your own secure setup page):")
        print(client.session.save())
    finally:
        await client.disconnect()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["key", "session", "webhook-secret"])
    args = parser.parse_args()
    if args.action == "key":
        print(generate_key())
    elif args.action == "webhook-secret":
        print(secrets.token_urlsafe(32))
    else:
        asyncio.run(create_session())


if __name__ == "__main__":
    main()
