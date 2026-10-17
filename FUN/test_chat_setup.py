"""Guided private-chat onboarding tests. No live Telegram credentials/network."""
import asyncio
import json
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock
import pytest
import pytest_asyncio
import mongomock_motor
from cryptography.fernet import Fernet
import config, db, runtime, handlers, chat_setup as chat, userbot_setup as setup
from test_repairs import update, context, session_string


class Client:
    def __init__(self, authorized=True, bot=False):
        self.authorized=authorized; self.bot=bot; self.connected=True; self.disconnects=0
    def is_connected(self): return self.connected
    async def is_user_authorized(self): return self.authorized
    async def get_me(self): return NS(id=777,bot=self.bot)
    async def disconnect(self): self.connected=False;self.disconnects+=1


@pytest_asyncio.fixture(autouse=True)
async def isolated(monkeypatch):
    chat._pending.clear()
    db.use_test_client(mongomock_motor.AsyncMongoMockClient(),"chat_setup")
    monkeypatch.setattr(config,"OWNER_ID",7)
    monkeypatch.setattr(config,"MONGO_URI","mongodb://fake-test-user:fake-test-password@localhost/test")
    monkeypatch.setattr(config,"SESSION_ENCRYPTION_KEY","")
    monkeypatch.delenv("API_ID",raising=False)
    monkeypatch.delenv("API_HASH",raising=False)
    monkeypatch.delenv("SESSION_ENCRYPTION_KEY",raising=False)
    runtime.userbot=None
    yield
    chat._pending.clear()
    await runtime.stop_jobs()


async def say(text,uid=7,chat_type="private"):
    u=update(uid,text);u.message.chat.type=chat_type
    await handlers.on_private_text(u,context())
    return u


async def reach_session():
    await handlers.cmd_setup_userbot(update(),context())
    await say("12345")
    await say("a"*32)


@pytest.mark.asyncio
async def test_exact_three_step_flow_no_extra_env_and_connected_status(monkeypatch):
    client=Client()
    connection=AsyncMock(return_value=client)
    monkeypatch.setattr(setup,"connect_credentials",connection)
    u=update();await handlers.cmd_setup_userbot(u,context())
    assert "Step 1/3" in u.message.replies[-1][0]
    a=await say("12345");assert "Step 2/3" in a.message.replies[-1][0] and a.message.deleted
    b=await say("a"*32);assert "Step 3/3" in b.message.replies[-1][0] and b.message.deleted
    c=await say(session_string())
    assert "Userbot connected" in c.message.replies[-1][0] and c.message.deleted
    connection.assert_awaited_once()
    assert connection.call_args.args[0]=={"api_id":12345,"api_hash":"a"*32,"session":session_string()}
    assert runtime.userbot is client and 7 not in chat._pending
    status=update();await handlers.cmd_userbot_status(status,context())
    assert "Connected: <code>yes</code>" in status.message.replies[-1][0]
    assert "Session stored: <code>yes</code>" in status.message.replies[-1][0]
    saved=await db.get_settings()
    assert session_string() not in json.dumps(saved)
    assert "a"*32 not in json.dumps(saved)
    assert "fake-test-password" not in json.dumps(saved)


@pytest.mark.asyncio
async def test_automatic_storage_reloads_after_restart_without_env(monkeypatch):
    credentials={"api_id":12345,"api_hash":"b"*32,"session":session_string()}
    await chat.save_credentials(credentials)
    chat._pending.clear()
    assert await setup.load_credentials()==credentials
    restored=Client()
    monkeypatch.setattr(setup,"connect_credentials",AsyncMock(return_value=restored))
    assert await setup.build_client() is restored
    assert config.SESSION_ENCRYPTION_KEY==""


@pytest.mark.asyncio
@pytest.mark.parametrize("value",["hello","0","-3","12.5","  ","١٢٣","2147483648"])
async def test_bad_api_id_stays_on_same_step(value):
    await handlers.cmd_setup_userbot(update(),context())
    u=await say(value)
    assert chat._pending[7]["step"]=="api_id"
    assert "positive number" in u.message.replies[-1][0]
    assert u.message.deleted


@pytest.mark.asyncio
@pytest.mark.parametrize("value",["secret","a"*31,"g"*32,"a"*33])
async def test_bad_api_hash_stays_on_same_step(value):
    await handlers.cmd_setup_userbot(update(),context());await say("12345")
    u=await say(value)
    assert chat._pending[7]["step"]=="api_hash"
    assert "doesn't look right" in u.message.replies[-1][0]
    assert u.message.deleted


