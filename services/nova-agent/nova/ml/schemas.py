"""
Pydantic schemas for ML feature extraction and query logging.
"""

from pydantic import BaseModel, Field
from typing import Optional, List, Dict, Any
from datetime import datetime
from uuid import UUID


class TemporalFeatures(BaseModel):
    """Temporal context features (20 dimensions)."""
    hour_of_day: int = Field(..., ge=0, le=23)
    minute_of_hour: int = Field(..., ge=0, le=59)
    day_of_week: int = Field(..., ge=0, le=6, description="0=Monday")
    day_of_month: int = Field(..., ge=1, le=31)
    week_of_year: int = Field(..., ge=1, le=52)
    is_weekend: bool
    is_morning: bool = Field(..., description="6-12")
    is_afternoon: bool = Field(..., description="12-18")
    is_evening: bool = Field(..., description="18-24")
    is_night: bool = Field(..., description="0-6")
    time_since_last_query_seconds: Optional[int] = None
    time_since_wake_seconds: Optional[int] = None
    is_work_hours: bool = Field(..., description="9-17 on weekdays")
    season: str = Field(..., description="spring, summer, fall, winter")
    is_holiday: bool = False
    days_until_weekend: int = Field(..., ge=0, le=6)
    query_frequency_this_hour: int = 0
    query_frequency_today: int = 0
    time_bucket: str = Field(..., description="early_morning, mid_morning, etc.")


class SpatialFeatures(BaseModel):
    """Spatial context features (15 dimensions)."""
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    city: Optional[str] = None
    state: Optional[str] = None
    country: Optional[str] = "USA"
    location_type: Optional[str] = Field(None, description="home, work, car, gym, coffee_shop")
    distance_from_home_km: Optional[float] = None
    distance_from_work_km: Optional[float] = None
    is_at_home: bool = False
    is_at_work: bool = False
    is_in_car: bool = False
    is_traveling: bool = Field(False, description="> 50km from home")
    location_change_rate_kmh: Optional[float] = None
    time_at_location_minutes: Optional[int] = None
    geofence_zone: Optional[str] = None


class BehavioralFeatures(BaseModel):
    """Behavioral context features (30 dimensions)."""
    session_id: UUID
    conversation_turn: int = 0
    previous_query_type: Optional[str] = None
    last_5_query_types: List[str] = Field(default_factory=list)
    query_type_frequency_24h: Dict[str, int] = Field(default_factory=dict)
    query_type_frequency_7d: Dict[str, int] = Field(default_factory=dict)
    session_duration_minutes: int = 0
    time_since_last_conversation_minutes: Optional[int] = None
    average_session_length_7d_minutes: Optional[int] = None
    total_queries_today: int = 0
    total_queries_this_week: int = 0
    query_success_rate: float = Field(0.0, ge=0.0, le=1.0)
    cache_hit_rate: float = Field(0.0, ge=0.0, le=1.0)
    is_first_query_of_day: bool = False
    is_first_query_after_wake: bool = False
    device_type: str = Field("iphone", description="iphone, tesla, dashboard")
    connection_type: Optional[str] = Field(None, description="wifi, cellular, bluetooth")
    battery_level: Optional[int] = Field(None, ge=0, le=100)
    is_charging: bool = False
    screen_brightness: Optional[int] = Field(None, ge=0, le=100)
    motion_state: Optional[str] = Field(None, description="stationary, walking, driving")
    previous_tool_used: Optional[str] = None
    tool_usage_frequency_24h: Dict[str, int] = Field(default_factory=dict)
    delegation_frequency: float = Field(0.0, ge=0.0, le=1.0)
    average_response_time_7d_ms: Optional[int] = None
    user_interruption_rate: float = Field(0.0, ge=0.0, le=1.0)
    voice_vs_text_ratio: float = Field(0.0, ge=0.0, le=1.0)
    query_complexity_score: float = Field(0.0, ge=0.0, le=1.0)
    follow_up_query_rate: float = Field(0.0, ge=0.0, le=1.0)
    topic_drift_rate: float = Field(0.0, ge=0.0, le=1.0)
    cognitive_load_proxy: float = Field(0.0, ge=0.0, le=1.0)


