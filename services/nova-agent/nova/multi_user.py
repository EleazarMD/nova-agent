"""
Multi-User / Shared Context System for Nova Agent.

Provides:
- Household/Family group management
- Shared preferences and context
- Permission system for sensitive tools
- User recognition and context switching
- Shared memory spaces (family memories vs personal memories)
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, Dict, List, Set, Any
from enum import Enum
from loguru import logger


class PermissionLevel(Enum):
    """Permission levels for tools and actions."""
    DENY = "deny"              # Cannot use
    ASK = "ask"                # Must ask for approval each time
    ALLOW = "allow"            # Can use freely
    OWNER_ONLY = "owner_only"    # Only household owner can use


class UserRole(Enum):
    """Roles within a household."""
    OWNER = "owner"            # Full control
    ADMIN = "admin"            # Can manage users, not billing
    MEMBER = "member"          # Standard user
    GUEST = "guest"            # Limited access, temporary


@dataclass
class UserProfile:
    """Profile for a user in the household."""
    user_id: str
    name: str
    role: UserRole
    voice_profile: Optional[str] = None  # Voice recognition ID
    created_at: datetime = field(default_factory=datetime.now)
    last_active: Optional[datetime] = None
    
    # Preferences
    preferences: Dict[str, Any] = field(default_factory=dict)
    
    # Tool permissions (tool_name -> PermissionLevel)
    permissions: Dict[str, PermissionLevel] = field(default_factory=dict)
    
    # Devices associated with this user
    devices: List[str] = field(default_factory=list)


@dataclass
class SharedContext:
    """Context shared across household members."""
    household_id: str
    name: str
    created_at: datetime = field(default_factory=datetime.now)
    
    # Shared preferences (location, timezone, defaults)
    preferences: Dict[str, Any] = field(default_factory=dict)
    
    # Shared memories (family events, preferences, etc.)
    shared_memories: List[str] = field(default_factory=list)
    
    # Shared calendars/emails that all members can access
    shared_resources: Dict[str, str] = field(default_factory=dict)
    
    # Tesla vehicle access (which VINs are household vehicles)
    household_vehicles: List[str] = field(default_factory=list)
    
    # Active members
    members: Dict[str, UserProfile] = field(default_factory=dict)


class HouseholdManager:
    """
    Manages multi-user household context and permissions.
    
    Features:
    - User recognition via voice/device
    - Shared context across members
    - Permission-based tool access
    - Context switching between personal and shared modes
    """
    
    def __init__(self, household_id: str):
        self.household_id = household_id
        self._context: Optional[SharedContext] = None
        self._active_user: Optional[UserProfile] = None
    
    async def initialize_household(self, name: str, owner_id: str, owner_name: str):
        """Initialize a new household with owner."""
        owner = UserProfile(
            user_id=owner_id,
            name=owner_name,
            role=UserRole.OWNER,
            permissions=self._get_owner_permissions(),
        )
        
        self._context = SharedContext(
            household_id=self.household_id,
            name=name,
            members={owner_id: owner},
        )
        
        logger.info(f"Household '{name}' created with owner {owner_name}")
    
    def _get_owner_permissions(self) -> Dict[str, PermissionLevel]:
        """Owner has full permissions."""
        return {
            "tesla_*": PermissionLevel.OWNER_ONLY,
            "service_*": PermissionLevel.OWNER_ONLY,
            "hub_delegate": PermissionLevel.ALLOW,
            "web_search": PermissionLevel.ALLOW,
            "manage_notes": PermissionLevel.ALLOW,
        }
    
    def _get_default_permissions(self, role: UserRole) -> Dict[str, PermissionLevel]:
        """Get default permissions for a role."""
        if role == UserRole.OWNER:
            return self._get_owner_permissions()
        
        elif role == UserRole.ADMIN:
            return {
                "tesla_*": PermissionLevel.ALLOW,
                "service_*": PermissionLevel.ALLOW,
                "hub_delegate": PermissionLevel.ALLOW,
                "web_search": PermissionLevel.ALLOW,
                "manage_notes": PermissionLevel.ALLOW,
            }
        
        elif role == UserRole.MEMBER:
            return {
                "tesla_*": PermissionLevel.ASK,  # Ask for vehicle commands
                "service_restart": PermissionLevel.DENY,
                "service_stop": PermissionLevel.DENY,
                "hub_delegate": PermissionLevel.ALLOW,
                "web_search": PermissionLevel.ALLOW,
                "manage_notes": PermissionLevel.ALLOW,
            }
        
        else:  # GUEST
            return {
                "tesla_*": PermissionLevel.DENY,
                "service_*": PermissionLevel.DENY,
                "hub_delegate": PermissionLevel.ASK,
                "web_search": PermissionLevel.ALLOW,
                "manage_notes": PermissionLevel.ALLOW,
            }
    
    async def add_member(
        self,
        user_id: str,
        name: str,
        role: UserRole = UserRole.MEMBER,
        added_by: Optional[str] = None,
    ) -> UserProfile:
        """Add a member to the household."""
        if not self._context:
            raise ValueError("Household not initialized")
        
        # Check if adder has permission
        if added_by:
            adder = self._context.members.get(added_by)
            if not adder or adder.role not in (UserRole.OWNER, UserRole.ADMIN):
                raise PermissionError("Only owners and admins can add members")
        
        member = UserProfile(
            user_id=user_id,
            name=name,
            role=role,
            permissions=self._get_default_permissions(role),
        )
        
        self._context.members[user_id] = member
        logger.info(f"Added member {name} ({role.value}) to household {self.household_id}")
        
        return member
    
    def get_member(self, user_id: str) -> Optional[UserProfile]:
        """Get a household member by ID."""
        if not self._context:
            return None
        return self._context.members.get(user_id)
    
    def can_use_tool(self, user_id: str, tool_name: str) -> PermissionLevel:
        """
        Check if a user can use a tool.
        
        Returns the permission level for the tool.
        """
        member = self.get_member(user_id)
        if not member:
            return PermissionLevel.DENY
        
        # Check exact match
        if tool_name in member.permissions:
            return member.permissions[tool_name]
        
        # Check wildcard patterns
        for pattern, level in member.permissions.items():
            if pattern.endswith("*"):
                prefix = pattern[:-1]
                if tool_name.startswith(prefix):
                    return level
        
        # Default deny
        return PermissionLevel.DENY
    
    def require_approval(self, user_id: str, tool_name: str) -> bool:
        """Check if this tool usage requires explicit approval."""
        level = self.can_use_tool(user_id, tool_name)
        return level == PermissionLevel.ASK
    
    def recognize_user(
        self,
        voice_profile: Optional[str] = None,
        device_id: Optional[str] = None,
    ) -> Optional[UserProfile]:
        """
        Attempt to recognize user from voice or device.
        
        Returns the recognized user or None if unknown.
        """
        if not self._context:
            return None
        
        # Try voice recognition first
        if voice_profile:
            for member in self._context.members.values():
                if member.voice_profile == voice_profile:
                    self._active_user = member
                    member.last_active = datetime.now()
                    logger.info(f"Recognized user {member.name} by voice")
                    return member
        
        # Try device recognition
        if device_id:
            for member in self._context.members.values():
                if device_id in member.devices:
                    self._active_user = member
                    member.last_active = datetime.now()
                    logger.info(f"Recognized user {member.name} by device {device_id}")
                    return member
        
        return None
    
    def set_active_user(self, user_id: str) -> Optional[UserProfile]:
        """Manually set the active user."""
        member = self.get_member(user_id)
        if member:
            self._active_user = member
            member.last_active = datetime.now()
        return member
    
    def get_active_user(self) -> Optional[UserProfile]:
        """Get currently active user."""
        return self._active_user
    
    # -------------------------------------------------------------------------
    # Shared Context Management
    # -------------------------------------------------------------------------
    
    def get_shared_preference(self, key: str, default: Any = None) -> Any:
        """Get a shared household preference."""
        if not self._context:
            return default
        return self._context.preferences.get(key, default)
    
    def set_shared_preference(self, key: str, value: Any, user_id: str):
        """Set a shared household preference."""
        if not self._context:
            return
        
        # Check permissions
        member = self.get_member(user_id)
        if not member or member.role not in (UserRole.OWNER, UserRole.ADMIN):
            logger.warning(f"User {user_id} not allowed to set shared preferences")
            return
        
        self._context.preferences[key] = value
        logger.info(f"Shared preference '{key}' updated by {member.name}")
    
    def add_shared_memory(self, memory: str, user_id: str):
        """Add a memory to the shared household memory."""
        if not self._context:
            return
        
        member = self.get_member(user_id)
        if not member:
            return
        
        self._context.shared_memories.append({
            "content": memory,
            "added_by": member.name,
            "added_at": datetime.now().isoformat(),
        })
        
        logger.info(f"Shared memory added by {member.name}")
    
    def get_shared_context_for_llm(self) -> str:
        """Get shared context formatted for LLM system prompt."""
        if not self._context:
            return ""
        
        lines = [f"## Household: {self._context.name}"]
        
        # Members
        lines.append("### Household Members:")
        for member in self._context.members.values():
            role_str = f" ({member.role.value})" if member.role != UserRole.MEMBER else ""
            lines.append(f"- {member.name}{role_str}")
        
        # Shared preferences
        if self._context.preferences:
            lines.append("### Shared Preferences:")
            for key, value in self._context.preferences.items():
                lines.append(f"- {key}: {value}")
        
        # Recent shared memories (last 5)
        if self._context.shared_memories:
            lines.append("### Recent Household Memories:")
            for mem in self._context.shared_memories[-5:]:
                content = mem if isinstance(mem, str) else mem.get("content", "")
                lines.append(f"- {content}")
        
        return "\n".join(lines)
    
    def get_personalized_system_prompt(self, user_id: str) -> str:
        """Generate personalized system prompt for a user."""
        member = self.get_member(user_id)
        if not member:
            return ""
        
        lines = [f"## Current User: {member.name}"]
        
        # User preferences
        if member.preferences:
            lines.append("### User Preferences:")
            for key, value in member.preferences.items():
                lines.append(f"- {key}: {value}")
        
        # Add shared context
        shared = self.get_shared_context_for_llm()
        if shared:
            lines.append("")
            lines.append(shared)
        
        return "\n".join(lines)
    
    # -------------------------------------------------------------------------
    # Resource Sharing
    # -------------------------------------------------------------------------
    
    def add_household_vehicle(self, vin: str, user_id: str):
        """Add a Tesla VIN to household vehicles."""
        if not self._context:
            return
        
        member = self.get_member(user_id)
        if not member or member.role not in (UserRole.OWNER, UserRole.ADMIN):
            return
        
        if vin not in self._context.household_vehicles:
            self._context.household_vehicles.append(vin)
            logger.info(f"Vehicle {vin} added to household by {member.name}")
    
    def get_household_vehicles(self) -> List[str]:
        """Get list of household vehicles."""
        if not self._context:
            return []
        return self._context.household_vehicles


# Registry of household managers
_households: Dict[str, HouseholdManager] = {}


def get_household_manager(household_id: str) -> HouseholdManager:
    """Get or create household manager."""
    if household_id not in _households:
        _households[household_id] = HouseholdManager(household_id)
    return _households[household_id]


async def initialize_default_household(
    household_id: str,
    owner_id: str,
    owner_name: str,
) -> HouseholdManager:
    """Initialize default household for single-user setup."""
    manager = get_household_manager(household_id)
    await manager.initialize_household(
        name=f"{owner_name}'s Household",
        owner_id=owner_id,
        owner_name=owner_name,
    )
    return manager
