"""
support_tools.py — Telecom Customer Support Tool Dispatcher for Wavelink Mobile.
Provides in-memory network/account state, JSON schema tool definitions, and a
schema-validated execution dispatcher for the voice agent.
"""

import json
from typing import Any, Dict, Optional


# ============================================================================
# 1. In-Memory State (Mock Telecom Backend)
# ============================================================================

MOCK_NETWORK_STATUS: Dict[str, Dict[str, Any]] = {
    "downtown": {
        "status": "outage",
        "affected_services": ["voice", "data"],
        "eta": "45 minutes",
        "incident_id": "NET-4471",
    },
    "riverside": {
        "status": "degraded",
        "affected_services": ["data"],
        "eta": "20 minutes",
        "incident_id": "NET-4468",
    },
}

DEFAULT_NETWORK_STATUS = {
    "status": "operational",
    "affected_services": [],
    "eta": None,
    "incident_id": None,
}

MOCK_ACCOUNTS: Dict[str, Dict[str, Any]] = {
    "5551234567": {"customer_name": "Alex Mercer", "plan": "Unlimited Plus", "status": "ACTIVE", "line_resets": 0},
    "5559876543": {"customer_name": "Jamie Lee", "plan": "Basic 5GB", "status": "ACTIVE", "line_resets": 0},
}


# ============================================================================
# 2. Tool Implementations (Core Business Logic)
# ============================================================================

def check_network_status(location: str) -> Dict[str, Any]:
    """
    Checks for known network outages or degraded service in a specific area.
    """
    key = location.strip().lower()
    info = MOCK_NETWORK_STATUS.get(key, DEFAULT_NETWORK_STATUS)

    if info["status"] == "operational":
        return {
            "status": "success",
            "location": location,
            "network_status": "operational",
            "message": f"No known issues in {location}. Service is operating normally.",
        }

    return {
        "status": "success",
        "location": location,
        "network_status": info["status"],
        "affected_services": info["affected_services"],
        "eta": info["eta"],
        "incident_id": info["incident_id"],
        "message": (
            f"{info['status'].capitalize()} affecting {', '.join(info['affected_services'])} "
            f"in {location}. Estimated resolution: {info['eta']}."
        ),
    }


def end_call(reason: str) -> Dict[str, Any]:
    """
    Ends the current support call. The agent must have already told the
    caller it's about to hang up (e.g. summarized the resolution and said
    goodbye) in the same turn before calling this — the relay server waits
    for that turn's speech to finish playing before actually closing the line.
    """
    return {
        "status": "success",
        "action": "CALL_ENDED",
        "reason": reason,
        "message": "Call will end after this turn finishes playing.",
    }


def restart_connection(phone_number: str) -> Dict[str, Any]:
    """
    Remotely resets a customer's network connection/line to resolve connectivity issues.
    """
    key = phone_number.strip().replace("-", "").replace(" ", "")
    account = MOCK_ACCOUNTS.get(key)

    if not account:
        return {
            "status": "error",
            "message": f"No account found for phone number '{phone_number}'.",
        }

    account["line_resets"] += 1

    return {
        "status": "success",
        "phone_number": phone_number,
        "action": "CONNECTION_RESET",
        "reset_count_today": account["line_resets"],
        "message": f"The connection for {phone_number} has been reset. Please power-cycle the device and try again in about a minute.",
    }


# ============================================================================
# 3. AssemblyAI Tool Definitions (JSON Schema)
# ============================================================================

TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "check_network_status",
            "description": "Checks for known network outages or degraded service in a specific area.",
            "parameters": {
                "type": "object",
                "properties": {
                    "location": {
                        "type": "string",
                        "description": "City, neighborhood, or area to check (e.g., 'Downtown', 'Riverside').",
                    },
                },
                "required": ["location"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "end_call",
            "description": (
                "Ends the current support call. Only call this AFTER you have already "
                "told the caller, in the same reply, that the issue is resolved (or that "
                "you're unable to help further) and said goodbye. Never call this silently "
                "or before notifying the caller."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "reason": {
                        "type": "string",
                        "description": "Short reason the call is ending, e.g. 'issue resolved' or 'caller requested to end the call'.",
                    },
                },
                "required": ["reason"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "restart_connection",
            "description": "Remotely resets a customer's network connection/line to resolve connectivity issues.",
            "parameters": {
                "type": "object",
                "properties": {
                    "phone_number": {
                        "type": "string",
                        "description": "The customer's phone number on the account.",
                    },
                },
                "required": ["phone_number"],
            },
        },
    },
]


# ============================================================================
# 4. Tool Execution Dispatcher with Schema Validation
# ============================================================================

def execute_tool(tool_name: str, arguments: Any) -> Dict[str, Any]:
    """
    Parses arguments, validates required inputs against schemas,
    dispatches execution to the corresponding tool, and returns a structured output.
    """
    if isinstance(arguments, str):
        try:
            args = json.loads(arguments) if arguments.strip() else {}
        except json.JSONDecodeError as err:
            return {
                "status": "error",
                "message": f"Malformed JSON arguments: {str(err)}",
            }
    elif isinstance(arguments, dict):
        args = arguments
    else:
        args = {}

    try:
        if tool_name == "check_network_status":
            if "location" not in args or not str(args["location"]).strip():
                return {"status": "error", "message": "Missing required parameter 'location'."}

            return check_network_status(location=str(args["location"]))

        elif tool_name == "restart_connection":
            if "phone_number" not in args or not str(args["phone_number"]).strip():
                return {"status": "error", "message": "Missing required parameter 'phone_number'."}

            return restart_connection(phone_number=str(args["phone_number"]))

        elif tool_name == "end_call":
            reason = str(args.get("reason") or "Call completed.")
            return end_call(reason=reason)

        else:
            return {
                "status": "error",
                "message": f"Tool '{tool_name}' is not recognized.",
            }

    except Exception as exc:
        return {
            "status": "error",
            "message": f"Unexpected execution failure in '{tool_name}': {str(exc)}",
        }


# ============================================================================
# 5. Direct Execution Self-Test
# ============================================================================

if __name__ == "__main__":
    print("=== Testing support_tools.py tool dispatch ===")

    res1 = execute_tool("check_network_status", {"location": "Downtown"})
    print("\n[1] check_network_status (Downtown):")
    print(json.dumps(res1, indent=2))
    assert res1["status"] == "success"
    assert res1["network_status"] == "outage"

    res2 = execute_tool("check_network_status", {"location": "Suburbia"})
    print("\n[2] check_network_status (Suburbia, no known issues):")
    print(json.dumps(res2, indent=2))
    assert res2["network_status"] == "operational"

    res3 = execute_tool("restart_connection", json.dumps({"phone_number": "555-123-4567"}))
    print("\n[3] restart_connection (555-123-4567):")
    print(json.dumps(res3, indent=2))
    assert res3["status"] == "success"
    assert res3["action"] == "CONNECTION_RESET"

    res4 = execute_tool("check_network_status", {})
    print("\n[4] Validation error check:")
    print(json.dumps(res4, indent=2))
    assert res4["status"] == "error"

    print("\n[ALL LOCAL TESTS PASSED] support_tools.py is ready for server integration.")
