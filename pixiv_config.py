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
    "オリジナル", "女の子", "イラスト", "風景", "ファンタジー",
    "男の子", "動物", "メカ", "建築", "食べ物",
    "original", "illustration", "fantasy", "landscape", "cute",
    "anime", "character", "digital art", "painting",
]
FULL_CRAWL_PER_SOURCE = 500
FULL_CRAWL_API_DELAY = 1.0
INDEX_REBUILD_INTERVAL = 500

# ===== 圖片下載設定 =====
MAX_IMAGE_SIZE = (1024, 1024)
DOWNLOAD_WORKERS = 4

# ===== 特徵提取設定 =====
COLOR_BINS = 32
COLOR_FEATURE_DIM = 32 * 3  # 96 維
THUMB_SIZE = (224, 224)

# ===== 代理設定（可選）=====
PROXY = os.environ.get("HTTP_PROXY", None)
