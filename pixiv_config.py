"""
Pixiv 模組設定 - 路徑指向 pixivdata/ 資料夾
"""
import os
from config import PIXIV_REFRESH_TOKEN  # noqa: F401 (re-export for crawler modules)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PIXIV_DATA_DIR = os.path.join(BASE_DIR, "pixivdata")
IMAGES_DIR = os.path.join(PIXIV_DATA_DIR, "images")

DATA_DIR = os.path.join(PIXIV_DATA_DIR, "data")
DB_PATH = os.path.join(DATA_DIR, "pixiv.db")
FAISS_INDEX_PATH = os.path.join(DATA_DIR, "feature.index")

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
    # 一般插畫
    "オリジナル", "女の子", "イラスト", "風景", "ファンタジー",
    "男の子", "動物", "メカ", "建築", "食べ物",
    "original", "illustration", "fantasy", "landscape", "cute",
    "anime", "character", "digital art", "painting",
    # 服裝・萌え要素
    "メイド服", "ロリ", "ケモミミ",
    # ブックマーク数
    "10000users入り", "たんプリ1000users入り",
    # ゲーム
    "Fate/Grand Order", "原神", "ブルーアーカイブ", "ブルアカ",
    "勝利の女神:NIKKE", "ウマ娘プリティーダービー", "アイドルマスター",
    "プロジェクトセカイ", "アズールレーン",
    "アークナイツ", "arknights", "Arknights:Endfield", "エンドフィールド", "ロッシ(エンドフィールド)", "ロッシ",
    "崩壊:スターレイル", "東方Project",
    "ポケットモンスター", "ゼルダの伝説", "スプラトゥーン",
    # その他
    "名探偵プリキュア!", "森亜るるか",
]
FULL_CRAWL_API_DELAY = 1.0
INDEX_REBUILD_INTERVAL = 500

# tag 搜尋排序方向：先熱門再最新，盡量覆蓋全站
CRAWL_TAG_SORTS = ["popular_desc", "date_desc"]

# ===== 圖片下載設定 =====
MAX_IMAGE_SIZE = (1024, 1024)
DOWNLOAD_WORKERS = 4
DOWNLOAD_RETRIES = 3
DOWNLOAD_CHUNK_SIZE = 64 * 1024
DOWNLOAD_RATE_LIMIT_Mbps = 120
MAX_DOWNLOAD_RATE_LIMIT_Mbps = 120

# ===== 特徵提取設定 =====
PHASH_BITS = 64          # pHash 位元數（8x8 DCT = 64 bits = 8 bytes）
MAX_GALLERY_PAGES = 20   # 每件作品最多索引的頁數（避免大型漫畫拖垮爬取）

# ===== 狀態網頁伺服器 =====
STATUS_WEB_PORT: int = int(os.environ.get("PIXIV_STATUS_PORT", "8766"))

# ===== 作者 ID 順序掃描設定 =====
# 從 user_id=1 開始往上掃，找出全站所有作者並爬取其作品
USER_ID_SCAN_ENABLED: bool = True
# worker 數量：api_sem=1 讓所有 worker 序列化 user_detail，
# 多 worker 只是讓下載/處理與下一個 user_detail 探測重疊（pipeline 效果）
USER_ID_SCAN_WORKERS: int = 2
# 每次 user_detail API 呼叫後的強制等待（秒）
# api_sem=1 下，實際速率 = 1 / USER_ID_SCAN_DELAY 次/秒
# 加上主爬蟲同時也在發請求，建議 ≥ 2.0
USER_ID_SCAN_DELAY: float = 2.0
USER_ID_SCAN_CURSOR_FILE: str = os.path.join(DATA_DIR, "user_id_scan_cursor.json")

# ===== 代理設定（可選）=====
PROXY = os.environ.get("HTTP_PROXY", None)
