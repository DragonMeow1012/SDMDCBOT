"""
圖形渲染模組：為本群關係圖生成視覺化網絡圖。
依賴：matplotlib、networkx、Pillow、requests
"""
import io
import math
import asyncio
import datetime

import requests as _requests
import numpy as np
import networkx as nx
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.font_manager as fm
import matplotlib.patheffects as pe
from matplotlib.offsetbox import OffsetImage, AnnotationBbox
from PIL import Image, ImageDraw, ImageFilter
import discord


# ── 字型 ──────────────────────────────────────────────────────────────────────

def _find_cjk_font() -> str:
    candidates = [
        'Microsoft YaHei', 'Microsoft JhengHei',
        'SimHei', 'PingFang SC', 'Noto Sans CJK SC',
        'Arial Unicode MS',
    ]
    available = {f.name for f in fm.fontManager.ttflist}
    for c in candidates:
        if c in available:
            return c
    return 'sans-serif'

_FONT = _find_cjk_font()


# ── 頭像處理 ──────────────────────────────────────────────────────────────────

def _fetch_avatar(url: str, size: int = 128) -> np.ndarray | None:
    """同步下載並裁切為圓形頭像，回傳 RGBA numpy array。"""
    try:
        resp = _requests.get(url, timeout=8)
        resp.raise_for_status()
        img = Image.open(io.BytesIO(resp.content)).convert('RGBA').resize((size, size))
        mask = Image.new('L', (size, size), 0)
        ImageDraw.Draw(mask).ellipse((0, 0, size, size), fill=255)
        img.putalpha(mask)
        return np.array(img)
    except Exception as e:
        print(f'[GRAPH] 頭像下載失敗: {e}')
        return None


async def _collect_members(
    guild: discord.Guild,
    uids: set[str],
) -> tuple[dict[str, str], dict[str, np.ndarray]]:
    """非同步收集成員顯示名稱與頭像。"""
    name_map:   dict[str, str]        = {}
    avatar_map: dict[str, np.ndarray] = {}

    async def _fetch_one(uid: str):
        member = guild.get_member(int(uid))
        if not member:
            try:
                member = await guild.fetch_member(int(uid))
            except discord.NotFound:
                pass
        if not member:
            return
        name_map[uid] = member.display_name
        url = str(member.display_avatar.replace(size=128).url)
        arr = await asyncio.to_thread(_fetch_avatar, url)
        if arr is not None:
            avatar_map[uid] = arr

    await asyncio.gather(*[_fetch_one(uid) for uid in uids])
    return name_map, avatar_map


# ── 漸層背景 ──────────────────────────────────────────────────────────────────

def _draw_gradient_bg(fig: plt.Figure) -> None:
    """在 figure 最底層畫由上（粉紅）到下（白）的漸層。"""
    ax_bg = fig.add_axes([0, 0, 1, 1], zorder=0)
    H, W  = 400, 600
    grad  = np.linspace(0, 1, H).reshape(-1, 1) * np.ones((1, W))
    # top: #F2B8C0  bottom: #FFFFFF
    r = 0.949 - (0.949 - 1.0) * grad   # 0.949 → 1.0
    g = 0.722 - (0.722 - 1.0) * grad
    b = 0.752 - (0.752 - 1.0) * grad
    ax_bg.imshow(np.dstack([r, g, b]), aspect='auto',
                 extent=[0, 1, 0, 1], origin='upper')
    ax_bg.axis('off')


# ── 主渲染 ────────────────────────────────────────────────────────────────────

