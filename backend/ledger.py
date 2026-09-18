"""
ledger.py — Banking Ledger and Tool Dispatcher for DisputeFlow.
Provides in-memory transaction states, card actions, dispute filing,
JSON schema definitions, and a schema-validated execution dispatcher.
"""

from datetime import datetime, timezone
import json
from typing import Any, Dict, List, Optional


# ============================================================================
# 1. In-Memory State (Mock Ledger Database)
# ============================================================================

MOCK_CARDS = {
    "4242": {
        "cardholder": "Alex Mercer",
        "card_type": "Visa Infinite",
        "status": "ACTIVE",  # ACTIVE, FROZEN, CANCELLED
        "frozen_at": None,
        "freeze_reason": None,
    },
    "8819": {
        "cardholder": "Alex Mercer",
        "card_type": "Mastercard World Elite",
        "status": "ACTIVE",
        "frozen_at": None,
        "freeze_reason": None,
    },
}

MOCK_TRANSACTIONS: List[Dict[str, Any]] = [
    {
        "transaction_id": "TX-8921-AF",
        "merchant": "Uber",
        "amount": 142.50,
        "currency": "USD",
        "card_last_four": "4242",
        "date_range_category": "today",
        "timestamp": "2026-09-17T08:14:22Z",
        "status": "COMPLETED",
        "category": "Transportation",
        "flagged_fraud": True,
    },
    {
        "transaction_id": "TX-4410-BC",
        "merchant": "Target",
        "amount": 84.19,
        "currency": "USD",
        "card_last_four": "4242",
        "date_range_category": "today",
        "timestamp": "2026-09-17T06:30:10Z",
        "status": "COMPLETED",
        "category": "Retail",
        "flagged_fraud": False,
    },
    {
        "transaction_id": "TX-3309-DL",
        "merchant": "Delta Air Lines",
        "amount": 640.00,
        "currency": "USD",
        "card_last_four": "8819",
        "date_range_category": "this_week",
        "timestamp": "2026-09-14T14:22:00Z",
        "status": "COMPLETED",
        "category": "Travel",
        "flagged_fraud": False,
    },
    {
        "transaction_id": "TX-1092-AM",
        "merchant": "Amazon",
        "amount": 29.99,
        "currency": "USD",
        "card_last_four": "4242",
        "date_range_category": "this_month",
        "timestamp": "2026-09-02T19:05:41Z",
        "status": "COMPLETED",
        "category": "E-Commerce",
        "flagged_fraud": False,
    },
]

MOCK_DISPUTES: Dict[str, Dict[str, Any]] = {}


# ============================================================================
# 2. Tool Implementations (Core Business Logic)
# ============================================================================

def lookup_transaction(merchant: str, date_range: str = "this_week") -> Dict[str, Any]:
    """
    Finds transactions by approximate merchant name and timeframe.
    """
    clean_merchant = merchant.strip().lower()
    matches = []

    for tx in MOCK_TRANSACTIONS:
        # Match if merchant keyword is contained in the transaction merchant field
        if clean_merchant in tx["merchant"].lower():
            # Filter by timeframe if applicable
            if date_range == "today" and tx["date_range_category"] != "today":
                continue
            if date_range == "this_week" and tx["date_range_category"] not in ["today", "this_week"]:
                continue
            matches.append(tx)

    if not matches:
        return {
            "status": "not_found",
            "message": f"No transactions found matching merchant '{merchant}' within timeframe '{date_range}'.",
            "count": 0,
            "results": [],
        }

    return {
        "status": "success",
        "count": len(matches),
        "results": matches,
    }


def freeze_card(card_last_four: str, reason: str = "suspected_fraud") -> Dict[str, Any]:
    """
    Suspends a payment card to prevent further unauthorized charges.
    """
    card_key = card_last_four.strip()
    if card_key not in MOCK_CARDS:
        return {
            "status": "error",
            "message": f"Card ending in '{card_key}' not found on file.",
        }

    card = MOCK_CARDS[card_key]
    if card["status"] == "FROZEN":
        return {
            "status": "already_frozen",
            "card_last_four": card_key,
            "cardholder": card["cardholder"],
            "frozen_at": card["frozen_at"],
            "message": f"Card ending in {card_key} is already suspended.",
        }

    freeze_time = datetime.now(timezone.utc).isoformat()
    card["status"] = "FROZEN"
    card["frozen_at"] = freeze_time
    card["freeze_reason"] = reason

    return {
        "status": "success",
        "card_last_four": card_key,
        "cardholder": card["cardholder"],
        "card_type": card["card_type"],
        "action": "CARD_FROZEN",
        "timestamp": freeze_time,
        "reason": reason,
        "message": f"Card ending in {card_key} has been successfully frozen.",
    }