@pytest.mark.asyncio
async def test_invalid_session_retry_without_reentering_api_credentials(monkeypatch):
    await reach_session()
    u=await say("bad_session")
    assert "Invalid Telethon" in u.message.replies[-1][0]
    assert chat._pending[7]["step"]=="session"
    monkeypatch.setattr(setup,"connect_credentials",AsyncMock(return_value=Client()))
    v=await say(session_string());assert "Userbot connected" in v.message.replies[-1][0]


@pytest.mark.asyncio
async def test_unrelated_input_not_deleted_without_setup():
    u=await say("ordinary message")
    assert not u.message.deleted and not u.message.replies
    assert not await db.get_settings()


@pytest.mark.asyncio
@pytest.mark.parametrize("uid,kind",[(9,"private"),(7,"group"),(7,"supergroup"),(9,"group")])
async def test_other_users_and_group_setup_denied(uid,kind):
    u=update(uid);u.message.chat.type=kind
    await handlers.cmd_setup_userbot(u,context())
    assert not u.message.replies and uid not in chat._pending
    v=await say("a"*32,uid=uid,chat_type=kind)
    assert not v.message.deleted and not v.message.replies


@pytest.mark.asyncio
async def test_different_user_cannot_advance_owner_flow():
    await handlers.cmd_setup_userbot(update(),context())
    await say("12345",uid=9)
    assert chat._pending[7]["step"]=="api_id" and 9 not in chat._pending


@pytest.mark.asyncio
async def test_cancel_discards_partial_input_and_preserves_saved_session():
    creds={"api_id":12345,"api_hash":"c"*32,"session":session_string()}
    await chat.save_credentials(creds)
    await handlers.cmd_setup_userbot(update(),context());await say("54321")
    u=update();await handlers.cmd_cancel(u,context())
    assert "cancelled" in u.message.replies[-1][0]
    assert not chat._pending and await chat.load_credentials()==creds


@pytest.mark.asyncio
async def test_expired_flow_deletes_late_secret_and_requires_restart():
    await reach_session();chat._pending[7]["expires_at"]=0
    u=await say(session_string())
    assert "expired" in u.message.replies[-1][0]
    assert u.message.deleted and not chat._pending
    assert not await db.get_settings()


@pytest.mark.asyncio
async def test_restart_setup_resets_step():
    await reach_session();await handlers.cmd_setup_userbot(update(),context())
    assert chat._pending[7]["step"]=="api_id"
    assert "api_hash" not in chat._pending[7]


@pytest.mark.asyncio
async def test_removal_disconnects_clears_credentials_and_setup(monkeypatch):
    client=Client();runtime.userbot=client
    await chat.save_credentials({"api_id":1,"api_hash":"d"*32,"session":session_string()})
    await reach_session()
    u=update();await handlers.cmd_remove_userbot(u,context())
    assert client.disconnects==1 and runtime.userbot is None and not chat._pending
    assert await setup.load_credentials() is None
    status=update();await handlers.cmd_userbot_status(status,context())
    assert "Connected: <code>no</code>" in status.message.replies[-1][0]
    assert "Session stored: <code>no</code>" in status.message.replies[-1][0]


@pytest.mark.asyncio
async def test_network_failure_does_not_replace_old_credentials_or_leak(monkeypatch):
    creds={"api_id":1,"api_hash":"e"*32,"session":session_string()}
    await chat.save_credentials(creds)
    old=Client();runtime.userbot=old
    await reach_session()
    monkeypatch.setattr(setup,"connect_credentials",AsyncMock(side_effect=RuntimeError("PRIVATE_SECRET_NEVER_SHOW")))
    u=await say(session_string())
    assert "Connection failed" in u.message.replies[-1][0]
    assert "PRIVATE_SECRET_NEVER_SHOW" not in str(u.message.replies)
    assert await chat.load_credentials()==creds and runtime.userbot is old


@pytest.mark.asyncio
async def test_bot_account_session_rejected_and_disconnected(monkeypatch):
    bad=Client(bot=True)
    monkeypatch.setattr(setup,"connect_credentials",AsyncMock(return_value=bad))
    await reach_session();u=await say(session_string())
    assert "not a bot session" in u.message.replies[-1][0]
    assert bad.disconnects==1 and not (await db.get_settings()).get("userbot_chat_credentials")