async def render_relation_graph(
    guild: discord.Guild,
    rels:      dict[str, str],   # {pet_id: master_id}
    wife_rels: dict[str, str] | None = None,  # {husband_id: wife_id}
) -> io.BytesIO:
    """
    生成主寵 + 媽媽關係視覺圖並回傳 PNG BytesIO。
    rels 結構：{pet_id: master_id}，箭頭方向 master → pet。
    wife_rels 結構：{husband_id: wife_id}，箭頭方向 husband → wife。
    """
    wife_rels = wife_rels or {}

    # ── 建立有向圖 ──────────────────────────────────────
    G = nx.DiGraph()
    for pet_id, master_id in rels.items():
        G.add_edge(master_id, pet_id, kind='pet')
    for husband_id, wife_id in wife_rels.items():
        G.add_edge(husband_id, wife_id, kind='wife')

    all_uids = set(G.nodes())
    name_map, avatar_map = await _collect_members(guild, all_uids)

    # ── 版面配置 ─────────────────────────────────────────
    pos = nx.spring_layout(G, seed=42, k=3.5)

    xs  = [v[0] for v in pos.values()]
    ys  = [v[1] for v in pos.values()]
    pad = 0.9
    x_min, x_max = min(xs) - pad, max(xs) + pad
    y_min, y_max = min(ys) - pad, max(ys) + pad

    fig_w, fig_h = 16, 9
    fig, ax = plt.subplots(figsize=(fig_w, fig_h), dpi=120)

    ax.set_facecolor('white')
    fig.patch.set_facecolor('white')
    ax.set_xlim(x_min, x_max)
    ax.set_ylim(y_min, y_max)
    ax.axis('off')
    ax.set_zorder(1)

    # ── 邊（箭頭） ────────────────────────────────────────
    NODE_R = 0.22   # 節點半徑（軸單位）

    for u, v, edata in G.edges(data=True):
        x1, y1 = pos[u]
        x2, y2 = pos[v]
        dx, dy  = x2 - x1, y2 - y1
        dist    = math.hypot(dx, dy) or 1e-9
        shrink  = NODE_R / dist
        kind    = edata.get('kind', 'pet')
        color   = '#F4A0B8' if kind == 'wife' else '#A8C8E8'
        label   = '我媽'    if kind == 'wife' else '寵物'

        ax.annotate(
            '', zorder=2,
            xy    =(x2 - dx * shrink,  y2 - dy * shrink),
            xytext=(x1 + dx * shrink,  y1 + dy * shrink),
            arrowprops=dict(
                arrowstyle='-|>', color=color, lw=1.6,
                mutation_scale=18,
                connectionstyle='arc3,rad=0.08',
            ),
        )
        angle = math.degrees(math.atan2(dy, dx))
        if angle > 90 or angle < -90:
            angle += 180
        mx, my = (x1 + x2) / 2, (y1 + y2) / 2
        ax.text(mx, my, label, fontsize=16, color='#999999',
                ha='center', va='center', rotation=angle,
                fontfamily=_FONT, zorder=3)

    # ── 節點（頭像圓形） ───────────────────────────────────
    AVATAR_ZOOM = 0.57   # 原 0.19 × 3
    BORDER_CLR  = '#A8C8E8'

    for uid, (x, y) in pos.items():
        if uid in avatar_map:
            oi = OffsetImage(avatar_map[uid], zoom=AVATAR_ZOOM)
            ab = AnnotationBbox(
                oi, (x, y), frameon=True, zorder=5, pad=0.05,
                bboxprops=dict(
                    boxstyle='circle,pad=0.12',
                    fc='white', ec=BORDER_CLR, lw=2.5,
                ),
            )
            ax.add_artist(ab)
        else:
            ax.add_patch(plt.Circle((x, y), NODE_R,
                                    color='#A8C8E8', zorder=5))

        name = name_map.get(uid, uid[:8])
        # 收集指向此節點的主人/老公
        pet_masters = [name_map.get(u, u[:8]) for u, v, d in G.in_edges(uid, data=True) if d.get('kind') == 'pet']
        husbands    = [name_map.get(u, u[:8]) for u, v, d in G.in_edges(uid, data=True) if d.get('kind') == 'wife']
        parts = []
        if pet_masters:
            parts.append(f'{"和".join(pet_masters)}的寵物')
        if husbands:
            parts.append(f'{"和".join(husbands)}的媽媽')
        sub = f'({"、".join(parts)})' if parts else ''

        ax.text(
            x, y - NODE_R - 0.10, name,
            fontsize=30, ha='center', va='top',
            fontfamily=_FONT, fontweight='bold', color='#333333',
            zorder=6,
            path_effects=[pe.withStroke(linewidth=3, foreground='white')],
        )
        if sub:
            ax.text(
                x, y - NODE_R - 0.48, sub,
                fontsize=14, ha='center', va='top',
                fontfamily=_FONT, color='#888888',
                zorder=6,
                path_effects=[pe.withStroke(linewidth=2, foreground='white')],
            )

    # ── 標題 ─────────────────────────────────────────────
    today = datetime.date.today().strftime('%Y-%m-%d')
    title_txt = f'{guild.name} 羈絆關係圖（{today}）'
    # 淺藍底色色塊
    fig.text(
        0.5, 0.97,
        title_txt,
        fontsize=24, fontweight='bold', color='#333333',
        ha='center', va='top', fontfamily=_FONT,
        bbox=dict(boxstyle='round,pad=0.4', fc='#C8E0F4', ec='none'),
    )

    # ── 輸出 ─────────────────────────────────────────────
    buf = io.BytesIO()
    plt.savefig(buf, format='png', bbox_inches='tight', dpi=120,
                facecolor='white')
    plt.close(fig)
    buf.seek(0)
    return buf
