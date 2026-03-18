"""
名言佳句圖片生成器。
生成 1024x512 黑底圖，左側頭像（漸層淡出）+ 右側白色引言文字。
"""
import io
import textwrap

import requests
from PIL import Image, ImageDraw, ImageFont

W, H = 1024, 512
AVATAR_SIZE = H  # 512x512，撐滿高度
TEXT_X = 430     # 文字區域起始 X
TEXT_MAX_W = W - TEXT_X - 40
FADE_START_RATIO = 0.45  # 頭像從 45% 處開始淡出


_FONT_PATHS_BOLD = [
    'C:/Windows/Fonts/NotoSansTC-VF.ttf',
    'C:/Windows/Fonts/msjhbd.ttc',
    'C:/Windows/Fonts/msyhbd.ttc',
]
_FONT_PATHS_REGULAR = [
    'C:/Windows/Fonts/NotoSansTC-VF.ttf',
    'C:/Windows/Fonts/msjh.ttc',
    'C:/Windows/Fonts/msyh.ttc',
]


def _load_font(paths: list[str], size: int) -> ImageFont.FreeTypeFont:
    for p in paths:
        try:
            return ImageFont.truetype(p, size)
        except Exception:
            pass
    return ImageFont.load_default()


def _wrap_text(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, max_width: int) -> list[str]:
    """按像素寬度自動換行，回傳行列表。"""
    words = text.replace('\n', ' \n ').split(' ')
    lines: list[str] = []
    current = ''
    for word in words:
        if word == '\n':
            lines.append(current.strip())
            current = ''
            continue
        test = (current + ' ' + word).strip()
        bbox = draw.textbbox((0, 0), test, font=font)
        if bbox[2] - bbox[0] <= max_width:
            current = test
        else:
            if current:
                lines.append(current.strip())
            current = word
    if current:
        lines.append(current.strip())
    return lines


def make_quote_image(
    avatar_url: str,
    quote: str,
    author_name: str,
    author_id: int,
    bot_name: str = '小龍喵',
    grayscale: bool = True,
) -> bytes:
    # ── Layer 1：黑底 ──────────────────────────────────────────────
    bg = Image.new('RGBA', (W, H), (0, 0, 0, 255))

    # ── Layer 2：頭像 ──────────────────────────────────────────────
    try:
        resp = requests.get(avatar_url, timeout=10)
        avatar = Image.open(io.BytesIO(resp.content)).convert('RGBA')
    except Exception:
        avatar = Image.new('RGBA', (AVATAR_SIZE, AVATAR_SIZE), (40, 40, 40, 255))

    avatar = avatar.resize((AVATAR_SIZE, AVATAR_SIZE), Image.LANCZOS)

    if grayscale:
        r, g, b, a = avatar.split()
        gray_rgb = Image.merge('RGB', (r, g, b)).convert('L')
        gray_rgb = gray_rgb.convert('RGB')
        avatar = Image.merge('RGBA', (*gray_rgb.split(), a))

    # 水平漸層遮罩：左側不透明 → 右側全透明
    fade_start = int(AVATAR_SIZE * FADE_START_RATIO)
    mask = Image.new('L', (AVATAR_SIZE, AVATAR_SIZE), 255)
    draw_mask = ImageDraw.Draw(mask)
    for x in range(fade_start, AVATAR_SIZE):
        alpha = int(255 * (1 - (x - fade_start) / (AVATAR_SIZE - fade_start)))
        draw_mask.line([(x, 0), (x, AVATAR_SIZE - 1)], fill=alpha)

    avatar.putalpha(mask)
    bg.paste(avatar, (0, 0), avatar)

    # ── Layer 3：文字 ──────────────────────────────────────────────
    draw = ImageDraw.Draw(bg)

    font_main  = _load_font(_FONT_PATHS_BOLD,    36)
    font_attr  = _load_font(_FONT_PATHS_REGULAR, 22)
    font_id    = _load_font(_FONT_PATHS_REGULAR, 18)
    font_water = _load_font(_FONT_PATHS_REGULAR, 13)

    # 引言文字（自動換行）
    lines = _wrap_text(draw, f'"{quote}"', font_main, TEXT_MAX_W)
    line_h = draw.textbbox((0, 0), 'A', font=font_main)[3] + 8
    total_text_h = line_h * len(lines)
    text_y = max(60, (H - total_text_h) // 2 - 30)

    for line in lines:
        draw.text((TEXT_X, text_y), line, font=font_main, fill=(255, 255, 255, 255))
        text_y += line_h

    # 署名
    text_y += 16
    draw.text((TEXT_X + 10, text_y), f'— {author_name}', font=font_attr, fill=(200, 200, 200, 255))
    text_y += 28
    draw.text((TEXT_X + 10, text_y), f'@{author_id}', font=font_id, fill=(130, 130, 130, 255))

    # 浮水印
    watermark = f'Made by {bot_name}'
    wb = draw.textbbox((0, 0), watermark, font=font_water)
    draw.text((W - (wb[2] - wb[0]) - 12, H - (wb[3] - wb[1]) - 10),
              watermark, font=font_water, fill=(70, 70, 70, 255))

    # 輸出 PNG bytes
    out = io.BytesIO()
    bg.convert('RGB').save(out, format='PNG')
    out.seek(0)
    return out.getvalue()
