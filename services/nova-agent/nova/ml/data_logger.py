"""
Data logger for ML training data collection.
Logs every query with 100+ features to PostgreSQL.
"""

import os
import asyncio
import asyncpg
from typing import Optional, Dict, Any
from uuid import UUID
from datetime import datetime
from loguru import logger

from .schemas import QueryFeatures, QueryHistoryRecord
from .feature_extractor import FeatureExtractor


class QueryDataLogger:
    """
    Logs query data with extracted features to PostgreSQL.
    
    This is Phase 1 of the ML pipeline: passive data collection.
    Every query is logged with 100+ contextual features for training.
    """
    
    def __init__(self):
        self.feature_extractor = FeatureExtractor()
        self.db_pool: Optional[asyncpg.Pool] = None
        self.enabled = os.getenv("NOVA_ML_LOGGING_ENABLED", "true").lower() == "true"
        
        # Database connection
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
        """
        Log a query with extracted features to database.
        
        Args:
            user_id: User identifier
            query_text: The query text
            query_type: Classified query type (news, email, calendar, etc.)
            session_id: Current session UUID
            conversation_turn: Turn number in conversation
            location: {"latitude": float, "longitude": float, "city": str, ...}
            device_type: "iphone", "tesla", "dashboard"
            context: Additional context (calendar, email, weather, etc.)
            response_text: The response text (optional, can be updated later)
            
        Returns:
            UUID of inserted record, or None if logging failed
        """
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
            
            # Add response text
            features.response_text = response_text
            
            # Convert to database record
            record = self._features_to_record(features)
            
            # Insert into database
            record_id = await self._insert_record(record)
            
            logger.debug(f"[ML Logger] Logged query: {query_type} (id={record_id})")
            return record_id
            
        except Exception as e:
            logger.error(f"[ML Logger] Failed to log query: {e}")
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
        """
        Update outcome metrics for a logged query.
        
        This is called after the response is delivered to update training labels.
        """
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
    
    def _features_to_record(self, features: QueryFeatures) -> QueryHistoryRecord:
        """Convert QueryFeatures to QueryHistoryRecord for database insertion."""
        return QueryHistoryRecord(
            user_id=features.user_id,
            timestamp=features.timestamp,
            query_text=features.query_text,
            query_type=features.query_type,
            response_text=features.response_text,
            
            # Temporal
            hour_of_day=features.temporal.hour_of_day,
            minute_of_hour=features.temporal.minute_of_hour,
            day_of_week=features.temporal.day_of_week,
            day_of_month=features.temporal.day_of_month,
            week_of_year=features.temporal.week_of_year,
            is_weekend=features.temporal.is_weekend,
            is_morning=features.temporal.is_morning,
            is_afternoon=features.temporal.is_afternoon,
            is_evening=features.temporal.is_evening,
            is_night=features.temporal.is_night,
            time_since_last_query_seconds=features.temporal.time_since_last_query_seconds,
            time_since_wake_seconds=features.temporal.time_since_wake_seconds,
            is_work_hours=features.temporal.is_work_hours,
            season=features.temporal.season,
            is_holiday=features.temporal.is_holiday,
            days_until_weekend=features.temporal.days_until_weekend,
            query_frequency_this_hour=features.temporal.query_frequency_this_hour,
            query_frequency_today=features.temporal.query_frequency_today,
            time_bucket=features.temporal.time_bucket,
            
            # Spatial
            latitude=features.spatial.latitude,
            longitude=features.spatial.longitude,
            city=features.spatial.city,
            state=features.spatial.state,
            country=features.spatial.country,
            location_type=features.spatial.location_type,
            distance_from_home_km=features.spatial.distance_from_home_km,
            distance_from_work_km=features.spatial.distance_from_work_km,
            is_at_home=features.spatial.is_at_home,
            is_at_work=features.spatial.is_at_work,
            is_in_car=features.spatial.is_in_car,
            is_traveling=features.spatial.is_traveling,
            location_change_rate_kmh=features.spatial.location_change_rate_kmh,
            time_at_location_minutes=features.spatial.time_at_location_minutes,
            geofence_zone=features.spatial.geofence_zone,
            
            # Behavioral
            session_id=features.behavioral.session_id,
            conversation_turn=features.behavioral.conversation_turn,
            previous_query_type=features.behavioral.previous_query_type,
            last_5_query_types={"items": features.behavioral.last_5_query_types} if features.behavioral.last_5_query_types else {},
            query_type_frequency_24h=features.behavioral.query_type_frequency_24h,
            query_type_frequency_7d=features.behavioral.query_type_frequency_7d,
            session_duration_minutes=features.behavioral.session_duration_minutes,
            time_since_last_conversation_minutes=features.behavioral.time_since_last_conversation_minutes,
            average_session_length_7d_minutes=features.behavioral.average_session_length_7d_minutes,
            total_queries_today=features.behavioral.total_queries_today,
            total_queries_this_week=features.behavioral.total_queries_this_week,
            query_success_rate=features.behavioral.query_success_rate,
            cache_hit_rate=features.behavioral.cache_hit_rate,
            is_first_query_of_day=features.behavioral.is_first_query_of_day,
            is_first_query_after_wake=features.behavioral.is_first_query_after_wake,
            device_type=features.behavioral.device_type,
            connection_type=features.behavioral.connection_type,
            battery_level=features.behavioral.battery_level,
            is_charging=features.behavioral.is_charging,
            screen_brightness=features.behavioral.screen_brightness,
            motion_state=features.behavioral.motion_state,
            previous_tool_used=features.behavioral.previous_tool_used,
            tool_usage_frequency_24h=features.behavioral.tool_usage_frequency_24h,
            delegation_frequency=features.behavioral.delegation_frequency,
            average_response_time_7d_ms=features.behavioral.average_response_time_7d_ms,
            user_interruption_rate=features.behavioral.user_interruption_rate,
            voice_vs_text_ratio=features.behavioral.voice_vs_text_ratio,
            query_complexity_score=features.behavioral.query_complexity_score,
            follow_up_query_rate=features.behavioral.follow_up_query_rate,
            topic_drift_rate=features.behavioral.topic_drift_rate,
            cognitive_load_proxy=features.behavioral.cognitive_load_proxy,
            
            # Contextual
            calendar_next_event_type=features.contextual.calendar_next_event_type,
            calendar_next_event_minutes=features.contextual.calendar_next_event_minutes,
            calendar_is_in_meeting=features.contextual.calendar_is_in_meeting,
            calendar_meeting_count_today=features.contextual.calendar_meeting_count_today,
            email_unread_count=features.contextual.email_unread_count,
            email_urgent_count=features.contextual.email_urgent_count,
            email_time_since_last_check_minutes=features.contextual.email_time_since_last_check_minutes,
            weather_temp_f=features.contextual.weather_temp_f,
            weather_condition=features.contextual.weather_condition,
            weather_is_extreme=features.contextual.weather_is_extreme,
            traffic_commute_time_minutes=features.contextual.traffic_commute_time_minutes,
            traffic_is_rush_hour=features.contextual.traffic_is_rush_hour,
            homekit_lights_on_count=features.contextual.homekit_lights_on_count,
            homekit_is_home_occupied=features.contextual.homekit_is_home_occupied,
            homekit_hvac_state=features.contextual.homekit_hvac_state,
            tesla_is_in_car=features.contextual.tesla_is_in_car,
            tesla_is_driving=features.contextual.tesla_is_driving,
            tesla_battery_level=features.contextual.tesla_battery_level,
            tesla_is_charging=features.contextual.tesla_is_charging,
            tesla_destination_set=features.contextual.tesla_destination_set,
            
            # Historical
            same_hour_yesterday_query_type=features.historical.same_hour_yesterday_query_type,
            same_hour_last_week_query_type=features.historical.same_hour_last_week_query_type,
            same_location_yesterday_query_type=features.historical.same_location_yesterday_query_type,
            same_day_of_week_pattern=features.historical.same_day_of_week_pattern,
            morning_routine_pattern={"items": features.historical.morning_routine_pattern} if features.historical.morning_routine_pattern else {},
            evening_routine_pattern={"items": features.historical.evening_routine_pattern} if features.historical.evening_routine_pattern else {},
            commute_pattern={"items": features.historical.commute_pattern} if features.historical.commute_pattern else {},
            weekend_pattern={"items": features.historical.weekend_pattern} if features.historical.weekend_pattern else {},
            work_pattern={"items": features.historical.work_pattern} if features.historical.work_pattern else {},
            post_meal_pattern={"items": features.historical.post_meal_pattern} if features.historical.post_meal_pattern else {},
            pre_sleep_pattern={"items": features.historical.pre_sleep_pattern} if features.historical.pre_sleep_pattern else {},
            wake_up_pattern={"items": features.historical.wake_up_pattern} if features.historical.wake_up_pattern else {},
            gym_pattern={"items": features.historical.gym_pattern} if features.historical.gym_pattern else {},
            travel_pattern={"items": features.historical.travel_pattern} if features.historical.travel_pattern else {},
            stress_pattern={"items": features.historical.stress_pattern} if features.historical.stress_pattern else {},
            
            # Outcome
            was_useful=features.outcome.was_useful,
            response_time_ms=features.outcome.response_time_ms,
            was_interrupted=features.outcome.was_interrupted,
            cache_hit=features.outcome.cache_hit,
            user_satisfaction_score=features.outcome.user_satisfaction_score,
            
            # Flexible
            features=features.features
        )
    
    async def _insert_features_direct(self, features: QueryFeatures) -> UUID:
        """Insert features directly without Pydantic validation."""
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
        
        # Convert list fields to JSONB-compatible dicts
        last_5_types = {"items": features.behavioral.last_5_query_types} if features.behavioral.last_5_query_types else {}
        morning_routine = {"items": features.historical.morning_routine_pattern} if features.historical.morning_routine_pattern else {}
        evening_routine = {"items": features.historical.evening_routine_pattern} if features.historical.evening_routine_pattern else {}
        commute = {"items": features.historical.commute_pattern} if features.historical.commute_pattern else {}
        weekend = {"items": features.historical.weekend_pattern} if features.historical.weekend_pattern else {}
        work = {"items": features.historical.work_pattern} if features.historical.work_pattern else {}
        post_meal = {"items": features.historical.post_meal_pattern} if features.historical.post_meal_pattern else {}
        pre_sleep = {"items": features.historical.pre_sleep_pattern} if features.historical.pre_sleep_pattern else {}
        wake_up = {"items": features.historical.wake_up_pattern} if features.historical.wake_up_pattern else {}
        gym = {"items": features.historical.gym_pattern} if features.historical.gym_pattern else {}
        travel = {"items": features.historical.travel_pattern} if features.historical.travel_pattern else {}
        stress = {"items": features.historical.stress_pattern} if features.historical.stress_pattern else {}
        
        async with self.db_pool.acquire() as conn:
            row = await conn.fetchrow(
                query,
                features.user_id, features.timestamp, features.query_text, features.query_type,
                features.response_text,
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
                features.behavioral.session_id, features.behavioral.conversation_turn,
                features.behavioral.previous_query_type, last_5_types,
                features.behavioral.query_type_frequency_24h, features.behavioral.query_type_frequency_7d,
                features.behavioral.session_duration_minutes, features.behavioral.time_since_last_conversation_minutes,
                features.behavioral.average_session_length_7d_minutes, features.behavioral.total_queries_today,
                features.behavioral.total_queries_this_week, features.behavioral.query_success_rate,
                features.behavioral.cache_hit_rate, features.behavioral.is_first_query_of_day,
                features.behavioral.is_first_query_after_wake, features.behavioral.device_type,
                features.behavioral.connection_type, features.behavioral.battery_level,
                features.behavioral.is_charging, features.behavioral.screen_brightness,
                features.behavioral.motion_state, features.behavioral.previous_tool_used,
                features.behavioral.tool_usage_frequency_24h, features.behavioral.delegation_frequency,
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
                features.outcome.was_useful, features.outcome.response_time_ms, features.outcome.was_interrupted,
                features.outcome.cache_hit, features.outcome.user_satisfaction_score, features.features
            )
            return row['id']
    
    async def _insert_record(self, record: QueryHistoryRecord) -> UUID:
        """Insert a record into the database and return its ID."""
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
                record.user_id, record.timestamp, record.query_text, record.query_type,
                record.response_text,
                record.hour_of_day, record.minute_of_hour, record.day_of_week,
                record.day_of_month, record.week_of_year, record.is_weekend,
                record.is_morning, record.is_afternoon, record.is_evening, record.is_night,
                record.time_since_last_query_seconds, record.time_since_wake_seconds,
                record.is_work_hours, record.season, record.is_holiday,
                record.days_until_weekend, record.query_frequency_this_hour,
                record.query_frequency_today, record.time_bucket,
                record.latitude, record.longitude, record.city, record.state, record.country,
                record.location_type, record.distance_from_home_km, record.distance_from_work_km,
                record.is_at_home, record.is_at_work, record.is_in_car, record.is_traveling,
                record.location_change_rate_kmh, record.time_at_location_minutes,
                record.geofence_zone,
                record.session_id, record.conversation_turn, record.previous_query_type,
                record.last_5_query_types, record.query_type_frequency_24h,
                record.query_type_frequency_7d, record.session_duration_minutes,
                record.time_since_last_conversation_minutes, record.average_session_length_7d_minutes,
                record.total_queries_today, record.total_queries_this_week,
                record.query_success_rate, record.cache_hit_rate, record.is_first_query_of_day,
                record.is_first_query_after_wake, record.device_type, record.connection_type,
                record.battery_level, record.is_charging, record.screen_brightness,
                record.motion_state, record.previous_tool_used, record.tool_usage_frequency_24h,
                record.delegation_frequency, record.average_response_time_7d_ms,
                record.user_interruption_rate, record.voice_vs_text_ratio,
                record.query_complexity_score, record.follow_up_query_rate,
                record.topic_drift_rate, record.cognitive_load_proxy,
                record.calendar_next_event_type, record.calendar_next_event_minutes,
                record.calendar_is_in_meeting, record.calendar_meeting_count_today,
                record.email_unread_count, record.email_urgent_count,
                record.email_time_since_last_check_minutes, record.weather_temp_f,
                record.weather_condition, record.weather_is_extreme,
                record.traffic_commute_time_minutes, record.traffic_is_rush_hour,
                record.homekit_lights_on_count, record.homekit_is_home_occupied,
                record.homekit_hvac_state, record.tesla_is_in_car, record.tesla_is_driving,
                record.tesla_battery_level, record.tesla_is_charging, record.tesla_destination_set,
                record.same_hour_yesterday_query_type, record.same_hour_last_week_query_type,
                record.same_location_yesterday_query_type, record.same_day_of_week_pattern,
                record.morning_routine_pattern, record.evening_routine_pattern,
                record.commute_pattern, record.weekend_pattern, record.work_pattern,
                record.post_meal_pattern, record.pre_sleep_pattern, record.wake_up_pattern,
                record.gym_pattern, record.travel_pattern, record.stress_pattern,
                record.was_useful, record.response_time_ms, record.was_interrupted,
                record.cache_hit, record.user_satisfaction_score, record.features
            )
            return row['id']


# Global instance
_logger_instance: Optional[QueryDataLogger] = None


async def get_logger() -> QueryDataLogger:
    """Get or create the global QueryDataLogger instance."""
    global _logger_instance
    if _logger_instance is None:
        _logger_instance = QueryDataLogger()
        await _logger_instance.initialize()
    return _logger_instance
