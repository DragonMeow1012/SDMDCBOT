"""
/random-nhentai — 從 nhentai tag 隨機抽一本 gallery。

參數：
  - tag：自行輸入或從 autocomplete 清單挑；空白/底線會轉成連字號。

實作：
  GET /api/v2/search?query=tag:<slug>&page=1
  取 num_pages → 隨機 page → 隨機 index，兩個 API call 解決。

只能在 NSFW 頻道使用（nhentai 全站 18+）。
"""
import random

import aiohttp
import discord
from discord import app_commands

_API_BASE = 'https://nhentai.net/api/v2'
_HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/124.0 Safari/537.36'
    ),
    'Accept': 'application/json',
}
_TIMEOUT = aiohttp.ClientTimeout(total=15)
_COLOR_NH = 0xED2553

_PRESET_TAGS: tuple[str, ...] = (
    'lolicon', 'yuri', 'kemonomimi', 'gender-bender'
)


def _normalize_tag(raw: str) -> str:
    return raw.strip().lower().replace(' ', '-').replace('_', '-')


async def _tag_autocomplete(
    interaction: discord.Interaction, current: str,
) -> list[app_commands.Choice[str]]:
    try:
        cur = current.lower().strip()
        hits = [t for t in _PRESET_TAGS if cur in t] if cur else list(_PRESET_TAGS)
        return [app_commands.Choice(name=t, value=t) for t in hits[:25]]
    except Exception as e:
        print(f'[nhentai] autocomplete error: {type(e).__name__}: {e}')
        return []


async def _fetch_random(
    session: aiohttp.ClientSession, tag: str | None, chinese_only: bool,
) -> dict | None:
    # 沒指定 tag → 用 `*` 從全站抽（nhentai v2 的 wildcard）
    parts = [f'tag:{_normalize_tag(tag)}' if tag else '*']
    if chinese_only:
        parts.append('language:chinese')
    query = ' '.join(parts)

    # 先問 num_pages，再在全範圍隨機挑一頁、該頁隨機挑一本
    params = {'query': query, 'page': 1}
    async with session.get(f'{_API_BASE}/search', params=params, timeout=_TIMEOUT) as r:
        if r.status != 200:
            return None
        meta = await r.json()

    num_pages = meta.get('num_pages') or 0
    if num_pages == 0:
        return None
    if num_pages == 1:
        results = meta.get('result') or []
        return random.choice(results) if results else None

    params['page'] = random.randint(1, num_pages)
    async with session.get(f'{_API_BASE}/search', params=params, timeout=_TIMEOUT) as r:
        if r.status != 200:
            return None
        data = await r.json()
    results = data.get('result') or []
    return random.choice(results) if results else None


def setup(tree: app_commands.CommandTree) -> None:

    @tree.command(
        name='random-nhentai',
        description='從 nhentai tag 隨機抽一本（tag 可不填 = 全站抽）',
    )
    @app_commands.describe(
        tag='tag 名稱（可留空、自己輸入或從清單選）',
        限定中文='True=只抽中文版；預設不限',
    )
    @app_commands.autocomplete(tag=_tag_autocomplete)
    async def _cmd(
        interaction: discord.Interaction,
        tag: str | None = None,
        限定中文: bool = False,
    ):
        channel = interaction.channel
        if not getattr(channel, 'is_nsfw', lambda: False)():
            await interaction.response.send_message(
                '此指令只能在 NSFW 頻道使用喵！', ephemeral=True)
            return

        await interaction.response.defer()

        chinese_only = bool(限定中文)

        try:
            async with aiohttp.ClientSession(headers=_HEADERS) as session:
                gallery = await _fetch_random(session, tag, chinese_only)
        except Exception as e:
            await interaction.followup.send(
                f'nhentai API 失敗了喵... ({type(e).__name__}: {e})', ephemeral=True)
            return

        if gallery is None:
            hint = f'tag `{tag}`' if tag else '全站'
            lang_hint = '（中文）' if chinese_only else ''
            await interaction.followup.send(
                f'{hint}{lang_hint} 找不到喵...', ephemeral=True)
            return

        gid = gallery.get('id')
        url = f'https://nhentai.net/g/{gid}/'
        name = (gallery.get('japanese_title')
                or gallery.get('english_title')
                or gallery.get('title_pretty')
                or '(無標題)')
        embed = discord.Embed(
            title='抽本本',
            description=f'{name}\n{url}',
            color=_COLOR_NH,
        )
        if thumb := gallery.get('thumbnail'):
            if not thumb.startswith('http'):
                thumb = f'https://t.nhentai.net/{thumb.lstrip("/")}'
            embed.set_image(url=thumb)
        await interaction.followup.send(embed=embed)
