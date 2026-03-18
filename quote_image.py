"""
名言佳句圖片生成器。
生成 1920x1080 黑底圖，左側頭像（漸層淡出）+ 右側空白區域置中引言文字。
"""
import io

import requests
from PIL import Image, ImageDraw, ImageFont

W, H = 1920, 1080
AVATAR_SIZE = H                  # 1080x1080，撐滿高度
FADE_START_RATIO = 0.45          # 頭像從 45% 處開始淡出

# 文字區域：頭像淡出後的純黑空間（x=1100 ~ x=1890）
TEXT_AREA_X   = 1100
TEXT_AREA_W   = W - TEXT_AREA_X  # 820px
TEXT_PADDING  = 40
TEXT_MAX_W    = TEXT_AREA_W - TEXT_PADDING * 2   # 740px
TEXT_CENTER_X = TEXT_AREA_X + TEXT_AREA_W // 2  # 1510

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


def _text_w(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont) -> int:
    bbox = draw.textbbox((0, 0), text, font=font)
    return bbox[2] - bbox[0]


def _wrap_text(draw: ImageDraw.ImageDraw, text: str,
               font: ImageFont.FreeTypeFont, max_width: int) -> list[str]:
    """換行時優先在空白、標點處斷行；純中文以字元為單位逐字累加，不截斷單字。"""
    lines: list[str] = []
    for paragraph in text.split('\n'):
        paragraph = paragraph.strip()
        if not paragraph:
            lines.append('')
            continue

        words = paragraph.split(' ')
        if len(words) > 1:
            current = ''
            for word in words:
                test = (current + ' ' + word).strip()
                if _text_w(draw, test, font) <= max_width:
                    current = test
                else:
                    if current:
                        lines.append(current)
                    if _text_w(draw, word, font) > max_width:
                        sub = ''
                        for ch in word:
                            if _text_w(draw, sub + ch, font) <= max_width:
                                sub += ch
                            else:
                                lines.append(sub)
                                sub = ch
                        current = sub
                    else:
                        current = word
            if current:
                lines.append(current)
        else:
            current = ''
            for ch in paragraph:
                if _text_w(draw, current + ch, font) <= max_width:
                    current += ch
                else:
                    lines.append(current)
                    current = ch
            if current:
                lines.append(current)

    return lines


def _draw_centered(draw: ImageDraw.ImageDraw, y: int, text: str,
                   font: ImageFont.FreeTypeFont, fill: tuple) -> None:
    w = _text_w(draw, text, font)
    draw.text((TEXT_CENTER_X - w // 2, y), text, font=font, fill=fill)


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
        gray = Image.merge('RGB', (r, g, b)).convert('L').convert('RGB')
        avatar = Image.merge('RGBA', (*gray.split(), a))

    # 水平漸層遮罩：左側不透明 → 右側全透明
    fade_start = int(AVATAR_SIZE * FADE_START_RATIO)
    mask = Image.new('L', (AVATAR_SIZE, AVATAR_SIZE), 255)
    dm = ImageDraw.Draw(mask)
    for x in range(fade_start, AVATAR_SIZE):
        alpha = int(255 * (1 - (x - fade_start) / (AVATAR_SIZE - fade_start)))
        dm.line([(x, 0), (x, AVATAR_SIZE - 1)], fill=alpha)

    avatar.putalpha(mask)
    bg.paste(avatar, (0, 0), avatar)

    # ── Layer 3：文字（右側空白區置中） ──────────────────────────
    draw = ImageDraw.Draw(bg)

    font_main  = _load_font(_FONT_PATHS_BOLD,    52)
    font_attr  = _load_font(_FONT_PATHS_REGULAR, 34)
    font_water = _load_font(_FONT_PATHS_REGULAR, 20)

    # 引言換行
    lines = _wrap_text(draw, f'\u201c{quote}\u201d', font_main, TEXT_MAX_W)
    line_h = draw.textbbox((0, 0), '測', font=font_main)[3] + 10
    total_h = line_h * len(lines) + 20 + 40  # 預留署名高度
    text_y = max(80, (H - total_h) // 2)

    for line in lines:
        _draw_centered(draw, text_y, line, font_main, (255, 255, 255, 255))
        text_y += line_h

    # 署名（只顯示名字，不顯示 ID）
    text_y += 20
    _draw_centered(draw, text_y, f'— {author_name}', font_attr, (190, 190, 190, 255))

    # 浮水印（右下角）
    watermark = f'Made by {bot_name}'
    wb = draw.textbbox((0, 0), watermark, font=font_water)
    draw.text((W - (wb[2] - wb[0]) - 16, H - (wb[3] - wb[1]) - 12),
              watermark, font=font_water, fill=(60, 60, 60, 255))

    out = io.BytesIO()
    bg.convert('RGB').save(out, format='PNG')
    out.seek(0)
    return out.getvalue()
