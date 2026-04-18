"""
Pixiv 模組設定 - 路徑指向 pixivdata/ 資料夾
"""
import os
from config import PIXIV_REFRESH_TOKEN, PIXIV_REFRESH_TOKENS, PIXIV_WEB_COOKIE  # noqa: F401 (re-export for crawler modules)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PIXIV_DATA_DIR = os.path.join(BASE_DIR, "pixivdata")
IMAGES_DIR = os.path.join(PIXIV_DATA_DIR, "images")

DATA_DIR = os.path.join(PIXIV_DATA_DIR, "data")
DB_PATH = os.path.join(DATA_DIR, "pixiv.db")
FAISS_INDEX_PATH = os.path.join(DATA_DIR, "feature.index")
PAGE_LOG_DIR = os.path.join(PIXIV_DATA_DIR, "pagedata")

LOGS_DIR = os.path.join(PIXIV_DATA_DIR, "logs")
LOG_FILE = os.path.join(LOGS_DIR, "spider.log")

# ===== 爬取設定 =====
CRAWL_LIMIT = 50
CRAWL_TAGS = ["風景", "插畫"]
CRAWL_MODE = "day"
MIN_BOOKMARKS = 0

# ===== 全站爬取設定 =====
# 只留 day：week/month/原創/rookie 與 day 高度重疊，熱門作品 tag 與 related 擴散會自然覆蓋。
ALL_RANKING_MODES = ["day"]
ALL_TAGS = [
    # 核心優先：蘿莉・可愛・特定作品 (最上位)
    "森亜るるか",
    "ロリ", "メイド服", "ケモミミ", "猫耳", "cute", "女の子",
    "たんプリ1000users入り", "10000users入り",

    # 二次元熱門遊戲
    "アークナイツ", "arknights",                     # 明日方舟
    "Arknights:Endfield", "エンドフィールド",        # 終末地
    "ロッシ(エンドフィールド)", "ロッシ",
    "ブルーアーカイブ", "ブルアカ", "bluearchive", "blueazur",
    "プロジェクトセカイ",
    "原神", "崩壊:スターレイル", "ウマ娘プリティーダービー",
    "Fate/Grand Order", "アズールレーン",
    "アイドルマスター", "ポケットモンスター",
    "ゼルダの伝説", "スプラトゥーン",
    "勝利の女神:NIKKE", "東方Project",

    # 一般插畫與基礎分類
    "オリジナル", "original", "イラスト", "illustration", "anime",
    "character", "digital art", "painting", "ファンタジー", "fantasy",

    # 背景・物件與其他
    "男の子",

    # ===== 長尾擴充（低重複、走廣度）=====
    # 服裝・場景
    "セーラー服", "スクール水着", "体操服", "チャイナドレス", "和服",
    "ランジェリー", "水着", "浴衣", "ドレス", "制服", "ゴスロリ",
    "パーカー", "ニーソックス", "タイツ",

    # 身材・姿勢
    "ツインテール", "ポニーテール", "銀髪", "金髪", "黒髪", "ロングヘア", "ショートヘア",
    "眼鏡", "ヘッドホン", "笑顔", "横顔", "立ち絵", "座り", "寝そべり",

    # 角色類型
    "獣人", "ドラゴン", "エルフ", "魔女", "天使", "悪魔", "メカ少女",
    "シスター", "巫女", "ナース", "バニーガール", "アイドル",

    # 遊戲・動畫（補）
    "ファイアーエムブレム", "ドラゴンクエスト", "ファイナルファンタジー",
    "ゲゲゲの鬼太郎", "鬼滅の刃", "呪術廻戦", "チェンソーマン", "SPY×FAMILY",
    "ヒロアカ", "ラブライブ", "バーチャルYouTuber", "ホロライブ", "にじさんじ",
    "ニーアオートマタ", "メイドインアビス",

    # 場景・氛圍
    "風景", "背景", "空", "海", "夜景", "桜", "雨", "雪", "森", "花",
    "ファンアート", "線画", "落書き", "色鉛筆", "水彩", "油絵", "pixelart",

    # 英文 tag（覆蓋西方用戶）
    "girl", "boy", "portrait", "landscape", "fanart", "oc",
    "schoolgirl", "maid", "dress", "kawaii", "pixiv",

    # BL / GL / CP
    "BL", "GL", "百合", "女の子同士",
]

FULL_CRAWL_API_DELAY = 0.3
INDEX_REBUILD_INTERVAL = 500

