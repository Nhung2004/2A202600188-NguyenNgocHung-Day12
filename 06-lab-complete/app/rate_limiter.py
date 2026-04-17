"""
In-Memory Rate Limiter

Giới hạn số request mỗi user trong 1 khoảng thời gian.
Trong production: thay bằng Redis-based rate limiter để scale.

Algorithm: Sliding Window Counter
- Mỗi user có 1 bucket
- Bucket đếm request trong window (60 giây)
- Vượt quá limit → trả về 429 Too Many Requests
"""
import os
import time
import logging
import redis
from fastapi import HTTPException

logger = logging.getLogger(__name__)

class RateLimiter:
    def __init__(self, max_requests: int = 10, window_seconds: int = 60):
        """
        Args:
            max_requests: Số request tối đa trong window
            window_seconds: Khoảng thời gian (giây)
        """
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        
        # Redis setup
        redis_url = os.getenv("REDIS_URL", "redis://localhost:6379/0")
        try:
            self.r = redis.from_url(redis_url, decode_responses=True)
            self.r.ping()
            self.use_redis = True
        except Exception as e:
            logger.warning(f"Redis not available for RateLimiter: {e}. Falling back to in-memory (not recommended for production).")
            self.use_redis = False
            self._windows = {} # Fallback in-memory

    def check(self, user_id: str) -> dict:
        """
        Kiểm tra user có vượt rate limit không bằng thuật toán Sliding Window.
        Sử dụng Redis Sorted Set (ZSET).
        """
        now = time.time()
        key = f"rate_limit:{user_id}"
        
        if self.use_redis:
            try:
                # 1. Loại bỏ các request cũ ngoài window
                self.r.zremrangebyscore(key, 0, now - self.window_seconds)
                
                # 2. Đếm số request trong window hiện tại
                request_count = self.r.zcard(key)
                
                if request_count >= self.max_requests:
                    # Lấy timestamp của request cũ nhất để tính Retry-After
                    oldest = self.r.zrange(key, 0, 0, withscores=True)
                    retry_after = 0
                    if oldest:
                        retry_after = int(float(oldest[0][1]) + self.window_seconds - now) + 1
                    
                    raise self._raise_429(retry_after)

                # 3. Thêm request mới
                # Dùng pipeline để đảm bảo atomic và tăng tốc
                pipe = self.r.pipeline()
                pipe.zadd(key, {str(now): now})
                pipe.expire(key, self.window_seconds + 10) # Dọn dẹp key nếu không dùng
                pipe.execute()
                
                remaining = self.max_requests - (request_count + 1)
            except redis.RedisError as e:
                logger.error(f"Redis error in RateLimiter: {e}")
                # Optional: fallback to in-memory or allow request
                remaining = 0
        else:
            # Fallback in-memory logic (giữ lại code cũ)
            from collections import deque
            if user_id not in self._windows:
                self._windows[user_id] = deque()
            window = self._windows[user_id]
            while window and window[0] < now - self.window_seconds:
                window.popleft()
            if len(window) >= self.max_requests:
                retry_after = int(window[0] + self.window_seconds - now) + 1
                raise self._raise_429(retry_after)
            window.append(now)
            remaining = self.max_requests - len(window)

        return {
            "limit": self.max_requests,
            "remaining": max(0, remaining),
            "reset_at": int(now + self.window_seconds),
        }

    def _raise_429(self, retry_after: int):
        return HTTPException(
            status_code=429,
            detail={
                "error": "Rate limit exceeded",
                "limit": self.max_requests,
                "window_seconds": self.window_seconds,
                "retry_after_seconds": retry_after,
            },
            headers={"Retry-After": str(max(1, retry_after))},
        )

    def get_stats(self, user_id: str) -> dict:
        """Trả về stats của user (không check limit)."""
        key = f"rate_limit:{user_id}"
        if self.use_redis:
            now = time.time()
            self.r.zremrangebyscore(key, 0, now - self.window_seconds)
            active = self.r.zcard(key)
        else:
            active = 0 # Simplified fallback
        return {
            "requests_in_window": active,
            "limit": self.max_requests,
            "remaining": max(0, self.max_requests - active),
        }

# Singleton instances cho các tiers khác nhau
rate_limiter_user = RateLimiter(max_requests=10, window_seconds=60)
rate_limiter_admin = RateLimiter(max_requests=100, window_seconds=60)