class ContextualFeatures(BaseModel):
    """Contextual state features (20 dimensions)."""
    calendar_next_event_type: Optional[str] = None
    calendar_next_event_minutes: Optional[int] = None
    calendar_is_in_meeting: bool = False
    calendar_meeting_count_today: int = 0
    email_unread_count: int = 0
    email_urgent_count: int = 0
    email_time_since_last_check_minutes: Optional[int] = None
    weather_temp_f: Optional[float] = None
    weather_condition: Optional[str] = None
    weather_is_extreme: bool = False
    traffic_commute_time_minutes: Optional[int] = None
    traffic_is_rush_hour: bool = False
    homekit_lights_on_count: int = 0
    homekit_is_home_occupied: bool = False
    homekit_hvac_state: Optional[str] = None
    tesla_is_in_car: bool = False
    tesla_is_driving: bool = False
    tesla_battery_level: Optional[int] = Field(None, ge=0, le=100)
    tesla_is_charging: bool = False
    tesla_destination_set: bool = False


class HistoricalFeatures(BaseModel):
    """Historical pattern features (15 dimensions)."""
    same_hour_yesterday_query_type: Optional[str] = None
    same_hour_last_week_query_type: Optional[str] = None
    same_location_yesterday_query_type: Optional[str] = None
    same_day_of_week_pattern: Optional[str] = None
    morning_routine_pattern: List[str] = Field(default_factory=list)
    evening_routine_pattern: List[str] = Field(default_factory=list)
    commute_pattern: List[str] = Field(default_factory=list)
    weekend_pattern: List[str] = Field(default_factory=list)
    work_pattern: List[str] = Field(default_factory=list)
    post_meal_pattern: List[str] = Field(default_factory=list)
    pre_sleep_pattern: List[str] = Field(default_factory=list)
    wake_up_pattern: List[str] = Field(default_factory=list)
    gym_pattern: List[str] = Field(default_factory=list)
    travel_pattern: List[str] = Field(default_factory=list)
    stress_pattern: List[str] = Field(default_factory=list)


class OutcomeMetrics(BaseModel):
    """Outcome metrics for training labels."""
    was_useful: Optional[bool] = None
    response_time_ms: Optional[int] = None
    was_interrupted: bool = False
    cache_hit: bool = False
    user_satisfaction_score: Optional[int] = Field(None, ge=1, le=5)


class QueryFeatures(BaseModel):
    """Complete feature set for a single query (100+ dimensions)."""
    # Core query info
    user_id: str
    timestamp: datetime
    query_text: str
    query_type: str = Field(..., description="news, email, calendar, weather, homelab, tesla, etc.")
    response_text: Optional[str] = None
    
    # Feature groups
    temporal: TemporalFeatures
    spatial: SpatialFeatures
    behavioral: BehavioralFeatures
    contextual: ContextualFeatures
    historical: HistoricalFeatures
    outcome: OutcomeMetrics
    
    # Flexible features
    features: Dict[str, Any] = Field(default_factory=dict)
    
    class Config:
        json_encoders = {
            datetime: lambda v: v.isoformat(),
            UUID: lambda v: str(v)
        }


