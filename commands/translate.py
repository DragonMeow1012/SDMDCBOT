"""
圖片文字翻譯指令：/translate-img

兩種使用模式：
1. 帶 圖片1..10：直接翻譯，回傳 Discord 圖片附件（最多 10 張）。
2. 帶 壓縮檔（zip/cbz/rar/cbr）：解壓出全部圖片翻譯，回傳翻譯後的壓縮檔，
   突破 Discord 單訊息 10 張附件上限。

RAR 解壓需要系統有 UnRAR.exe（WinRAR 內建或從 rarlab.com 下載）。
找不到 UnRAR.exe 時 RAR 檔會回明確錯誤，zip/cbz 不受影響。

並行上限由 manga_translate 模組裡的 asyncio.Semaphore(MANGA_TRANSLATOR_CONCURRENCY) 控管。
"""
import asyncio
import io
import os
import time
import traceback
import zipfile
from datetime import datetime, timezone

import aiohttp
import discord
from discord import app_commands

from manga_translate import translate_image

# RAR 支援：rarfile 純 Python 套件 + 外部 UnRAR.exe 二進位。
# import 失敗 / 找不到 binary 會降級為「RAR 不支援」，zip 路徑不受影響。
try:
    import rarfile
    # Windows WinRAR 預設安裝路徑；若 user 自行裝在別處可用 env override。
    _UNRAR_CANDIDATES = [
        os.environ.get('UNRAR_TOOL', ''),
        r'C:\Program Files\WinRAR\UnRAR.exe',
        r'C:\Program Files (x86)\WinRAR\UnRAR.exe',
        'unrar',  # PATH 裡的 unrar
        'unrar.exe',
    ]
    for _cand in _UNRAR_CANDIDATES:
        if _cand and (os.path.isfile(_cand) or _cand in ('unrar', 'unrar.exe')):
            rarfile.UNRAR_TOOL = _cand
            break
    _RAR_AVAILABLE = True
except ImportError:
    rarfile = None  # type: ignore
    _RAR_AVAILABLE = False


# Discord interaction token 15 分鐘後失效，notice.edit() 會 401。
# 超過這條線就改用 channel.send 發新訊息，避免長批次（webtoon／冷啟動）翻完丟不回去。
_EDIT_DEADLINE = 720.0

# Discord 預設檔案上限 25MB；伺服器 Boost L2 = 50MB、L3 = 100MB。
# 超過會 413。輸出 zip 超過時要提醒使用者，無法傳上 Discord。
_DISCORD_FILE_LIMIT_MB = 25

_MIME_BY_EXT: dict[str, str] = {
    '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg',
    '.png': 'image/png', '.webp': 'image/webp',
    '.gif': 'image/gif', '.bmp': 'image/bmp',
}


_LANG_CHOICES = [
    app_commands.Choice(name='繁體中文', value='繁體中文'),
    app_commands.Choice(name='簡體中文', value='簡體中文'),
    app_commands.Choice(name='English', value='English'),
    app_commands.Choice(name='日本語', value='日本語'),
    app_commands.Choice(name='한국어', value='한국어'),
]


def _extract_images_from_zip(zip_bytes: bytes) -> list[tuple[str, bytes, str]]:
    """
    解壓 zip/cbz，找出圖片檔。回傳 [(filename, bytes, mime), ...]，依檔名排序。
    BadZipFile 抛 ValueError；個別檔解壓失敗 print 跳過。
    """
    out: list[tuple[str, bytes, str]] = []
    try:
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            # 過濾資料夾項目、排序確保翻譯順序穩定
            names = sorted(n for n in zf.namelist() if not n.endswith('/'))
            for n in names:
                ext = os.path.splitext(n)[1].lower()
                if ext not in _MIME_BY_EXT:
                    continue
                try:
                    data = zf.read(n)
                    if not data:
                        continue
                    out.append((os.path.basename(n), data, _MIME_BY_EXT[ext]))
                except Exception as e:
                    print(f'[TRANSLATE-ZIP] 解 {n} 失敗，跳過: {type(e).__name__}: {e}')
    except zipfile.BadZipFile:
        raise ValueError('壓縮檔格式錯誤或損毀（請確認是 zip/cbz）')
    return out


