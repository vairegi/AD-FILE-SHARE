"""Real Bot API rich blocks via the pinned PTB transport.

PTB 22.8 does not expose sendRichMessage/InputRichBlockTable yet. Use its
existing request/auth/error handling with documented JSON fields instead of
upgrading every delivery dependency or silently showing a monospace fake.
Schema: https://core.telegram.org/bots/api#inputrichblocktable
"""
from telegram.error import BadRequest


def cell(text, *, header=False, align='left'):
    value = {'text':text, 'align':align, 'valign':'middle'}
    if header:
        value['is_header'] = True
    return value


def paragraph(text):
    return {'type':'paragraph', 'text':text}


async def send(bot, chat_id, rich, keyboard):
    return await bot._post('sendRichMessage', {
        'chat_id':chat_id, 'rich_message':rich, 'reply_markup':keyboard.to_dict()})


async def edit(bot, chat_id, message_id, rich, keyboard):
    try:
        return await bot._post('editMessageText', {
            'chat_id':chat_id, 'message_id':message_id,
            'rich_message':rich, 'reply_markup':keyboard.to_dict()})
    except BadRequest as exc:
        text = str(exc).lower()
        if 'message is not modified' in text:
            return None
        if 'message to edit not found' in text or "message can't be edited" in text:
            return await send(bot, chat_id, rich, keyboard)
        raise  # Never replace a native table with a fake text table.
