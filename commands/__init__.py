"""
Commands 套件入口。
呼叫 setup_all(tree) 將所有指令群組與指令注冊到 CommandTree。
"""
from discord import app_commands

from commands import admin, nick, gag, fun, social, artillery, quote, search, kb


def setup_all(tree: app_commands.CommandTree) -> None:
    admin.setup(tree)
    nick.setup(tree)
    gag.setup(tree)
    fun.setup(tree)
    social.setup(tree)
    artillery.setup(tree)
    quote.setup(tree)
    search.setup(tree)
    kb.setup(tree)