def _extract_images_from_rar(rar_bytes: bytes) -> list[tuple[str, bytes, str]]:
    """
    解壓 rar/cbr，找出圖片檔。需要 rarfile 套件 + 系統 UnRAR.exe。
    rarfile 拿到 BytesIO 會 spool 到 tempfile（unrar 需要實體檔），自動處理。
    """
    if not _RAR_AVAILABLE:
        raise ValueError('沒裝 rarfile 套件喵：pip install rarfile')
    out: list[tuple[str, bytes, str]] = []
    try:
        with rarfile.RarFile(io.BytesIO(rar_bytes)) as rf:
            names = sorted(n for n in rf.namelist() if not n.endswith('/'))
            for n in names:
                ext = os.path.splitext(n)[1].lower()
                if ext not in _MIME_BY_EXT:
                    continue
                try:
                    data = rf.read(n)
                    if not data:
                        continue
                    out.append((os.path.basename(n), data, _MIME_BY_EXT[ext]))
                except Exception as e:
                    print(f'[TRANSLATE-RAR] 解 {n} 失敗，跳過: {type(e).__name__}: {e}')
    except rarfile.RarCannotExec as e:
        raise ValueError(
            f'找不到 UnRAR.exe 喵：{e}。請裝 WinRAR 或從 rarlab.com 下載 unrar 並放到 PATH，'
            f'或設 env UNRAR_TOOL=完整路徑'
        )
    except rarfile.BadRarFile:
        raise ValueError('RAR 格式錯誤或損毀')
    except rarfile.NeedFirstVolume:
        raise ValueError('RAR 是分卷壓縮（part1.rar 等），請傳完整單檔')
    except rarfile.PasswordRequired:
        raise ValueError('RAR 有密碼保護，無法解開')
    return out


def _extract_images_from_archive(
    archive_bytes: bytes, filename: str,
) -> list[tuple[str, bytes, str]]:
    """依附檔名分派 zip/rar 解壓。未知格式預設當 zip 試（cbz/cbr 也走這條 dispatch）。"""
    ext = os.path.splitext(filename)[1].lower()
    if ext in ('.rar', '.cbr'):
        return _extract_images_from_rar(archive_bytes)
    # 預設 zip（含 .zip / .cbz / 沒附檔名 等）
    return _extract_images_from_zip(archive_bytes)


def _detect_format_ext(image_bytes: bytes) -> str:
    """從 magic header 判副檔名（含點）。對應 server 端 mirror input format 的輸出。"""
    if not image_bytes or len(image_bytes) < 12:
        return '.png'
    if image_bytes[:3] == b'\xff\xd8\xff':
        return '.jpg'
    if image_bytes[:8].startswith(b'\x89PNG'):
        return '.png'
    if image_bytes[:4] == b'RIFF' and image_bytes[8:12] == b'WEBP':
        return '.webp'
    if image_bytes[:6] in (b'GIF87a', b'GIF89a'):
        return '.gif'
    if image_bytes[:2] == b'BM':
        return '.bmp'
    return '.png'


def _build_output_zip(results: list[tuple[int, str, bytes | None, str | None]]) -> bytes:
    """
    把翻譯結果打包成 zip。輸入 list 元素：(idx, original_filename, image_bytes, err_str)。
    err 不是 None 的略過。檔名格式 translated_001.<ext> 用 idx 補零保證解壓後順序，
    副檔名從 image_bytes magic header 偵測（server 端 mirror input format）。
    輸出位元組已壓縮，用 ZIP_STORED 不再壓（速度快、檔案大小幾乎不變）。
    """
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, mode='w', compression=zipfile.ZIP_STORED, allowZip64=True) as zf:
        for idx, _name, out, err in results:
            if err is not None or out is None:
                continue
            ext = _detect_format_ext(out)
            zf.writestr(f'translated_{idx:03d}{ext}', out)
    return buf.getvalue()


