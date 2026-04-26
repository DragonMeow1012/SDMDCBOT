"""
圖片文字翻譯指令：/translate-img
把上傳的圖片丟到 manga-image-translator 翻譯後回傳。

slash command 不支援單一參數吃多個 attachment，這裡開 10 個 Optional
Attachment 欄位讓使用者一次可以傳多張。並行上限由 manga_translate 模組
裡的 asyncio.Semaphore(MANGA_TRANSLATOR_CONCURRENCY) 控管。
"""
import asyncio
import io
import time
import traceback

import aiohttp
import discord
from discord import app_commands

from manga_translate import translate_image


# Discord interaction token 15 分鐘後失效，notice.edit() 會 401。
# 超過這條線就改用 channel.send 發新訊息，避免長批次（webtoon／冷啟動）翻完丟不回去。
_EDIT_DEADLINE = 720.0


_LANG_CHOICES = [
    app_commands.Choice(name='繁體中文', value='繁體中文'),
    app_commands.Choice(name='簡體中文', value='簡體中文'),
    app_commands.Choice(name='English', value='English'),
    app_commands.Choice(name='日本語', value='日本語'),
    app_commands.Choice(name='한국어', value='한국어'),
]


def setup(tree: app_commands.CommandTree) -> None:

    @tree.command(name='translate-img', description='翻譯圖片文字，大部分情況都通用，一張圖大約30秒')
    @app_commands.describe(
        圖片1='要翻譯的圖片',
        圖片2='(可選) 第 2 張',
        圖片3='(可選) 第 3 張',
        圖片4='(可選) 第 4 張',
        圖片5='(可選) 第 5 張',
        圖片6='(可選) 第 6 張',
        圖片7='(可選) 第 7 張',
        圖片8='(可選) 第 8 張',
        圖片9='(可選) 第 9 張',
        圖片10='(可選) 第 10 張',
        目標語言='翻譯成什麼語言（預設繁體中文）',
    )
    @app_commands.choices(目標語言=_LANG_CHOICES)
    async def slash_translate_img(
        interaction: discord.Interaction,
        圖片1: discord.Attachment,
        圖片2: discord.Attachment | None = None,
        圖片3: discord.Attachment | None = None,
        圖片4: discord.Attachment | None = None,
        圖片5: discord.Attachment | None = None,
        圖片6: discord.Attachment | None = None,
        圖片7: discord.Attachment | None = None,
        圖片8: discord.Attachment | None = None,
        圖片9: discord.Attachment | None = None,
        圖片10: discord.Attachment | None = None,
        目標語言: app_commands.Choice[str] | None = None,
    ):
        attachments = [
            a for a in (圖片1, 圖片2, 圖片3, 圖片4, 圖片5,
                        圖片6, 圖片7, 圖片8, 圖片9, 圖片10)
            if a is not None
        ]

        # 過濾非圖片
        valid: list[tuple[discord.Attachment, str]] = []
        skipped: list[str] = []
        for a in attachments:
            mime = (a.content_type or '').split(';')[0].strip()
            if mime.startswith('image/'):
                valid.append((a, mime))
            else:
                skipped.append(a.filename)

        if not valid:
            await interaction.response.send_message('請上傳圖片檔案喵！', ephemeral=True)
            return

        await interaction.response.defer()

        target_lang = 目標語言.value if 目標語言 else '繁體中文'
        total = len(valid)
        notice = await interaction.followup.send(
            f'小龍喵正在翻譯圖片喵...',
            wait=True,
        )
        started = time.monotonic()

        async with aiohttp.ClientSession() as session:
            async def _one(idx: int, att: discord.Attachment, mime: str):
                try:
                    async with session.get(att.url) as resp:
                        image_data = await resp.read()
                    out = await translate_image(image_data, mime, target_lang)
                except Exception as e:
                    print(f'[TRANSLATE] 第 {idx} 張失敗: {type(e).__name__}: {e}')
                    traceback.print_exc()
                    return idx, att.filename, None, f'{type(e).__name__}: {e}'
                return idx, att.filename, out, None

            results = await asyncio.gather(
                *(_one(i, a, m) for i, (a, m) in enumerate(valid, 1))
            )

        files: list[discord.File] = []
        errors: list[str] = []
        for idx, name, out, err in results:
            if err is not None:
                errors.append(f'第 {idx} 張（{name}）：{err}')
                continue
            files.append(discord.File(io.BytesIO(out), filename=f'translated_{idx}.png'))

        elapsed = time.monotonic() - started
        over_deadline = elapsed > _EDIT_DEADLINE

        lines: list[str] = []
        head = '小龍喵幫你翻譯好了喵！' if files else '翻譯全部失敗喵...'
        if over_deadline:
            head += ' (本次翻譯時長超過12分鐘，避免舊訊息無法編輯已使用新messge)'
        lines.append(head)
        if skipped:
            lines.append(f'跳過非圖片檔：{", ".join(skipped)}')
        if errors:
            lines.append('失敗：\n' + '\n'.join(errors))
        content = '\n'.join(lines)

        # 超過 12 分鐘就直接發新訊息——followup token 已經接近／超過 15 分鐘上限，
        # edit 多半會 401。channel.send 不依賴 interaction token，可以一直發。
        if over_deadline:
            print(f'[TRANSLATE] 翻譯耗時 {elapsed:.0f}s 超過 {_EDIT_DEADLINE:.0f}s，改發新訊息')
            await interaction.channel.send(content=content, files=files)
            return

        try:
            await notice.edit(content=content, attachments=files)
        except Exception as e:
            print(f'[TRANSLATE] 編輯通知失敗、改用 channel.send: {type(e).__name__}: {e}')
            traceback.print_exc()
            await interaction.channel.send(content=content, files=files)