@pytest.mark.asyncio
async def test_failed_message_deletion_warns_and_continues():
    await handlers.cmd_setup_userbot(update(),context())
    u=update(text="12345");u.message.delete=AsyncMock(side_effect=RuntimeError("denied"))
    await handlers.on_private_text(u,context())
    assert "couldn't delete" in u.message.replies[0][0]
    assert "Step 2/3" in u.message.replies[-1][0]


@pytest.mark.asyncio
async def test_status_is_not_connected_for_revoked_or_disconnected_client():
    runtime.userbot=Client(authorized=False)
    u=update();await handlers.cmd_userbot_status(u,context())
    assert "Connected: <code>no</code>" in u.message.replies[-1][0]
    runtime.userbot.connected=False
    await handlers.cmd_userbot_status(u,context())
    assert "Connected: <code>no</code>" in u.message.replies[-1][0]


@pytest.mark.asyncio
async def test_changing_database_uri_requires_reimport_without_secret_leak(monkeypatch):
    await chat.save_credentials({"api_id":1,"api_hash":"f"*32,"session":session_string()})
    monkeypatch.setattr(config,"MONGO_URI","mongodb://different-secret@localhost/test")
    with pytest.raises(ValueError,match="run /setup_userbot again"):
        await chat.load_credentials()


@pytest.mark.asyncio
async def test_commands_are_not_mistaken_for_api_inputs():
    await handlers.cmd_setup_userbot(update(),context())
    u=await say("/userbot_status")
    assert not u.message.deleted and chat._pending[7]["step"]=="api_id"


@pytest.mark.asyncio
async def test_whitespace_trimmed_and_existing_env_key_ignored(monkeypatch):
    monkeypatch.setattr(config,"SESSION_ENCRYPTION_KEY","not_fernet")
    await handlers.cmd_setup_userbot(update(),context())
    await say(" 12345 ");await say(" "+"a"*32+" ")
    client=Client();monkeypatch.setattr(setup,"connect_credentials",AsyncMock(return_value=client))
    await say(" "+session_string()+" ")
    assert runtime.userbot is client
    assert (await setup.load_credentials())["api_id"]==12345


@pytest.mark.asyncio
async def test_partial_inputs_never_persisted_to_database():
    await reach_session()
    assert not (await db.get_settings()).get("userbot_chat_credentials")
    assert not await db.col("settings").find_one({})


@pytest.mark.asyncio
async def test_restart_load_uses_connected_authorized_client(monkeypatch):
    creds={"api_id":12345,"api_hash":"a"*32,"session":session_string()}
    await chat.save_credentials(creds)
    client=Client()
    call=AsyncMock(return_value=client);monkeypatch.setattr(setup,"connect_credentials",call)
    restored=await setup.build_client()
    await runtime.replace_userbot(restored)
    status=update();await handlers.cmd_userbot_status(status,context())
    assert "Connected: <code>yes</code>" in status.message.replies[-1][0]
    call.assert_awaited_once_with(creds)


@pytest.mark.asyncio
async def test_extremely_long_api_id_rejected_without_crash():
    await handlers.cmd_setup_userbot(update(),context())
    u=await say("9"*5000)
    assert chat._pending[7]["step"]=="api_id"
    assert "positive number" in u.message.replies[-1][0]


@pytest.mark.asyncio
async def test_unknown_value_error_cannot_echo_secrets(monkeypatch):
    await reach_session()
    monkeypatch.setattr(setup,"connect_credentials",AsyncMock(side_effect=ValueError("PRIVATE_SECRET_NEVER_SHOW")))
    u=await say(session_string())
    assert "PRIVATE_SECRET_NEVER_SHOW" not in str(u.message.replies)
    assert "could not be validated" in str(u.message.replies)


@pytest.mark.asyncio
async def test_nonowner_cannot_remove_existing_session():
    creds={"api_id":123,"api_hash":"a"*32,"session":session_string()}
    await chat.save_credentials(creds)
    u=update(9);await handlers.cmd_remove_userbot(u,context())
    assert not u.message.replies and await chat.load_credentials()==creds


@pytest.mark.asyncio
async def test_revoked_session_reports_safe_validation_error(monkeypatch):
    await reach_session()
    monkeypatch.setattr(setup,"connect_credentials",AsyncMock(side_effect=ValueError(
        "This session is not authorized or has been revoked. Create a new StringSession on your trusted computer.")))
    u=await say(session_string())
    assert "revoked" in u.message.replies[-1][0]
    assert not (await db.get_settings()).get("userbot_chat_credentials")
