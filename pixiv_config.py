"""
Pixiv 模組設定 - 路徑指向 pixivdata/ 資料夾
"""
import os
from config import PIXIV_REFRESH_TOKEN, PIXIV_WEB_COOKIE  # noqa: F401 (re-export for crawler modules)

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
ALL_RANKING_MODES = [
    "day", "week", "month",
    "day_male", "day_female",
    "week_original", "week_rookie",
]
ALL_TAGS = [
    # 核心優先：蘿莉・可愛・特定作品 (最上位)
    "森亜るるか", 
    "ロリ", "メイド服", "ケモミミ", "#猫耳", "cute", "女の子", 
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
    "男の子"
]

FULL_CRAWL_API_DELAY = 1.3
INDEX_REBUILD_INTERVAL = 500

# tag 搜尋排序方向：熱門→最新→最舊，盡量覆蓋全站（含歷史作品）
CRAWL_TAG_SORTS = ["date_desc", "date_asc"]

# 額外追加關鍵字（去重）
ALL_TAGS = list(dict.fromkeys([*ALL_TAGS, "セーラー服"]))

# illust_new 種子：每輪從「全站最新上傳」抓的頁數（每頁 30 件）
NEW_ILLUSTS_MAX_PAGES: int = 15

# ===== 圖片下載設定 =====
MAX_IMAGE_SIZE = (1024, 1024)
DOWNLOAD_WORKERS = 6           # 並行下載數（多核 I/O）
DOWNLOAD_RETRIES = 3
DOWNLOAD_CHUNK_SIZE = 64 * 1024
DOWNLOAD_RATE_LIMIT_Mbps = 120
MAX_DOWNLOAD_RATE_LIMIT_Mbps = 120

# illust_detail API 最大並發數（用於漫畫多頁 URL 補抓）
API_DETAIL_CONCURRENCY: int = 3
PIXIV_API_TIMEOUT: float = 60.0
RELATED_API_TIMEOUT: float = 20.0

# 擴散排程配額：避免 user_sync 長時間壟斷，讓 tag/ranking 可持續前進
DIFFUSION_USER_QUOTA_PER_TICK: int = 5
DIFFUSION_RELATED_QUOTA_PER_TICK: int = 5
DIFFUSION_TAIL_MULTIPLIER: int = 5
SEED_SOURCES_PER_DIFFUSION_TICK: int = 5

# ===== 特徵提取設定 =====
PHASH_BITS = 64          # pHash 位元數（8x8 DCT = 64 bits = 8 bytes）
MAX_GALLERY_PAGES = 100   # 每件作品最多索引的頁數（避免大型漫畫拖垮爬取）

# ===== 狀態網頁伺服器 =====
STATUS_WEB_PORT: int = int(os.environ.get("PIXIV_STATUS_PORT", "8766"))

# ===== 作者 ID 順序掃描設定 =====
# 從 user_id=1 開始往上掃，找出全站所有作者並爬取其作品
USER_ID_SCAN_ENABLED: bool = True
# worker 數量：api_sem=1 讓所有 worker 序列化 user_detail，
# 多 worker 只是讓下載/處理與下一個 user_detail 探測重疊（pipeline 效果）
USER_ID_SCAN_WORKERS: int = 3
# 每次 user_detail API 呼叫後的強制等待（秒）
# api_sem=1 下，實際速率 = 1 / USER_ID_SCAN_DELAY 次/秒
# 加上主爬蟲同時也在發請求，建議 ≥ 1.5
USER_ID_SCAN_DELAY: float = 1.5
USER_ID_SCAN_CURSOR_FILE: str = os.path.join(DATA_DIR, "user_id_scan_cursor.json")

# ===== Tag 進度爬取設定 =====
TAG_CRAWL_PROGRESS_FILE: str = os.path.join(DATA_DIR, "tag_crawl_progress.json")
TAG_PAGES_PER_VISIT: int = 100   # 每個 tag/sort 每次最多抓幾頁後換 tag
USER_SCAN_BATCH_SIZE: int = 50   # tag 抓完 100 頁後，切換爬 user_scan 的有效用戶數

# 作者作品抓取類型（會逐類型抓取後去重）
USER_FETCH_TYPES = ["illust", "manga", "ugoira"]

# ===== 代理設定（可選）=====
PROXY = os.environ.get("HTTP_PROXY", None)
PIXIV_TAG_IMPERSONATE = "chrome124"
