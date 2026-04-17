"""
知識庫指令：/kb 群組（add/remove/list/load）及 !kb 文字指令
"""
import asyncio
import discord
from discord import app_commands

from config import MASTER_ID
from knowledge import (
    add_entry, list_sections, remove_section,
    build_knowledge_context, load_knowledge,
)
from gemini_worker import analyze_for_kb
from history import save_history_async
import state


def setup(tree: app_commands.CommandTree) -> None:
    kb_group = app_commands.Group(name="kb", description="知識庫管理")
    tree.add_command(kb_group)

    @kb_group.command(name="add", description="新增內容到知識庫（文字或上傳檔案，檔案會由 AI 分析統整）")
    @app_commands.describe(文字="要儲存的文字內容", 檔案="要儲存並分析的檔案（.txt/.csv/.json/.sql 等）")
    async def slash_kb_add(interaction: discord.Interaction,
                           文字: str = None,
                           檔案: discord.Attachment = None):
        if not 文字 and not 檔案:
            await interaction.response.send_message('請提供文字內容或上傳檔案喵！', ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        parts = []
        if 文字:
            parts.append(文字.strip())

        if 檔案:
            try:
                raw = await 檔案.read()
                file_text = raw.decode('utf-8', errors='replace')
                await interaction.followup.send(f'正在分析 `{檔案.filename}`，請稍候...', ephemeral=True)
                summary = await analyze_for_kb(f'[{檔案.filename}]\n{file_text}')
                parts.append(f'[{檔案.filename} 分析結果]\n{summary}')
            except Exception as e:
                await interaction.followup.send(f'讀取或分析檔案失敗: {e}', ephemeral=True)
                return

        entry = add_entry(state.knowledge_entries, '\n'.join(parts), interaction.user.id)
        await interaction.followup.send(f'✅ 已分析並儲存至知識庫 `#{entry["id"]}`！', ephemeral=True)

    @kb_group.command(name="remove", description="刪除知識庫中指定節次的資料（主人限定）")
    @app_commands.describe(節次="要刪除的節次編號（先用 /kb list 查看）")
    async def slash_kb_remove(interaction: discord.Interaction, 節次: int):
        if interaction.user.id != MASTER_ID:
            await interaction.response.send_message('此指令限主人使用喵！', ephemeral=True)
            return
        sections = list_sections(state.knowledge_entries)
        if not sections:
            await interaction.response.send_message('知識庫目前是空的喵！', ephemeral=True)
            return
        if remove_section(state.knowledge_entries, 節次):
            remaining = len(list_sections(state.knowledge_entries))
            await interaction.response.send_message(
                f'✅ 已刪除第 `{節次}` 節，剩餘 {remaining} 節。', ephemeral=True)
        else:
            lines = '\n'.join(f'`[{i+1}]` {s[:80]}…' for i, s in enumerate(sections))
            await interaction.response.send_message(
                f'找不到第 `{節次}` 節喵！目前有 {len(sections)} 節：\n{lines}', ephemeral=True)

    @kb_group.command(name="list", description="列出知識庫各節內容（主人限定）")
    async def slash_kb_list(interaction: discord.Interaction):
        if interaction.user.id != MASTER_ID:
            await interaction.response.send_message('此指令限主人使用喵！', ephemeral=True)
            return
        sections = list_sections(state.knowledge_entries)
        if not sections:
            await interaction.response.send_message('知識庫目前是空的喵！', ephemeral=True)
            return
        lines = '\n\n'.join(
            f'**[{i+1}]** {s[:150]}{"…" if len(s) > 150 else ""}' for i, s in enumerate(sections)
        )
        await interaction.response.send_message(
            f'**知識庫各節（共 {len(sections)} 節）：**\n{lines}', ephemeral=True)

    @kb_group.command(name="load", description="從磁碟重新載入知識庫並注入此頻道對話供模型參考")
    async def slash_kb_load(interaction: discord.Interaction):
        state.knowledge_entries[:] = load_knowledge()

        cid = interaction.channel_id
        sess = state.chat_sessions.get(cid)
        if not sess or not sess.get('chat_obj'):
            await interaction.response.send_message('此頻道尚未開始對話喵！請先 @我 說話。', ephemeral=True)
            return
        kb_ctx = build_knowledge_context(state.knowledge_entries)
        if not kb_ctx.strip():
            await interaction.response.send_message('知識庫目前是空的喵！', ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        chat = sess['chat_obj']
        await asyncio.to_thread(chat.send_message, kb_ctx)
        await save_history_async(state.chat_sessions)
        await interaction.followup.send(
            f'✅ 知識庫已重新載入並注入此頻道對話！（共 {len(state.knowledge_entries)} 筆）', ephemeral=True)


async def handle_kb_command(msg: discord.Message, args: str) -> None:
    """!kb 文字指令（主人管理用）。"""
    args = args.strip()
    is_master = (msg.author.id == MASTER_ID)

    if not is_master:
        await msg.reply('知識庫管理指令限主人使用，新增請用 `/kb add` 喵！')
        return

    if args in ('列表', 'list'):
        sections = list_sections(state.knowledge_entries)
        if not sections:
            await msg.reply('知識庫目前是空的。')
        else:
            lines = '\n\n'.join(
                f'**[{i + 1}]** {s[:120]}{"…" if len(s) > 120 else ""}'
                for i, s in enumerate(sections)
            )
            await msg.reply(f'**知識庫各節（共 {len(sections)} 節）：**\n{lines}\n\n用 `!kb 清除 <節次>` 刪除指定節。')
        return

    if args.startswith('清除 ') or args.startswith('清除　'):
        n_str = args.split(None, 1)[1].strip()
        if not n_str.isdigit():
            await msg.reply('請提供有效的節次編號（數字）。')
            return
        if remove_section(state.knowledge_entries, int(n_str)):
            remaining = len(list_sections(state.knowledge_entries))
            await msg.reply(f'已刪除第 `{n_str}` 節，剩餘 {remaining} 節。')
        else:
            await msg.reply(f'找不到第 `{n_str}` 節，請先用 `!kb 列表` 確認節次。')
        return

    await msg.reply('語法：`!kb 列表` / `!kb 清除 <節次>`')