def setup(tree: app_commands.CommandTree) -> None:

    @tree.command(name='translate-img', description='翻譯圖片或整本漫畫壓縮檔（zip/cbz/rar/cbr）')
    @app_commands.describe(
        圖片1='(可選) 要翻譯的圖片',
        圖片2='(可選) 第 2 張',
        圖片3='(可選) 第 3 張',
        圖片4='(可選) 第 4 張',
        圖片5='(可選) 第 5 張',
        圖片6='(可選) 第 6 張',
        圖片7='(可選) 第 7 張',
        圖片8='(可選) 第 8 張',
        圖片9='(可選) 第 9 張',
        圖片10='(可選) 第 10 張',
        壓縮檔='(可選) zip/cbz/rar/cbr 壓縮檔（內含多張圖片）；翻譯結果回傳 zip',
        目標語言='翻譯成什麼語言（預設繁體中文）',
    )
    @app_commands.choices(目標語言=_LANG_CHOICES)
    async def slash_translate_img(
        interaction: discord.Interaction,
        圖片1: discord.Attachment | None = None,
        圖片2: discord.Attachment | None = None,
        圖片3: discord.Attachment | None = None,
        圖片4: discord.Attachment | None = None,
        圖片5: discord.Attachment | None = None,
        圖片6: discord.Attachment | None = None,
        圖片7: discord.Attachment | None = None,
        圖片8: discord.Attachment | None = None,
        圖片9: discord.Attachment | None = None,
        圖片10: discord.Attachment | None = None,
        壓縮檔: discord.Attachment | None = None,
        目標語言: app_commands.Choice[str] | None = None,
    ):
        target_lang = 目標語言.value if 目標語言 else '繁體中文'

        # 走壓縮檔路線（突破 10 圖上限）
        if 壓縮檔 is not None:
            await interaction.response.defer()
            await _handle_zip_flow(interaction, 壓縮檔, target_lang)
            return

        # 圖片附件路線（最多 10 張）
        attachments = [
            a for a in (圖片1, 圖片2, 圖片3, 圖片4, 圖片5,
                        圖片6, 圖片7, 圖片8, 圖片9, 圖片10)
            if a is not None
        ]

        if not attachments:
            await interaction.response.send_message(
                '請上傳圖片或壓縮檔喵！（圖片1..10 任選或 壓縮檔 二擇一）',
                ephemeral=True,
            )
            return

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
        # over_deadline 用 interaction.created_at 算（Discord token 15min 過期、按訊息發送瞬間起算）
        interaction_age = (datetime.now(timezone.utc) - interaction.created_at).total_seconds()
        over_deadline = interaction_age > _EDIT_DEADLINE

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


