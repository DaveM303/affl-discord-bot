# Valid positions for the fantasy league
VALID_POSITIONS = [
    'MID-FWD',
    'MID',
    'DEF-MID',
    'KEY FWD',
    'KEY DEF',
    'GEN FWD',
    'RUCK',
    'UTILITY',
    'GEN DEF',
    'RUCK-DEF',
    'RUCK-FWD',
    'SWINGMAN'
]

# Display order for positions in roster views
POSITION_DISPLAY_ORDER = [
    'KEY DEF',
    'GEN DEF',
    'DEF-MID',
    'MID',
    'MID-FWD',
    'GEN FWD',
    'KEY FWD',
    'SWINGMAN',
    'UTILITY',
    'RUCK',
    'RUCK-DEF',
    'RUCK-FWD'
]

def validate_position(position: str) -> tuple[bool, str]:
    """
    Validate and normalize a position string.
    Returns (is_valid, normalized_position)
    """
    normalized = position.upper().strip()
    
    if normalized in VALID_POSITIONS:
        return True, normalized
    
    return False, normalized

def get_positions_string() -> str:
    """Get a formatted string of all valid positions for error messages."""
    return ", ".join(VALID_POSITIONS)