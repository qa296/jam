import time
import xxhash 
import asyncio
import sys
from typing import Dict, Any, Optional, Tuple
import logging
from collections import deque
from app.utils.logging import log
import app.config.settings as settings
logger = logging.getLogger("my_logger")

# 定义缓存项的结构
CacheItem = Dict[str, Any]

class ResponseCacheManager:
    """管理API响应缓存的类，一个键可以对应多个缓存项（使用deque）"""
    
    def __init__(self, expiry_time: int, max_entries: int, 
                 cache_dict: Dict[str, deque[CacheItem]] = None):
        """
        初始化缓存管理器。
        
        Args:
            expiry_time (int): 缓存项的过期时间（秒）。
            max_entries (int): 缓存中允许的最大总条目数。
            cache_dict (Dict[str, deque[CacheItem]], optional): 初始缓存字典。默认为 None。
        """
        self.cache: Dict[str, deque[CacheItem]] = cache_dict if cache_dict is not None else {}
        self.expiry_time = expiry_time
        self.max_entries = max_entries # 总条目数限制
        self.cur_cache_num = 0 # 当前条目数
        self.cur_cache_size = 0  # 当前缓存占用的总大小（字节）
        self.max_total_size = settings.MAX_CACHE_SIZE  # 缓存总占用大小限制
        self.lock = asyncio.Lock() # Added lock

    def _estimate_size(self, obj: Any, seen: Optional[set] = None) -> int:
        """递归估算对象占用的内存大小"""
        if seen is None:
            seen = set()

        obj_id = id(obj)
        if obj_id in seen:
            return 0
        seen.add(obj_id)

        # 优先将 Pydantic 模型或具有 dict 能力的对象转成普通结构
        try:
            if hasattr(obj, "model_dump"):
                obj = obj.model_dump()
            elif hasattr(obj, "dict"):
                obj = obj.dict()
        except Exception:
            pass

        size = sys.getsizeof(obj)

        if isinstance(obj, dict):
            size += sum(self._estimate_size(k, seen) + self._estimate_size(v, seen) for k, v in obj.items())
        elif isinstance(obj, (list, tuple, set, deque)):
            size += sum(self._estimate_size(item, seen) for item in obj)

        return size

    async def get(self, cache_key: str) -> Tuple[Optional[Any], bool]: # Made async
        """获取指定键的第一个有效缓存项（不删除）"""
        now = time.time()
        async with self.lock:
            if cache_key in self.cache:
                cache_deque = self.cache[cache_key]
                # 查找第一个未过期的项，且不删除
                for item in cache_deque:
                    if now < item.get('expiry_time', 0):
                        response = item.get('response',None)
                        return response, True
            
            return None, False

    async def get_and_remove(self, cache_key: str) -> Tuple[Optional[Any], bool]:
        """获取并删除指定键的第一个有效缓存项。"""
        now = time.time()
        async with self.lock:
            if cache_key in self.cache:
                cache_deque = self.cache[cache_key]

                # 查找第一个有效项并收集过期项
                valid_item_to_remove = None
                response_to_return = None
                new_deque = deque()
                items_removed_count = 0
                size_freed = 0

                for item in cache_deque:
                    if now < item.get('expiry_time', 0):
                        if valid_item_to_remove is None: # 找到第一个有效项
                            valid_item_to_remove = item
                            response_to_return = item.get('response', None)
                            items_removed_count += 1 # 计数此项为移除
                            size_freed += item.get('size', 0)
                            
                        else:
                            new_deque.append(item) # 保留后续有效项
                    else:
                        items_removed_count += 1 # 计数过期项为移除
                        size_freed += item.get('size', 0)

                # 更新缓存状态
                if items_removed_count > 0:
                    self.cur_cache_num = max(0, self.cur_cache_num - items_removed_count)
                    self.cur_cache_size = max(0, self.cur_cache_size - size_freed)
                    if not new_deque:
                        # 如果所有项都被移除（过期或我们取的那个）
                        del self.cache[cache_key]
                    else:
                        self.cache[cache_key] = new_deque

                if valid_item_to_remove:
                    return response_to_return, True # 返回找到的有效项

            # 如果键不存在或未找到有效项
            return None, False

    async def store(self, cache_key: str, response: Any):
        """存储响应到缓存（追加到键对应的deque）"""
        
        # 使用更精确的大小估算
        response_size = self._estimate_size(response)
        if response_size > settings.MAX_CACHE_ITEM_SIZE:
            log('info', f"响应大小 {response_size} 字节超过限制 {settings.MAX_CACHE_ITEM_SIZE} 字节，跳过缓存")
            return
        
        now = time.time()
        new_item: CacheItem = {
            'response': response,
            'expiry_time': now + self.expiry_time,
            'created_at': now,
            'size': response_size,
        }

        needs_cleaning_count = False
        needs_cleaning_size = False
        async with self.lock:
            if cache_key not in self.cache:
                self.cache[cache_key] = deque()
            
            self.cache[cache_key].append(new_item) # 追加到deque末尾
            self.cur_cache_num += 1
            self.cur_cache_size += response_size
            needs_cleaning_count = self.cur_cache_num > self.max_entries
            needs_cleaning_size = self.cur_cache_size > self.max_total_size

        if needs_cleaning_count or needs_cleaning_size:
             # 在锁外调用清理，避免长时间持有锁
             await self.clean_if_needed()

    async def clean_expired(self):
        """清理所有缓存项中已过期的项。"""
        now = time.time()
        total_cleaned = 0
        total_size_freed = 0
        async with self.lock:
            # 迭代 cache 的副本以允许在循环中安全地修改 cache
            for key, cache_deque in list(self.cache.items()):
                original_len = len(cache_deque)
                # 创建一个新的 deque，只包含未过期的项
                expired_items = [item for item in cache_deque if now >= item.get('expiry_time', 0)]
                valid_items = deque(item for item in cache_deque if now < item.get('expiry_time', 0))
                cleaned_count = original_len - len(valid_items)

                if cleaned_count > 0:
                    # 统计释放的内存大小
                    for item in expired_items:
                        total_size_freed += item.get('size', 0)
                    
                    log('info', f"清理键 {key[:8]}... 的过期缓存项 {cleaned_count} 个。")
                    total_cleaned += cleaned_count

                if not valid_items:
                    # 在持有锁时直接删除键
                    if key in self.cache:
                         del self.cache[key]
                         log('info', f"缓存键 {key[:8]}... 的所有项均已过期，移除该键。")
                elif cleaned_count > 0:
                    # 替换为只包含有效项的 deque
                    self.cache[key] = valid_items

            # 统一更新缓存计数
            if total_cleaned > 0:
                 self.cur_cache_num = max(0, self.cur_cache_num - total_cleaned)
                 self.cur_cache_size = max(0, self.cur_cache_size - total_size_freed)

    async def clean_if_needed(self):
        """如果缓存总条目数或总大小超过限制，按时间顺序清理最旧的项目。"""

        async with self.lock: 
            if self.cur_cache_num <= self.max_entries and self.cur_cache_size <= self.max_total_size:
                return

            # 收集所有缓存项及其元数据
            all_items_meta = []
            for key, cache_deque in self.cache.items():
                for item in cache_deque:
                    all_items_meta.append({
                        'key': key,
                        'created_at': item.get('created_at', 0),
                        'item': item,
                        'size': item.get('size', 0)
                    })

            if not all_items_meta:
                return

            # 按创建时间从旧到新排序
            all_items_meta.sort(key=lambda x: x['created_at'])

            items_actually_removed = 0
            size_freed = 0
            keys_potentially_empty = set()

            for item_meta in all_items_meta:
                if self.cur_cache_num <= self.max_entries and self.cur_cache_size <= self.max_total_size:
                    break

                key_to_clean = item_meta['key']
                item_to_clean = item_meta['item']
                item_size = item_meta['size']

                if key_to_clean in self.cache:
                    try:
                        # 直接从 deque 中移除指定的 item 对象
                        self.cache[key_to_clean].remove(item_to_clean)
                        items_actually_removed += 1
                        size_freed += item_size
                        self.cur_cache_num = max(0, self.cur_cache_num - 1)
                        self.cur_cache_size = max(0, self.cur_cache_size - item_size)
                        log('info', f"因容量限制，删除键 {key_to_clean[:8]}... 的旧缓存项 (创建于 {item_meta['created_at']})。")
                        if not self.cache[key_to_clean]:
                            keys_potentially_empty.add(key_to_clean)
                    except (KeyError, ValueError):
                        log('warning', f"尝试因容量限制删除缓存项时未找到 (可能已被提前移除): {key_to_clean[:8]}...")
                        continue

            # 检查是否有 deque 因本次清理变空
            for key in keys_potentially_empty:
                 if key in self.cache and not self.cache[key]:
                     del self.cache[key]
                     log('info', f"因容量限制清理后，键 {key[:8]}... 的deque已空，移除该键。")

            if items_actually_removed > 0 or size_freed > 0:
                 log('info', f"因容量限制，共清理了 {items_actually_removed} 个旧缓存项，释放 {size_freed} 字节。清理后缓存数: {self.cur_cache_num}，总大小: {self.cur_cache_size} 字节")

