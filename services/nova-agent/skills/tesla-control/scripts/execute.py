#!/usr/bin/env python3
"""
Tesla Control Skill - Execution Script

Provides Tesla vehicle control through Tesla Relay Service with approval-gated commands.
"""

import asyncio
import json
import sys
from typing import Optional, Dict, Any

# Import the fixed Tesla tools module
sys.path.insert(0, '/home/eleazar/Projects/AIHomelab/services/nova-agent')
from nova.tesla_tools import (
    handle_tesla_status,
    handle_tesla_vehicles,
    handle_tesla_vehicle_status,
    handle_tesla_charging_control,
    handle_tesla_climate_control,
    handle_tesla_lock_control,
    handle_tesla_trunk_control,
    handle_tesla_wake,
    handle_tesla_honk_flash,
    handle_tesla_navigation,
)


async def execute_tesla_control(
    action: str,
    user_id: str = "default",
    vehicle_identifier: Optional[str] = None,
    command: Optional[str] = None,
    value: Optional[Any] = None,
    **kwargs
) -> Dict[str, Any]:
    """
    Execute Tesla control command.
    
    Args:
        action: The Tesla operation (vehicles, status, climate, charge, lock, trunk, wake, honk_flash, navigation)
        user_id: User ID for Tesla account
        vehicle_identifier: VIN, model name, or display name (optional, uses first vehicle if not provided)
        command: Specific command to execute (e.g., "start", "stop", "lock", "unlock")
        value: Command parameter (e.g., temperature, charge limit, destination)
        **kwargs: Additional parameters
    
    Returns:
        Dict with success status and result
    """
    try:
        result = None
        
        # Route to appropriate handler based on action
        if action == "status":
            result = await handle_tesla_status(user_id)
            
        elif action == "vehicles":
            result = await handle_tesla_vehicles(user_id)
            
        elif action == "vehicle_status":
            result = await handle_tesla_vehicle_status(user_id, vehicle_identifier)
            
        elif action == "climate":
            # Parse climate command
            if command == "set_temp" and value:
                temp = float(value)
                result = await handle_tesla_climate_control(user_id, "set_temp", vehicle_identifier, temp)
            elif command in ["start", "on"]:
                result = await handle_tesla_climate_control(user_id, "start", vehicle_identifier)
            elif command in ["stop", "off"]:
                result = await handle_tesla_climate_control(user_id, "stop", vehicle_identifier)
            else:
                return {"success": False, "error": f"Invalid climate command: {command}"}
                
        elif action == "charge":
            # Parse charge command
            if command == "start":
                result = await handle_tesla_charging_control(user_id, "start", vehicle_identifier)
            elif command == "stop":
                result = await handle_tesla_charging_control(user_id, "stop", vehicle_identifier)
            elif command == "set_limit" and value:
                limit = int(value)
                result = await handle_tesla_charging_control(user_id, "set_limit", vehicle_identifier, limit)
            elif command == "set_amps" and value:
                amps = int(value)
                result = await handle_tesla_charging_control(user_id, "set_amps", vehicle_identifier, None, amps)
            else:
                return {"success": False, "error": f"Invalid charge command: {command}"}
                
        elif action == "lock":
            # Parse lock command
            if command in ["lock", "unlock"]:
                result = await handle_tesla_lock_control(user_id, command, vehicle_identifier)
            else:
                return {"success": False, "error": f"Invalid lock command: {command}"}
                
        elif action == "trunk":
            # Parse trunk command
            if command in ["front", "rear"]:
                result = await handle_tesla_trunk_control(user_id, command, vehicle_identifier)
            else:
                return {"success": False, "error": f"Invalid trunk command: {command}. Use 'front' or 'rear'"}
                
        elif action == "wake":
            result = await handle_tesla_wake(user_id, vehicle_identifier)
            
        elif action == "honk_flash":
            # Parse honk/flash command
            if command in ["honk", "flash"]:
                result = await handle_tesla_honk_flash(user_id, command, vehicle_identifier)
            else:
                return {"success": False, "error": f"Invalid honk_flash command: {command}. Use 'honk' or 'flash'"}
                
        elif action == "navigation":
            # Parse navigation command
            if not value:
                return {"success": False, "error": "Navigation requires a destination (value parameter)"}
            
            # Extract lat/lon if provided in kwargs
            latitude = kwargs.get("latitude")
            longitude = kwargs.get("longitude")
            
            result = await handle_tesla_navigation(
                user_id,
                destination=str(value),
                latitude=latitude,
                longitude=longitude,
                vin=vehicle_identifier
            )
            
        else:
            return {
                "success": False,
                "error": f"Unknown action: {action}",
                "valid_actions": ["status", "vehicles", "vehicle_status", "climate", "charge", "lock", "trunk", "wake", "honk_flash", "navigation"]
            }
        
        # Return result
        return {
            "success": True,
            "action": action,
            "result": result
        }
        
    except Exception as e:
        return {
            "success": False,
            "action": action,
            "error": str(e)
        }


async def main():
    """CLI entry point for testing."""
    if len(sys.argv) < 2:
        print("Usage: execute.py <action> [vehicle_identifier] [command] [value]")
        print("\nExamples:")
        print("  execute.py vehicles")
        print("  execute.py vehicle_status 'Model 3'")
        print("  execute.py climate 'Model 3' set_temp 72")
        print("  execute.py lock 'Model 3' lock")
        print("  execute.py charge 'Model 3' start")
        sys.exit(1)
    
    action = sys.argv[1]
    vehicle_identifier = sys.argv[2] if len(sys.argv) > 2 else None
    command = sys.argv[3] if len(sys.argv) > 3 else None
    value = sys.argv[4] if len(sys.argv) > 4 else None
    
    result = await execute_tesla_control(
        action=action,
        vehicle_identifier=vehicle_identifier,
        command=command,
        value=value
    )
    
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