async def _handle_zip_flow(
    interaction: discord.Interaction,
    zip_att: discord.Attachment,
    target_lang: str,
) -> None:
    """
    壓縮檔翻譯流程：下載 → 解壓 → 翻譯 → 打包回傳。
    任何階段失敗都用 notice.edit 回報，不留 user 在「翻譯中...」。
    """
    notice = await interaction.followup.send(
        f'下載壓縮檔 `{zip_att.filename}`（{zip_att.size / 1024 / 1024:.1f}MB）中...',
        wait=True,
    )

    # 下載 zip
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(zip_att.url) as resp:
                zip_bytes = await resp.read()
    except Exception as e:
        print(f'[TRANSLATE-ZIP] 下載失敗: {type(e).__name__}: {e}')
        traceback.print_exc()
        await notice.edit(content=f'下載失敗喵: {type(e).__name__}: {e}')
        return

    # 解壓挑圖（同步邏輯放 thread）；依附檔名分派 zip/rar
    try:
        images = await asyncio.to_thread(
            _extract_images_from_archive, zip_bytes, zip_att.filename,
        )
    except ValueError as e:
        await notice.edit(content=f'{e}')
        return
    except Exception as e:
        print(f'[TRANSLATE-ZIP] 解壓異常: {type(e).__name__}: {e}')
        traceback.print_exc()
        await notice.edit(content=f'解壓失敗喵: {type(e).__name__}: {e}')
        return

    total = len(images)
    if total == 0:
        await notice.edit(content='壓縮檔內沒有任何圖片喵（支援 png/jpg/webp/gif/bmp）')
        return

    await notice.edit(content=f'解壓完成 {total} 張，開始翻譯（一張約20秒）喵...')
    started = time.monotonic()
    done = 0
    fail = 0
    progress_lock = asyncio.Lock()

    async def _one(idx: int, name: str, data: bytes, mime: str):
        nonlocal done, fail
        try:
            out = await translate_image(data, mime, target_lang)
            result = (idx, name, out, None)
        except Exception as e:
            print(f'[TRANSLATE-ZIP] {name} 失敗: {type(e).__name__}: {e}')
            traceback.print_exc()
            result = (idx, name, None, f'{type(e).__name__}: {e}')
        async with progress_lock:
            if result[2] is not None:
                done += 1
            else:
                fail += 1
            finished = done + fail
            elapsed = time.monotonic() - started
            eta_str = ''
            if 0 < finished < total:
                avg = elapsed / finished
                eta = avg * (total - finished)
                eta_str = f'、預估還剩 {eta:.0f}s'
            fail_str = f'（失敗 {fail}）' if fail else ''
            print(f'[TRANSLATE-ZIP] 進度 {finished}/{total}{fail_str}'
                  f' 已耗時 {elapsed:.0f}s{eta_str} ← {name}')
        return result

    results = await asyncio.gather(
        *(_one(i, n, d, m) for i, (n, d, m) in enumerate(images, 1))
    )

    # 打包輸出 zip（同步壓縮放 thread）
    out_zip_bytes = await asyncio.to_thread(_build_output_zip, results)
    out_size_mb = len(out_zip_bytes) / 1024 / 1024

    success = sum(1 for _, _, out, _ in results if out is not None)
    errors = [(name, err) for _, name, _, err in results if err is not None]
    elapsed = time.monotonic() - started

    msg_lines = [f'翻譯完成 {success}/{total} 張，耗時 {elapsed:.0f}s（輸出 zip {out_size_mb:.1f}MB）']
    if errors:
        head_n = min(3, len(errors))
        err_lines = '\n'.join(f'  • {n}: {e[:80]}' for n, e in errors[:head_n])
        msg_lines.append(f'失敗 {len(errors)}：\n{err_lines}')
        if len(errors) > head_n:
            msg_lines.append(f'  ...另 {len(errors) - head_n} 個失敗未列出')
    content = '\n'.join(msg_lines)

    # 超過 25MB 連 Discord 都傳不上去
    if out_size_mb > _DISCORD_FILE_LIMIT_MB:
        await notice.edit(content=(
            f'{content}\n'
            f'⚠️ 輸出 zip {out_size_mb:.1f}MB 超過 Discord 預設上限 {_DISCORD_FILE_LIMIT_MB}MB，'
            f'伺服器升 Boost L2/L3 才能傳，或減少張數重試喵。'
        ))
        return

    if success == 0:
        # 沒成功的就不傳 zip 了，傳了也是空檔
        await notice.edit(content=content)
        return

    out_filename = (os.path.splitext(zip_att.filename)[0] or 'manga') + '_translated.zip'
    file = discord.File(io.BytesIO(out_zip_bytes), filename=out_filename)
    # over_deadline 用 interaction.created_at 算（Discord token 15min 過期、按訊息發送瞬間起算）
    interaction_age = (datetime.now(timezone.utc) - interaction.created_at).total_seconds()
    over_deadline = interaction_age > _EDIT_DEADLINE

    if over_deadline:
        print(f'[TRANSLATE-ZIP] interaction 已 {interaction_age:.0f}s（>{_EDIT_DEADLINE:.0f}s），改發新訊息')
        await interaction.channel.send(content='翻譯完成了喵!', files=[file])
        return

    try:
        await notice.edit(content=content, attachments=[file])
    except Exception as e:
        print(f'[TRANSLATE-ZIP] 編輯失敗、改用 channel.send: {type(e).__name__}: {e}')
        traceback.print_exc()
        # interaction 過期才走到這 → 用簡短新訊息，stats 已在 log
        await interaction.channel.send(content='翻譯完成了喵!', files=[file])
