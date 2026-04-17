"""
Pixiv 爬蟲 package — 從 pixiv_crawler/_core.py 再匯出公開 API。
外部只需 `import pixiv_crawler as crawler` 即可使用。
"""
from pixiv_crawler._core import (
    run_full_crawl,
    crawl_user_by_id,
    get_user_name,
    get_user_id_from_artwork,
    enqueue_priority_user,
    get_priority_queue_size,
    clear_priority_queue,
    set_progress_hook,
    set_priority_user_done_hook,
)
