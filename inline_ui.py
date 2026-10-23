"""Outgoing private messages always provide inline navigation, including FSM prompts."""
from aiogram.client.session.middlewares.base import BaseRequestMiddleware
from aiogram.methods import SendMessage
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton


class InlineNavigation(BaseRequestMiddleware):
    async def __call__(self, make_request, bot, method):
        if isinstance(method, SendMessage) and isinstance(method.chat_id, int) and method.chat_id > 0:
            if method.reply_markup is None:
                method.reply_markup = InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(text='🏠 Menyu / Bekor qilish', callback_data='home')]])
        return await make_request(bot, method)
