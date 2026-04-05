"""
Simplified ML data logger - Phase 1 implementation.
Logs queries with extracted features directly to PostgreSQL.
"""

import os
import json
import asyncio
import asyncpg
from typing import Optional, Dict, Any
from uuid import UUID, uuid4
from datetime import datetime
from loguru import logger

from .feature_extractor import FeatureExtractor


class SimpleQueryLogger:
    """Simplified query logger that bypasses Pydantic for direct DB insertion."""
    
    def __init__(self):
        self.feature_extractor = FeatureExtractor()
        self.db_pool: Optional[asyncpg.Pool] = None
        self.enabled = os.getenv("NOVA_ML_LOGGING_ENABLED", "true").lower() == "true"
        
        self.db_host = os.getenv("POSTGRES_HOST", "localhost")
        self.db_port = int(os.getenv("POSTGRES_PORT", "5432"))
        self.db_name = os.getenv("POSTGRES_DB", "ecosystem_unified")
        self.db_user = os.getenv("POSTGRES_USER", "postgres")
        self.db_password = os.getenv("POSTGRES_PASSWORD", "")
    
    async def initialize(self):
        """Initialize database connection pool."""
        if not self.enabled:
            logger.info("[ML Logger] Disabled via NOVA_ML_LOGGING_ENABLED=false")
            return
        
        try:
            self.db_pool = await asyncpg.create_pool(
                host=self.db_host,
                port=self.db_port,
                database=self.db_name,
                user=self.db_user,
                password=self.db_password,
                min_size=2,
                max_size=10
            )
            logger.info(f"[ML Logger] Connected to PostgreSQL: {self.db_name}")
        except Exception as e:
            logger.error(f"[ML Logger] Failed to connect to PostgreSQL: {e}")
            self.enabled = False
    
    async def close(self):
        """Close database connection pool."""
        if self.db_pool:
            await self.db_pool.close()
            logger.info("[ML Logger] Database connection closed")
    
    async def log_query(
        self,
        user_id: str,
        query_text: str,
        query_type: str,
        session_id: UUID,
        conversation_turn: int,
        location: Optional[Dict[str, float]] = None,
        device_type: str = "iphone",
        context: Optional[Dict[str, Any]] = None,
        response_text: Optional[str] = None
    ) -> Optional[UUID]:
        """Log a query with extracted features."""
        if not self.enabled or not self.db_pool:
            return None
        
        try:
            # Extract features
            features = await self.feature_extractor.extract_features(
                user_id=user_id,
                query_text=query_text,
                query_type=query_type,
                session_id=session_id,
                conversation_turn=conversation_turn,
                location=location,
                device_type=device_type,
                context=context
            )
            
            # Convert ALL dict/list fields to JSON strings for asyncpg JSONB compatibility
            last_5_types = json.dumps({"items": features.behavioral.last_5_query_types} if features.behavioral.last_5_query_types else {})
            morning_routine = json.dumps({"items": features.historical.morning_routine_pattern} if features.historical.morning_routine_pattern else {})
            evening_routine = json.dumps({"items": features.historical.evening_routine_pattern} if features.historical.evening_routine_pattern else {})
            commute = json.dumps({"items": features.historical.commute_pattern} if features.historical.commute_pattern else {})
            weekend = json.dumps({"items": features.historical.weekend_pattern} if features.historical.weekend_pattern else {})
            work = json.dumps({"items": features.historical.work_pattern} if features.historical.work_pattern else {})
            post_meal = json.dumps({"items": features.historical.post_meal_pattern} if features.historical.post_meal_pattern else {})
            pre_sleep = json.dumps({"items": features.historical.pre_sleep_pattern} if features.historical.pre_sleep_pattern else {})
            wake_up = json.dumps({"items": features.historical.wake_up_pattern} if features.historical.wake_up_pattern else {})
            gym = json.dumps({"items": features.historical.gym_pattern} if features.historical.gym_pattern else {})
            travel = json.dumps({"items": features.historical.travel_pattern} if features.historical.travel_pattern else {})
            stress = json.dumps({"items": features.historical.stress_pattern} if features.historical.stress_pattern else {})
            
            # Insert directly
            query = """
                INSERT INTO query_history (
                    user_id, timestamp, query_text, query_type, response_text,
                    hour_of_day, minute_of_hour, day_of_week, day_of_month, week_of_year,
                    is_weekend, is_morning, is_afternoon, is_evening, is_night,
                    time_since_last_query_seconds, time_since_wake_seconds, is_work_hours,
                    season, is_holiday, days_until_weekend, query_frequency_this_hour,
                    query_frequency_today, time_bucket,
                    latitude, longitude, city, state, country, location_type,
                    distance_from_home_km, distance_from_work_km, is_at_home, is_at_work,
                    is_in_car, is_traveling, location_change_rate_kmh, time_at_location_minutes,
                    geofence_zone,
                    session_id, conversation_turn, previous_query_type, last_5_query_types,
                    query_type_frequency_24h, query_type_frequency_7d, session_duration_minutes,
                    time_since_last_conversation_minutes, average_session_length_7d_minutes,
                    total_queries_today, total_queries_this_week, query_success_rate,
                    cache_hit_rate, is_first_query_of_day, is_first_query_after_wake,
                    device_type, connection_type, battery_level, is_charging, screen_brightness,
                    motion_state, previous_tool_used, tool_usage_frequency_24h,
                    delegation_frequency, average_response_time_7d_ms, user_interruption_rate,
                    voice_vs_text_ratio, query_complexity_score, follow_up_query_rate,
                    topic_drift_rate, cognitive_load_proxy,
                    calendar_next_event_type, calendar_next_event_minutes, calendar_is_in_meeting,
                    calendar_meeting_count_today, email_unread_count, email_urgent_count,
                    email_time_since_last_check_minutes, weather_temp_f, weather_condition,
                    weather_is_extreme, traffic_commute_time_minutes, traffic_is_rush_hour,
                    homekit_lights_on_count, homekit_is_home_occupied, homekit_hvac_state,
                    tesla_is_in_car, tesla_is_driving, tesla_battery_level, tesla_is_charging,
                    tesla_destination_set,
                    same_hour_yesterday_query_type, same_hour_last_week_query_type,
                    same_location_yesterday_query_type, same_day_of_week_pattern,
                    morning_routine_pattern, evening_routine_pattern, commute_pattern,
                    weekend_pattern, work_pattern, post_meal_pattern, pre_sleep_pattern,
                    wake_up_pattern, gym_pattern, travel_pattern, stress_pattern,
                    was_useful, response_time_ms, was_interrupted, cache_hit,
                    user_satisfaction_score, features
                ) VALUES (
                    $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15,
                    $16, $17, $18, $19, $20, $21, $22, $23, $24, $25, $26, $27, $28,
                    $29, $30, $31, $32, $33, $34, $35, $36, $37, $38, $39, $40, $41,
                    $42, $43, $44, $45, $46, $47, $48, $49, $50, $51, $52, $53, $54,
                    $55, $56, $57, $58, $59, $60, $61, $62, $63, $64, $65, $66, $67,
                    $68, $69, $70, $71, $72, $73, $74, $75, $76, $77, $78, $79, $80,
                    $81, $82, $83, $84, $85, $86, $87, $88, $89, $90, $91, $92, $93,
                    $94, $95, $96, $97, $98, $99, $100, $101, $102, $103, $104, $105,
                    $106, $107, $108, $109, $110, $111
                ) RETURNING id
            """
            
            async with self.db_pool.acquire() as conn:
                row = await conn.fetchrow(
                    query,
                    user_id, datetime.now(), query_text, query_type, response_text,
                    features.temporal.hour_of_day, features.temporal.minute_of_hour, features.temporal.day_of_week,
                    features.temporal.day_of_month, features.temporal.week_of_year, features.temporal.is_weekend,
                    features.temporal.is_morning, features.temporal.is_afternoon, features.temporal.is_evening,
                    features.temporal.is_night, features.temporal.time_since_last_query_seconds,
                    features.temporal.time_since_wake_seconds, features.temporal.is_work_hours,
                    features.temporal.season, features.temporal.is_holiday, features.temporal.days_until_weekend,
                    features.temporal.query_frequency_this_hour, features.temporal.query_frequency_today,
                    features.temporal.time_bucket,
                    features.spatial.latitude, features.spatial.longitude, features.spatial.city,
                    features.spatial.state, features.spatial.country, features.spatial.location_type,
                    features.spatial.distance_from_home_km, features.spatial.distance_from_work_km,
                    features.spatial.is_at_home, features.spatial.is_at_work, features.spatial.is_in_car,
                    features.spatial.is_traveling, features.spatial.location_change_rate_kmh,
                    features.spatial.time_at_location_minutes, features.spatial.geofence_zone,
                    session_id, conversation_turn, features.behavioral.previous_query_type, last_5_types,
                    json.dumps(features.behavioral.query_type_frequency_24h or {}), json.dumps(features.behavioral.query_type_frequency_7d or {}),
                    features.behavioral.session_duration_minutes, features.behavioral.time_since_last_conversation_minutes,
                    features.behavioral.average_session_length_7d_minutes, features.behavioral.total_queries_today,
                    features.behavioral.total_queries_this_week, features.behavioral.query_success_rate,
                    features.behavioral.cache_hit_rate, features.behavioral.is_first_query_of_day,
                    features.behavioral.is_first_query_after_wake, device_type,
                    features.behavioral.connection_type, features.behavioral.battery_level,
                    features.behavioral.is_charging, features.behavioral.screen_brightness,
                    features.behavioral.motion_state, features.behavioral.previous_tool_used,
                    json.dumps(features.behavioral.tool_usage_frequency_24h or {}), features.behavioral.delegation_frequency,
                    features.behavioral.average_response_time_7d_ms, features.behavioral.user_interruption_rate,
                    features.behavioral.voice_vs_text_ratio, features.behavioral.query_complexity_score,
                    features.behavioral.follow_up_query_rate, features.behavioral.topic_drift_rate,
                    features.behavioral.cognitive_load_proxy,
                    features.contextual.calendar_next_event_type, features.contextual.calendar_next_event_minutes,
                    features.contextual.calendar_is_in_meeting, features.contextual.calendar_meeting_count_today,
                    features.contextual.email_unread_count, features.contextual.email_urgent_count,
                    features.contextual.email_time_since_last_check_minutes, features.contextual.weather_temp_f,
                    features.contextual.weather_condition, features.contextual.weather_is_extreme,
                    features.contextual.traffic_commute_time_minutes, features.contextual.traffic_is_rush_hour,
                    features.contextual.homekit_lights_on_count, features.contextual.homekit_is_home_occupied,
                    features.contextual.homekit_hvac_state, features.contextual.tesla_is_in_car,
                    features.contextual.tesla_is_driving, features.contextual.tesla_battery_level,
                    features.contextual.tesla_is_charging, features.contextual.tesla_destination_set,
                    features.historical.same_hour_yesterday_query_type, features.historical.same_hour_last_week_query_type,
                    features.historical.same_location_yesterday_query_type, features.historical.same_day_of_week_pattern,
                    morning_routine, evening_routine, commute, weekend, work, post_meal, pre_sleep,
                    wake_up, gym, travel, stress,
                    None, None, False, False, None, json.dumps({})
                )
                record_id = row['id']
            
            logger.debug(f"[ML Logger] Logged query: {query_type} (id={record_id})")
            return record_id
            
        except Exception as e:
            logger.error(f"[ML Logger] Failed to log query: {e}")
            import traceback
            traceback.print_exc()
            return None
    
    async def update_outcome(
        self,
        record_id: UUID,
        was_useful: Optional[bool] = None,
        response_time_ms: Optional[int] = None,
        was_interrupted: Optional[bool] = None,
        cache_hit: Optional[bool] = None,
        user_satisfaction_score: Optional[int] = None
    ):
        """Update outcome metrics for a logged query."""
        if not self.enabled or not self.db_pool:
            return
        
        try:
            updates = []
            values = []
            param_idx = 1
            
            if was_useful is not None:
                updates.append(f"was_useful = ${param_idx}")
                values.append(was_useful)
                param_idx += 1
            
            if response_time_ms is not None:
                updates.append(f"response_time_ms = ${param_idx}")
                values.append(response_time_ms)
                param_idx += 1
            
            if was_interrupted is not None:
                updates.append(f"was_interrupted = ${param_idx}")
                values.append(was_interrupted)
                param_idx += 1
            
            if cache_hit is not None:
                updates.append(f"cache_hit = ${param_idx}")
                values.append(cache_hit)
                param_idx += 1
            
            if user_satisfaction_score is not None:
                updates.append(f"user_satisfaction_score = ${param_idx}")
                values.append(user_satisfaction_score)
                param_idx += 1
            
            if not updates:
                return
            
            values.append(record_id)
            query = f"""
                UPDATE query_history
                SET {', '.join(updates)}
                WHERE id = ${param_idx}
            """
            
            async with self.db_pool.acquire() as conn:
                await conn.execute(query, *values)
            
            logger.debug(f"[ML Logger] Updated outcome for record {record_id}")
            
        except Exception as e:
            logger.error(f"[ML Logger] Failed to update outcome: {e}")
