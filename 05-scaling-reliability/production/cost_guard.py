"""
Cost Guard — Bảo Vệ Budget LLM

Mục tiêu: Tránh bill bất ngờ từ LLM API.
- Đếm tokens đã dùng mỗi ngày
- Cảnh báo khi gần hết budget
- Block khi vượt budget

Trong production: lưu trong Redis/DB, không phải in-memory.
"""
import os
import time
import logging
import json
import redis
from dataclasses import dataclass, field
from fastapi import HTTPException

logger = logging.getLogger(__name__)

# Giá token (tham khảo, thay đổi theo model)
PRICE_PER_1K_INPUT_TOKENS = 0.00015   # GPT-4o-mini: $0.15/1M input
PRICE_PER_1K_OUTPUT_TOKENS = 0.0006   # GPT-4o-mini: $0.60/1M output

@dataclass
class UsageRecord:
    user_id: str
    input_tokens: int = 0
    output_tokens: int = 0
    request_count: int = 0
    day: str = field(default_factory=lambda: time.strftime("%Y-%m-%d"))

    @property
    def total_cost_usd(self) -> float:
        input_cost = (self.input_tokens / 1000) * PRICE_PER_1K_INPUT_TOKENS
        output_cost = (self.output_tokens / 1000) * PRICE_PER_1K_OUTPUT_TOKENS
        return round(input_cost + output_cost, 6)

class CostGuard:
    def __init__(
        self,
        daily_budget_usd: float = 1.0,       # $1/ngày per user
        global_daily_budget_usd: float = 10.0, # $10/ngày tổng cộng
        warn_at_pct: float = 0.8,              # Cảnh báo khi dùng 80%
    ):
        self.daily_budget_usd = daily_budget_usd
        self.global_daily_budget_usd = global_daily_budget_usd
        self.warn_at_pct = warn_at_pct
        
        # Redis setup
        redis_url = os.getenv("REDIS_URL", "redis://localhost:6379/0")
        try:
            self.r = redis.from_url(redis_url, decode_responses=True)
            self.r.ping()
            self.use_redis = True
        except Exception:
            self.use_redis = False
            self._records = {} # Fallback
            self._global_cost_memory = 0.0
            logger.warning("Redis not available for CostGuard. Using in-memory storage.")

    def _get_user_key(self, user_id: str) -> str:
        today = time.strftime("%Y-%m-%d")
        return f"usage:{user_id}:{today}"

    def _get_global_key(self) -> str:
        today = time.strftime("%Y-%m-%d")
        return f"usage:global:{today}"

    def get_user_cost(self, user_id: str) -> float:
        """Tính tổng cost hiện tại của user."""
        if self.use_redis:
            key = self._get_user_key(user_id)
            data = self.r.hgetall(key)
            if not data: return 0.0
            
            in_t = int(data.get("input_tokens", 0))
            out_t = int(data.get("output_tokens", 0))
            return (in_t / 1000 * PRICE_PER_1K_INPUT_TOKENS + 
                    out_t / 1000 * PRICE_PER_1K_OUTPUT_TOKENS)
        else:
            today = time.strftime("%Y-%m-%d")
            record = self._records.get(user_id)
            if record and record.day == today:
                return record.total_cost_usd
            return 0.0

    def get_global_cost(self) -> float:
        """Tính tổng cost của toàn hệ thống trong ngày."""
        if self.use_redis:
            return float(self.r.get(self._get_global_key()) or 0.0)
        return self._global_cost_memory

    def check_budget(self, user_id: str) -> None:
        """Kiểm tra budget trước khi gọi LLM."""
        user_cost = self.get_user_cost(user_id)
        global_cost = self.get_global_cost()

        # Global check
        if global_cost >= self.global_daily_budget_usd:
            logger.critical(f"GLOBAL BUDGET EXCEEDED: ${global_cost:.4f}")
            raise HTTPException(503, "System budget exceeded. Try tomorrow.")

        # Per-user check
        if user_cost >= self.daily_budget_usd:
            raise HTTPException(402, "User daily budget exceeded.")

        if user_cost >= self.daily_budget_usd * self.warn_at_pct:
            logger.warning(f"User {user_id} near budget limit: ${user_cost:.4f}")

    def record_usage(self, user_id: str, input_tokens: int, output_tokens: int) -> UsageRecord:
        """Ghi nhận usage sau khi gọi LLM."""
        cost = (input_tokens / 1000 * PRICE_PER_1K_INPUT_TOKENS + 
                output_tokens / 1000 * PRICE_PER_1K_OUTPUT_TOKENS)
        
        if self.use_redis:
            key = self._get_user_key(user_id)
            g_key = self._get_global_key()
            
            pipe = self.r.pipeline()
            pipe.hincrby(key, "input_tokens", input_tokens)
            pipe.hincrby(key, "output_tokens", output_tokens)
            pipe.hincrby(key, "request_count", 1)
            pipe.expire(key, 86400 * 2) # Giữ 2 ngày
            
            pipe.incrbyfloat(g_key, cost)
            pipe.expire(g_key, 86400 * 2)
            pipe.execute()
            
            # Trả về record mới nhất
            data = self.r.hgetall(key)
            return UsageRecord(
                user_id=user_id,
                input_tokens=int(data["input_tokens"]),
                output_tokens=int(data["output_tokens"]),
                request_count=int(data["request_count"])
            )
        else:
            today = time.strftime("%Y-%m-%d")
            if user_id not in self._records or self._records[user_id].day != today:
                self._records[user_id] = UsageRecord(user_id=user_id, day=today)
            
            record = self._records[user_id]
            record.input_tokens += input_tokens
            record.output_tokens += output_tokens
            record.request_count += 1
            self._global_cost_memory += cost
            return record

    def get_usage(self, user_id: str) -> dict:
        user_cost = self.get_user_cost(user_id)
        if self.use_redis:
            data = self.r.hgetall(self._get_user_key(user_id))
            req_count = int(data.get("request_count", 0))
            in_t = int(data.get("input_tokens", 0))
            out_t = int(data.get("output_tokens", 0))
        else:
            record = self._records.get(user_id, UsageRecord(user_id=user_id))
            req_count = record.request_count
            in_t = record.input_tokens
            out_t = record.output_tokens

        return {
            "user_id": user_id,
            "requests": req_count,
            "cost_usd": round(user_cost, 6),
            "budget_usd": self.daily_budget_usd,
            "remaining_usd": round(max(0, self.daily_budget_usd - user_cost), 6)
        }

# Singleton
cost_guard = CostGuard(daily_budget_usd=1.0, global_daily_budget_usd=10.0)
