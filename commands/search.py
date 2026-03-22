"""
以圖搜圖指令：/以圖搜圖
"""
import contextlib
import discord
from discord import app_commands

from reverse_search import reverse_image_search
import state
from gemini_worker import msg_queue, create_chat
from summary import load_summary
from knowledge import build_knowledge_context
from nicknames import build_all_nicknames_summary
from config import MASTER_ID


def _ensure_session(cid: int) -> None:
    """確保頻道有 chat session，若無則以 general 人格初始化。"""
    if cid not in state.chat_sessions or not state.chat_sessions[cid].get('chat_obj'):
        sess = state.chat_sessions.get(cid)
        raw_history = sess.get('raw_history', []) if sess else []
        summary = load_summary(cid)
        state.chat_sessions[cid] = {
            'chat_obj':            create_chat('general', raw_history, summary),
            'personality':         'general',
            'raw_history':         raw_history,
            'current_web_context': None,
        }


def setup(tree: app_commands.CommandTree) -> None:

    @tree.command(name="以圖搜圖", description="用截圖找來源(pixiv/twitter/x/nh)")
    @app_commands.describe(圖片="要搜尋來源的圖片")
    async def slash_reverse_search(interaction: discord.Interaction, 圖片: discord.Attachment):
        mime = (圖片.content_type or '').split(';')[0].strip()
        if not mime.startswith('image/'):
            await interaction.response.send_message('請上傳圖片檔案喵！', ephemeral=True)
            return

        await interaction.response.defer()
        import aiohttp
        async with aiohttp.ClientSession() as session:
            async with session.get(圖片.url) as resp:
                image_data = await resp.read()

        search_results = await reverse_image_search(image_data, mime)

        prompt = (
            f'[以圖搜圖結果]\n{search_results}\n\n'
            f'用戶問題：請幫我找這張圖片的來源\n\n'
            f'[指示]\n'
            f'請根據上方搜尋結果挑選最相關的來源連結並輸出。\n'
            f'優先來源：pixiv、twitter、x.com、nhentai。若這些來源都沒有，再輸出其他最相關連結。\n\n'
            f'輸出格式（嚴格遵守）：\n'
            f'來源名稱(pixiv/X/twitter/nhentai等) | 作品名稱 | 作者\n'
            f'連結：**完整網址**\n\n'
            f'規則：\n'
            f'- 每筆結果佔兩行，第一行是來源名稱|作品|作者，第二行是連結\n'
            f'- 連結必須用 **網址** 加粗包住，禁止使用 [文字](連結) 格式，禁止裸露網址\n'
            f'- 不需特別強調是連篇漫畫或單張插畫\n'
            f'- 不得添加任何額外說明或延伸內容'
        )

        cid  = interaction.channel_id
        user = interaction.user
        _ensure_session(cid)

        # 與 on_message 相同：組 identity_prefix + kb_ctx
        uid_str      = str(user.id)
        nick         = state.nicknames.get(uid_str)
        display_name = user.display_name
        if nick:
            user_ctx = f'[User ID: {user.id}, 暱稱: {nick}]'
        else:
            user_ctx = f'[User ID: {user.id}, 伺服器名稱: {display_name}]'
        if user.id == MASTER_ID:
            identity_prefix = f'{build_all_nicknames_summary(state.nicknames)}\n{user_ctx}\n'
        else:
            identity_prefix = f'{user_ctx}\n'

        kb_ctx      = build_knowledge_context(state.knowledge_entries)
        final_prompt = (kb_ctx + identity_prefix + prompt) if kb_ctx else (identity_prefix + prompt)

        typing_ctx = (
            interaction.channel.typing()
            if interaction.channel else contextlib.nullcontext()
        )

        await msg_queue.put({
            'channel_id': cid,
            'prompt_text': final_prompt,
            'file_parts':  [],
            'reply_fn':    interaction.followup.send,
            'send_fn':     interaction.followup.send,
            'typing_ctx':  typing_ctx,
            'kb_save':     None,
        })