# DB 批次查詢「已完整索引」作品的 chunk size（避免載入全部 ID 進記憶體；也避免 SQLite 變數上限）
FULLY_INDEXED_QUERY_CHUNK_SIZE: int = 800

# tag 搜尋排序方向：熱門→最新→最舊，盡量覆蓋全站（含歷史作品）
CRAWL_TAG_SORTS = ["date_desc", "date_asc"]

# 額外追加關鍵字（去重）
ALL_TAGS = list(dict.fromkeys([*ALL_TAGS, "セーラー服"]))

# illust_new 種子：每輪從「全站最新上傳」抓的頁數（每頁 30 件）
# 80 頁 ≈ 2400 件，覆蓋 Pixiv 最新 ~1 小時（每小時 ~2000 件）
NEW_ILLUSTS_MAX_PAGES: int = 80

# ===== 圖片下載設定 =====
# 偏好下載尺寸：
#   "large"    → 1200px 長邊（master_1200），平均 ~400 KB，pHash 漂移 0–1 bits
#   "original" → 原尺寸（可達數 MB），pHash 無失真但吃爆頻寬
# 反搜用 pHash 解析度不變性，"large" 即可，省下 3–5x 頻寬。
PREFERRED_IMAGE_SIZE: str = "large"

# tag 搜尋書籤上限：>0 時只抓 bookmarks ≤ 此值的作品（走長尾，避開 ranking 重疊）；0 = 停用
TAG_BOOKMARK_MAX: int = 1000

MAX_IMAGE_SIZE = (1024, 1024)
DOWNLOAD_WORKERS = 24          # 並行下載數（i.pximg.net 寬容，24 workers 仍安全）
DOWNLOAD_RETRIES = 3
DOWNLOAD_CHUNK_SIZE = 64 * 1024
# 下載頻寬上限：0 = 關閉限速（僅受下載 worker 並發與網卡頻寬限制）
# i.pximg.net CDN 對下載非常寬容，不會造成 ban 風險
DOWNLOAD_RATE_LIMIT_Mbps = 0
MAX_DOWNLOAD_RATE_LIMIT_Mbps = 0

# illust_detail API 最大並發數（用於漫畫多頁 URL 補抓）
# 每個 api 實例獨立 token，同時 6 個仍在安全區
API_DETAIL_CONCURRENCY: int = 6
PIXIV_API_TIMEOUT: float = 60.0
# requests 底層 timeout（connect / read 秒數），須 < PIXIV_API_TIMEOUT；
# 用意是讓同步 HTTP 呼叫在 asyncio.wait_for 外層開鍘前就自行 raise，
# 避免 executor 執行緒被 TCP 半卡連線無限期佔住。
PIXIV_API_CONNECT_TIMEOUT: float = 10.0
PIXIV_API_READ_TIMEOUT: float = 30.0
RELATED_API_TIMEOUT: float = 20.0
RELATED_MAX_PAGES: int = 10          # 相關作品每次最多抓幾頁後跳出（避免滾雪球）

# 擴散排程配額：避免 user_sync 長時間壟斷，讓 tag/ranking 可持續前進
DIFFUSION_USER_QUOTA_PER_TICK: int = 5
DIFFUSION_RELATED_QUOTA_PER_TICK: int = 5
DIFFUSION_TAIL_MULTIPLIER: int = 5
SEED_SOURCES_PER_DIFFUSION_TICK: int = 5
# 擴散佇列硬上限：滿了就 drop producer，避免無界堆積吃 RAM
DIFFUSION_USER_Q_MAXSIZE: int = 10_000
DIFFUSION_RELATED_Q_MAXSIZE: int = 20_000

# visited_users Bloom filter 參數：
# - N 設 1 億（預留破億筆 user_id 的空間）
# - FP 0.01 下約 120 MB；false positive 代表「少擴散一位作者」，下輪可重新發現
VISITED_USERS_BLOOM_N: int = 100_000_000
VISITED_USERS_BLOOM_FP: float = 0.01

# ===== 特徵提取設定 =====
PHASH_BITS = 64          # pHash 位元數（8x8 DCT = 64 bits = 8 bytes）
MAX_GALLERY_PAGES = 100   # 每件作品最多索引的頁數（避免大型漫畫拖垮爬取）