def generate_cache_key(chat_request, last_n_messages: int = 65536, is_gemini=False) -> str:
    """
    根据模型名称和最后 N 条消息生成请求的唯一缓存键。
    Args:
        chat_request: 包含模型和消息列表的请求对象 (符合OpenAI格式)。
        last_n_messages: 需要包含在缓存键计算中的最后消息的数量。
    Returns:
        一个代表该请求的唯一缓存键字符串 (xxhash64哈希值)。
    """
    h = xxhash.xxh64()
    
    # 1. 哈希模型名称
    h.update(chat_request.model.encode('utf-8'))

    if last_n_messages <= 0:
        # 如果不考虑消息，直接返回基于模型的哈希
        return h.hexdigest()

    messages_processed = 0
    
    # 2. 增量哈希最后 N 条消息 (从后往前)
    if is_gemini:    
        # log('INFO', f"开启增量哈希gemini格式内容")
        for content_item in reversed(chat_request.payload.contents):
            if messages_processed >= last_n_messages:
                break
            role = content_item.get('role')
            if role is not None and isinstance(role, str):
                h.update(b'role:')
                h.update(role.encode('utf-8'))
            # log('INFO', f"哈希gemini格式角色{role}")
            parts = content_item.get('parts', [])
            if not isinstance(parts, list):
                parts = []
            for part in parts:
                text_content = part.get('text')
                if text_content is not None and isinstance(text_content, str):
                    h.update(b'text:')
                    h.update(text_content.encode('utf-8'))
                    # log('INFO', f"哈希gemini格式文本内容{text_content}")
                
                inline_data_obj = part.get('inline_data')
                if inline_data_obj is not None and isinstance(inline_data_obj, dict):
                    h.update(b'inline_data:')
                    data_payload = inline_data_obj.get('data', '')
                    # log('INFO', f"哈希gemini格式非文本内容{data_payload[:32]}")
                    if isinstance(data_payload, str):
                        h.update(b'data_prefix:')
                        h.update(data_payload[:32].encode('utf-8'))

                file_data_obj = part.get('file_data')
                if file_data_obj is not None and isinstance(file_data_obj, dict):
                    h.update(b'file_data:')
                    file_uri = file_data_obj.get('file_uri', '')
                    if isinstance(file_uri, str):
                        h.update(b'file_uri:')
                        h.update(file_uri.encode('utf-8'))
            messages_processed += 1
    
    else :
        for msg in reversed(chat_request.messages):
            if messages_processed >= last_n_messages:
                break

            # 哈希角色
            h.update(b'role:')
            h.update(msg.get('role', '').encode('utf-8'))

            # 哈希内容
            content = msg.get('content')
            if isinstance(content, str):
                h.update(b'text:')
                h.update(content.encode('utf-8'))
            elif isinstance(content, list):
                # 处理图文混合内容
                for item in content:
                    item_type = item.get('type') if hasattr(item, 'get') else None
                    if item_type == 'text':
                        text = item.get('text', '') if hasattr(item, 'get') else ''
                        h.update(b'text:') 
                        h.update(text.encode('utf-8'))
                    elif item_type == 'image_url':
                        image_url = item.get('image_url', {}) if hasattr(item, 'get') else {}
                        image_data = image_url.get('url', '') if hasattr(image_url, 'get') else ''
                        
                        h.update(b'image_url:') # 加入类型标识符
                        if image_data.startswith('data:image/'):
                            # 对于base64图像，使用前32字符作为标识符
                            h.update(image_data[:32].encode('utf-8'))
                        else:
                            h.update(image_data.encode('utf-8'))

            messages_processed += 1
    return h.hexdigest()
