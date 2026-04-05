"""
Feature extraction pipeline for ML training data.
Extracts 100+ contextual features from each query.
"""

import os
import asyncio
from datetime import datetime, timedelta
from typing import Optional, Dict, List, Any
from uuid import UUID
import math

from .schemas import (
    QueryFeatures,
    TemporalFeatures,
    SpatialFeatures,
    BehavioralFeatures,
    ContextualFeatures,
    HistoricalFeatures,
    OutcomeMetrics
)


class FeatureExtractor:
    """
    Extracts 100+ contextual features from query context.
    
    Features are organized into 5 groups:
    - Temporal (20): Time-based patterns
    - Spatial (15): Location-based patterns
    - Behavioral (30): User behavior patterns
    - Contextual (20): Current state (calendar, email, weather, etc.)
    - Historical (15): Learned patterns from past queries
    """
    
    def __init__(self):
        # Home/work coordinates (will be loaded from PIC in production)
        self.home_lat = 29.5  # Houston area
        self.home_lon = -95.3
        self.work_lat = 29.7
        self.work_lon = -95.4
        
        # Geofence zones (will be loaded from PIC)
        self.geofence_zones = {
            "starbucks_morning": (29.6, -95.4, 0.1),  # lat, lon, radius_km
            "gym": (29.55, -95.35, 0.1),
        }
    
    async def extract_features(
        self,
        user_id: str,
        query_text: str,
        query_type: str,
        session_id: UUID,
        conversation_turn: int,
        location: Optional[Dict[str, float]] = None,
        device_type: str = "iphone",
        context: Optional[Dict[str, Any]] = None
    ) -> QueryFeatures:
        """
        Extract complete feature set for a query.
        
        Args:
            user_id: User identifier
            query_text: The query text
            query_type: Classified query type (news, email, calendar, etc.)
            session_id: Current session UUID
            conversation_turn: Turn number in conversation
            location: {"latitude": float, "longitude": float, "city": str, ...}
            device_type: "iphone", "tesla", "dashboard"
            context: Additional context (calendar, email, weather, etc.)
            
        Returns:
            QueryFeatures with 100+ extracted features
        """
        now = datetime.now()
        context = context or {}
        
        # Extract feature groups
        temporal = await self._extract_temporal_features(now, user_id)
        spatial = await self._extract_spatial_features(location)
        behavioral = await self._extract_behavioral_features(
            user_id, session_id, conversation_turn, query_type, device_type
        )
        contextual = await self._extract_contextual_features(context)
        historical = await self._extract_historical_features(user_id, now, location)
        outcome = OutcomeMetrics()  # Will be updated after response
        
        return QueryFeatures(
            user_id=user_id,
            timestamp=now,
            query_text=query_text,
            query_type=query_type,
            temporal=temporal,
            spatial=spatial,
            behavioral=behavioral,
            contextual=contextual,
            historical=historical,
            outcome=outcome
        )
    
    async def _extract_temporal_features(
        self,
        timestamp: datetime,
        user_id: str
    ) -> TemporalFeatures:
        """Extract temporal context features (20 dimensions)."""
        hour = timestamp.hour
        minute = timestamp.minute
        dow = timestamp.weekday()  # 0=Monday
        
        # Time buckets
        is_morning = 6 <= hour < 12
        is_afternoon = 12 <= hour < 18
        is_evening = 18 <= hour < 24
        is_night = 0 <= hour < 6
        
        # Time bucket string
        if is_morning:
            if hour < 8:
                time_bucket = "early_morning"
            elif hour < 10:
                time_bucket = "mid_morning"
            else:
                time_bucket = "late_morning"
        elif is_afternoon:
            if hour < 14:
                time_bucket = "early_afternoon"
            elif hour < 16:
                time_bucket = "mid_afternoon"
            else:
                time_bucket = "late_afternoon"
        elif is_evening:
            if hour < 20:
                time_bucket = "early_evening"
            elif hour < 22:
                time_bucket = "mid_evening"
            else:
                time_bucket = "late_evening"
        else:
            time_bucket = "night"
        
        # Season
        month = timestamp.month
        if month in [3, 4, 5]:
            season = "spring"
        elif month in [6, 7, 8]:
            season = "summer"
        elif month in [9, 10, 11]:
            season = "fall"
        else:
            season = "winter"
        
        # Days until weekend
        if dow < 5:  # Weekday
            days_until_weekend = 4 - dow
        else:  # Weekend
            days_until_weekend = 0
        
        # TODO: Query from database for these
        time_since_last_query_seconds = None
        time_since_wake_seconds = None
        query_frequency_this_hour = 0
        query_frequency_today = 0
        
        return TemporalFeatures(
            hour_of_day=hour,
            minute_of_hour=minute,
            day_of_week=dow,
            day_of_month=timestamp.day,
            week_of_year=timestamp.isocalendar()[1],
            is_weekend=(dow >= 5),
            is_morning=is_morning,
            is_afternoon=is_afternoon,
            is_evening=is_evening,
            is_night=is_night,
            time_since_last_query_seconds=time_since_last_query_seconds,
            time_since_wake_seconds=time_since_wake_seconds,
            is_work_hours=(9 <= hour < 17 and dow < 5),
            season=season,
            is_holiday=False,  # TODO: Check calendar
            days_until_weekend=days_until_weekend,
            query_frequency_this_hour=query_frequency_this_hour,
            query_frequency_today=query_frequency_today,
            time_bucket=time_bucket
        )
    
    async def _extract_spatial_features(
        self,
        location: Optional[Dict[str, float]]
    ) -> SpatialFeatures:
        """Extract spatial context features (15 dimensions)."""
        if not location:
            return SpatialFeatures(
                is_at_home=False,
                is_at_work=False,
                is_in_car=False,
                is_traveling=False
            )
        
        lat = location.get("latitude")
        lon = location.get("longitude")
        
        if not lat or not lon:
            return SpatialFeatures(
                city=location.get("city"),
                state=location.get("state"),
                country=location.get("country", "USA"),
                is_at_home=False,
                is_at_work=False,
                is_in_car=False,
                is_traveling=False
            )
        
        # Calculate distances
        distance_from_home_km = self._haversine_distance(
            lat, lon, self.home_lat, self.home_lon
        )
        distance_from_work_km = self._haversine_distance(
            lat, lon, self.work_lat, self.work_lon
        )
        
        # Location type inference
        is_at_home = distance_from_home_km < 0.1  # Within 100m
        is_at_work = distance_from_work_km < 0.1
        is_traveling = distance_from_home_km > 50  # > 50km from home
        
        # Infer location type
        location_type = None
        if is_at_home:
            location_type = "home"
        elif is_at_work:
            location_type = "work"
        else:
            # Check geofence zones
            for zone_name, (zone_lat, zone_lon, radius_km) in self.geofence_zones.items():
                dist = self._haversine_distance(lat, lon, zone_lat, zone_lon)
                if dist < radius_km:
                    location_type = zone_name
                    break
        
        # Speed-based inference (requires previous location)
        is_in_car = False  # TODO: Calculate from location change rate
        location_change_rate_kmh = None
        
        return SpatialFeatures(
            latitude=lat,
            longitude=lon,
            city=location.get("city"),
            state=location.get("state"),
            country=location.get("country", "USA"),
            location_type=location_type,
            distance_from_home_km=distance_from_home_km,
            distance_from_work_km=distance_from_work_km,
            is_at_home=is_at_home,
            is_at_work=is_at_work,
            is_in_car=is_in_car,
            is_traveling=is_traveling,
            location_change_rate_kmh=location_change_rate_kmh,
            time_at_location_minutes=None,  # TODO: Track
            geofence_zone=location_type if location_type in self.geofence_zones else None
        )
    
    async def _extract_behavioral_features(
        self,
        user_id: str,
        session_id: UUID,
        conversation_turn: int,
        query_type: str,
        device_type: str
    ) -> BehavioralFeatures:
        """Extract behavioral context features (30 dimensions)."""
        # TODO: Query from database for historical patterns
        # For now, return defaults
        
        return BehavioralFeatures(
            session_id=session_id,
            conversation_turn=conversation_turn,
            previous_query_type=None,  # TODO: Get from DB
            last_5_query_types=[],
            query_type_frequency_24h={},
            query_type_frequency_7d={},
            session_duration_minutes=0,
            time_since_last_conversation_minutes=None,
            average_session_length_7d_minutes=None,
            total_queries_today=0,
            total_queries_this_week=0,
            query_success_rate=0.0,
            cache_hit_rate=0.0,
            is_first_query_of_day=False,
            is_first_query_after_wake=False,
            device_type=device_type,
            connection_type=None,
            battery_level=None,
            is_charging=False,
            screen_brightness=None,
            motion_state=None,
            previous_tool_used=None,
            tool_usage_frequency_24h={},
            delegation_frequency=0.0,
            average_response_time_7d_ms=None,
            user_interruption_rate=0.0,
            voice_vs_text_ratio=0.0,
            query_complexity_score=len(query_type.split()) / 20.0,  # Simple heuristic
            follow_up_query_rate=0.0,
            topic_drift_rate=0.0,
            cognitive_load_proxy=0.0
        )
    
    async def _extract_contextual_features(
        self,
        context: Dict[str, Any]
    ) -> ContextualFeatures:
        """Extract contextual state features (20 dimensions)."""
        # Extract from provided context
        calendar = context.get("calendar", {})
        email = context.get("email", {})
        weather = context.get("weather", {})
        traffic = context.get("traffic", {})
        homekit = context.get("homekit", {})
        tesla = context.get("tesla", {})
        
        return ContextualFeatures(
            calendar_next_event_type=calendar.get("next_event_type"),
            calendar_next_event_minutes=calendar.get("next_event_minutes"),
            calendar_is_in_meeting=calendar.get("is_in_meeting", False),
            calendar_meeting_count_today=calendar.get("meeting_count_today", 0),
            email_unread_count=email.get("unread_count", 0),
            email_urgent_count=email.get("urgent_count", 0),
            email_time_since_last_check_minutes=email.get("time_since_last_check_minutes"),
            weather_temp_f=weather.get("temp_f"),
            weather_condition=weather.get("condition"),
            weather_is_extreme=weather.get("is_extreme", False),
            traffic_commute_time_minutes=traffic.get("commute_time_minutes"),
            traffic_is_rush_hour=traffic.get("is_rush_hour", False),
            homekit_lights_on_count=homekit.get("lights_on_count", 0),
            homekit_is_home_occupied=homekit.get("is_home_occupied", False),
            homekit_hvac_state=homekit.get("hvac_state"),
            tesla_is_in_car=tesla.get("is_in_car", False),
            tesla_is_driving=tesla.get("is_driving", False),
            tesla_battery_level=tesla.get("battery_level"),
            tesla_is_charging=tesla.get("is_charging", False),
            tesla_destination_set=tesla.get("destination_set", False)
        )
    
    async def _extract_historical_features(
        self,
        user_id: str,
        timestamp: datetime,
        location: Optional[Dict[str, float]]
    ) -> HistoricalFeatures:
        """Extract historical pattern features (15 dimensions)."""
        # TODO: Query from database for learned patterns
        # For now, return empty patterns
        
        return HistoricalFeatures(
            same_hour_yesterday_query_type=None,
            same_hour_last_week_query_type=None,
            same_location_yesterday_query_type=None,
            same_day_of_week_pattern=None,
            morning_routine_pattern=[],
            evening_routine_pattern=[],
            commute_pattern=[],
            weekend_pattern=[],
            work_pattern=[],
            post_meal_pattern=[],
            pre_sleep_pattern=[],
            wake_up_pattern=[],
            gym_pattern=[],
            travel_pattern=[],
            stress_pattern=[]
        )
    
    def _haversine_distance(
        self,
        lat1: float,
        lon1: float,
        lat2: float,
        lon2: float
    ) -> float:
        """
        Calculate distance between two points on Earth in kilometers.
        Uses Haversine formula.
        """
        R = 6371  # Earth radius in km
        
        lat1_rad = math.radians(lat1)
        lat2_rad = math.radians(lat2)
        dlat = math.radians(lat2 - lat1)
        dlon = math.radians(lon2 - lon1)
        
        a = (
            math.sin(dlat / 2) ** 2 +
            math.cos(lat1_rad) * math.cos(lat2_rad) * math.sin(dlon / 2) ** 2
        )
        c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
        
        return R * c