class QueryHistoryRecord(BaseModel):
    """Database record for query_history table."""
    id: Optional[UUID] = None
    user_id: str
    timestamp: datetime
    
    # Query details
    query_text: str
    query_type: str
    query_embedding: Optional[List[float]] = None
    response_text: Optional[str] = None
    
    # Temporal (20)
    hour_of_day: int
    minute_of_hour: int
    day_of_week: int
    day_of_month: int
    week_of_year: int
    is_weekend: bool
    is_morning: bool
    is_afternoon: bool
    is_evening: bool
    is_night: bool
    time_since_last_query_seconds: Optional[int] = None
    time_since_wake_seconds: Optional[int] = None
    is_work_hours: bool
    season: str
    is_holiday: bool
    days_until_weekend: int
    query_frequency_this_hour: int
    query_frequency_today: int
    time_bucket: str
    
    # Spatial (15)
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    city: Optional[str] = None
    state: Optional[str] = None
    country: Optional[str] = None
    location_type: Optional[str] = None
    distance_from_home_km: Optional[float] = None
    distance_from_work_km: Optional[float] = None
    is_at_home: bool
    is_at_work: bool
    is_in_car: bool
    is_traveling: bool
    location_change_rate_kmh: Optional[float] = None
    time_at_location_minutes: Optional[int] = None
    geofence_zone: Optional[str] = None
    
    # Behavioral (30)
    session_id: UUID
    conversation_turn: int
    previous_query_type: Optional[str] = None
    last_5_query_types: Optional[Dict] = None
    query_type_frequency_24h: Optional[Dict] = None
    query_type_frequency_7d: Optional[Dict] = None
    session_duration_minutes: int
    time_since_last_conversation_minutes: Optional[int] = None
    average_session_length_7d_minutes: Optional[int] = None
    total_queries_today: int
    total_queries_this_week: int
    query_success_rate: float
    cache_hit_rate: float
    is_first_query_of_day: bool
    is_first_query_after_wake: bool
    device_type: str
    connection_type: Optional[str] = None
    battery_level: Optional[int] = None
    is_charging: bool
    screen_brightness: Optional[int] = None
    motion_state: Optional[str] = None
    previous_tool_used: Optional[str] = None
    tool_usage_frequency_24h: Optional[Dict] = None
    delegation_frequency: float
    average_response_time_7d_ms: Optional[int] = None
    user_interruption_rate: float
    voice_vs_text_ratio: float
    query_complexity_score: float
    follow_up_query_rate: float
    topic_drift_rate: float
    cognitive_load_proxy: float
    
    # Contextual (20)
    calendar_next_event_type: Optional[str] = None
    calendar_next_event_minutes: Optional[int] = None
    calendar_is_in_meeting: bool
    calendar_meeting_count_today: int
    email_unread_count: int
    email_urgent_count: int
    email_time_since_last_check_minutes: Optional[int] = None
    weather_temp_f: Optional[float] = None
    weather_condition: Optional[str] = None
    weather_is_extreme: bool
    traffic_commute_time_minutes: Optional[int] = None
    traffic_is_rush_hour: bool
    homekit_lights_on_count: int
    homekit_is_home_occupied: bool
    homekit_hvac_state: Optional[str] = None
    tesla_is_in_car: bool
    tesla_is_driving: bool
    tesla_battery_level: Optional[int] = None
    tesla_is_charging: bool
    tesla_destination_set: bool
    
    # Historical (15)
    same_hour_yesterday_query_type: Optional[str] = None
    same_hour_last_week_query_type: Optional[str] = None
    same_location_yesterday_query_type: Optional[str] = None
    same_day_of_week_pattern: Optional[str] = None
    morning_routine_pattern: Optional[Dict] = None
    evening_routine_pattern: Optional[Dict] = None
    commute_pattern: Optional[Dict] = None
    weekend_pattern: Optional[Dict] = None
    work_pattern: Optional[Dict] = None
    post_meal_pattern: Optional[Dict] = None
    pre_sleep_pattern: Optional[Dict] = None
    wake_up_pattern: Optional[Dict] = None
    gym_pattern: Optional[Dict] = None
    travel_pattern: Optional[Dict] = None
    stress_pattern: Optional[Dict] = None
    
    # Outcome
    was_useful: Optional[bool] = None
    response_time_ms: Optional[int] = None
    was_interrupted: bool
    cache_hit: bool
    user_satisfaction_score: Optional[int] = None
    
    # Flexible
    features: Optional[Dict] = None
    
    # Metadata
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    
    class Config:
        json_encoders = {
            datetime: lambda v: v.isoformat(),
            UUID: lambda v: str(v)
        }