def submit_dispute(transaction_id: str, dispute_reason: str) -> Dict[str, Any]:
    """
    Opens an official chargeback dispute for a specified transaction ID.
    """
    tx_key = transaction_id.strip().upper()
    target_tx = next((tx for tx in MOCK_TRANSACTIONS if tx["transaction_id"] == tx_key), None)

    if not target_tx:
        return {
            "status": "error",
            "message": f"Transaction ID '{tx_key}' was not found. Please confirm the ID.",
        }

    # Generate deterministic dispute reference ID
    dispute_id = f"DISP-{len(MOCK_DISPUTES) + 80101}"
    record = {
        "dispute_id": dispute_id,
        "transaction_id": tx_key,
        "merchant": target_tx["merchant"],
        "amount": target_tx["amount"],
        "currency": target_tx["currency"],
        "dispute_reason": dispute_reason,
        "status": "OPEN",
        "provisional_credit_issued": True,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }

    MOCK_DISPUTES[dispute_id] = record

    return {
        "status": "success",
        "dispute_id": dispute_id,
        "transaction_id": tx_key,
        "merchant": target_tx["merchant"],
        "disputed_amount": f"${target_tx['amount']:.2f} {target_tx['currency']}",
        "provisional_credit": f"${target_tx['amount']:.2f}",
        "action": "DISPUTE_FILED",
        "message": f"Dispute {dispute_id} filed. A provisional credit of ${target_tx['amount']:.2f} has been credited.",
    }


# ============================================================================
# 3. AssemblyAI Tool Definitions (JSON Schema)
# ============================================================================

TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "lookup_transaction",
            "description": "Searches recent card transactions by merchant name and timeframe to investigate charges.",
            "parameters": {
                "type": "object",
                "properties": {
                    "merchant": {
                        "type": "string",
                        "description": "Name of the merchant (e.g., 'Uber', 'Target', 'Delta Air Lines').",
                    },
                    "date_range": {
                        "type": "string",
                        "enum": ["today", "this_week", "this_month"],
                        "default": "this_week",
                        "description": "Time period to search for transactions.",
                    },
                },
                "required": ["merchant"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "freeze_card",
            "description": "Immediately suspends a debit or credit card to prevent further unauthorized transactions.",
            "parameters": {
                "type": "object",
                "properties": {
                    "card_last_four": {
                        "type": "string",
                        "description": "The last 4 digits of the card to freeze (e.g., '4242').",
                    },
                    "reason": {
                        "type": "string",
                        "description": "Reason for freezing the card (e.g., 'unrecognized charge from Uber').",
                        "default": "suspected_fraud",
                    },
                },
                "required": ["card_last_four"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "submit_dispute",
            "description": "Files a formal chargeback dispute for a verified fraudulent or incorrect transaction ID.",
            "parameters": {
                "type": "object",
                "properties": {
                    "transaction_id": {
                        "type": "string",
                        "description": "The exact transaction ID to dispute (e.g., 'TX-8921-AF').",
                    },
                    "dispute_reason": {
                        "type": "string",
                        "enum": [
                            "fraudulent_charge",
                            "incorrect_amount",
                            "item_not_received",
                            "duplicate_charge",
                        ],
                        "description": "The primary categorization for the dispute claim.",
                    },
                },
                "required": ["transaction_id", "dispute_reason"],
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
    # 1. Parse arguments if passed as JSON string
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

    # 2. Dispatch with validation
    try:
        if tool_name == "lookup_transaction":
            if "merchant" not in args or not str(args["merchant"]).strip():
                return {"status": "error", "message": "Missing required parameter 'merchant'."}
            
            return lookup_transaction(
                merchant=str(args["merchant"]),
                date_range=str(args.get("date_range", "this_week")),
            )

        elif tool_name == "freeze_card":
            if "card_last_four" not in args or not str(args["card_last_four"]).strip():
                return {"status": "error", "message": "Missing required parameter 'card_last_four'."}
            
            return freeze_card(
                card_last_four=str(args["card_last_four"]),
                reason=str(args.get("reason", "suspected_fraud")),
            )

        elif tool_name == "submit_dispute":
            missing = [k for k in ["transaction_id", "dispute_reason"] if k not in args or not str(args[k]).strip()]
            if missing:
                return {
                    "status": "error",
                    "message": f"Missing required parameter(s): {', '.join(missing)}.",
                }
            
            return submit_dispute(
                transaction_id=str(args["transaction_id"]),
                dispute_reason=str(args["dispute_reason"]),
            )

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
    print("=== Testing ledger.py tool dispatch ===")

    # Test 1: Lookup
    res1 = execute_tool("lookup_transaction", {"merchant": "uber", "date_range": "today"})
    print("\n[1] lookup_transaction (Uber):")
    print(json.dumps(res1, indent=2))
    assert res1["status"] == "success"
    assert res1["count"] == 1
    assert res1["results"][0]["transaction_id"] == "TX-8921-AF"

    # Test 2: Freeze Card
    res2 = execute_tool("freeze_card", json.dumps({"card_last_four": "4242", "reason": "fraud"}))
    print("\n[2] freeze_card (4242):")
    print(json.dumps(res2, indent=2))
    assert res2["status"] == "success"
    assert res2["action"] == "CARD_FROZEN"

    # Test 3: Submit Dispute
    res3 = execute_tool(
        "submit_dispute",
        {"transaction_id": "TX-8921-AF", "dispute_reason": "fraudulent_charge"},
    )
    print("\n[3] submit_dispute (TX-8921-AF):")
    print(json.dumps(res3, indent=2))
    assert res3["status"] == "success"
    assert res3["action"] == "DISPUTE_FILED"
    assert "DISP-" in res3["dispute_id"]

    # Test 4: Missing Parameter Validation
    res4 = execute_tool("lookup_transaction", {})
    print("\n[4] Validation error check:")
    print(json.dumps(res4, indent=2))
    assert res4["status"] == "error"

    print("\n[ALL LOCAL TESTS PASSED] ledger.py is ready for server integration.")