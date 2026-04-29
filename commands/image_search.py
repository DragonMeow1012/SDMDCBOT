"""
以圖搜圖指令：/以圖搜圖
"""
import aiohttp
import discord
from discord import app_commands

from reverse_search import reverse_image_search


def setup(tree: app_commands.CommandTree) -> None:

    @tree.command(name='以圖搜圖', description='用截圖找來源(pixiv/twitter/x/nh)')
    @app_commands.describe(圖片='要搜尋來源的圖片')
    async def slash_reverse_search(interaction: discord.Interaction, 圖片: discord.Attachment):
        mime = (圖片.content_type or '').split(';')[0].strip()
        if not mime.startswith('image/'):
            await interaction.response.send_message('請上傳圖片檔案', ephemeral=True)
            return

        await interaction.response.defer()
        async with aiohttp.ClientSession() as session:
            async with session.get(圖片.url) as resp:
                image_data = await resp.read()

        result = await reverse_image_search(image_data, mime)
        await interaction.followup.send(result)