# ===== NN binary hash（SSCD，抗裁切/修圖/翻譯的主力）=====
# SSCD (Self-Supervised Copy Detection) 512-d float embedding → sign quantize → 64 B (512-bit)
# 整個索引 150M 頁僅 9.6 GB，FAISS IndexBinaryFlat 夠撐到 ~250M；之後可遷 IVF。
NN_HASH_BITS: int = 512
NN_HASH_BYTES: int = 64
NN_INDEX_PATH = os.path.join(DATA_DIR, "nn.index")
NN_MODEL_CACHE = os.path.join(os.path.expanduser("~"), ".cache", "sscd")
NN_MODEL_URL = "https://dl.fbaipublicfiles.com/sscd-copy-detection/sscd_disc_mixup.torchscript.pt"
NN_INPUT_SIZE: int = 288
NN_BATCH_SIZE: int = 32

# ===== 狀態網頁伺服器 =====
STATUS_WEB_PORT: int = int(os.environ.get("PIXIV_STATUS_PORT", "8766"))

# ===== 作者 ID 順序掃描設定 =====
# 從 user_id=1 開始往上掃，找出全站所有作者並爬取其作品
USER_ID_SCAN_ENABLED: bool = True
# worker 數量：每個 segment 一個 worker，並行探測不同 user_id 區段
USER_ID_SCAN_WORKERS: int = 6
# 每次 user_detail API 呼叫後的強制等待（秒）
# 獨立 scan_api token，0.3s = ~3.3 req/s，安全區間
USER_ID_SCAN_DELAY: float = 0.3
USER_ID_SCAN_CURSOR_FILE: str = os.path.join(DATA_DIR, "user_id_scan_cursor.json")

# user_id 分段掃描：Pixiv 的活躍作者多集中在特定 ID 區段，
# 線性掃 1→N 會浪費大量時間在死帳號區。分段並行能同時覆蓋多個「活躍區」。
# 格式：[(start, end_exclusive), ...]；end=None 表示無上限（只到 Pixiv 最新 user_id）
# 下列區段涵蓋 Pixiv 帳號史：
#   0–1M       早期帳號（大多死帳號，但有歷史元老）
#   1M–10M     2010–2015 高峰期
#   10M–30M    2015–2019 擴張期（密度最高）
#   30M–60M    2019–2022
#   60M–90M    2022–2024
#   90M+       2024–至今
USER_ID_SCAN_SEGMENTS: list[tuple[int, int | None]] = [
    (0, 1_000_000),
    (1_000_000, 10_000_000),
    (10_000_000, 30_000_000),
    (30_000_000, 60_000_000),
    (60_000_000, 90_000_000),
    (90_000_000, None),
]

# ===== Tag 進度爬取設定 =====
TAG_CRAWL_PROGRESS_FILE: str = os.path.join(DATA_DIR, "tag_crawl_progress.json")
RANKING_LAST_RUN_FILE: str = os.path.join(DATA_DIR, "ranking_last_run.json")
TAG_PAGES_PER_VISIT: int = 30    # 每個 tag/sort 每次最多抓幾頁後換 tag（淺抓換廣度，下輪回補）
TAG_FETCH_FLUSH_PAGES: int = 20  # tag stream 每抓幾頁就 flush 一批給下載器（邊抓邊下）
USER_SCAN_BATCH_SIZE: int = 100   # tag 抓完 200 頁後，切換爬 user_scan 的有效用戶數
TAGS_PER_ROUND: int = len(ALL_TAGS) * 2  # 每輪輪詢的 tag 數量（預設跑全部）

# ===== Tag 日期切片（突破 offset 5000 硬性上限）=====
# Pixiv search_illust 的 offset 硬性上限 5000（≈ 167 頁 × 30 件），
# 大 tag 會被截斷。改用 start_date/end_date 逐年切片，每個窗口各自 5000 件 → 十倍覆蓋。
# 0 = 停用（維持舊行為，只用 sort 翻頁）；>0 = 每個日期窗口的天數
TAG_DATE_SLICE_DAYS: int = 365         # 一年一窗口
TAG_DATE_SLICE_START: str = "2007-09-10"   # Pixiv 上線日
TAG_DATE_SLICE_MAX_PAGES_PER_WINDOW: int = 167  # offset 5000 / 30 = 166.67，保守取 167

# 作者作品抓取類型（會逐類型抓取後去重）
USER_FETCH_TYPES = ["illust", "manga", "ugoira"]

# ===== 代理設定（可選）=====
PROXY = os.environ.get("HTTP_PROXY", None)
PIXIV_TAG_IMPERSONATE = "chrome124"
