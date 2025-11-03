# Free Agency & Contracts System - Implementation Plan

## Overview
This document outlines the complete implementation plan for the free agency and contracts system for the AFFL Discord Bot.

---

## ✅ Phase 1: Database Schema (COMPLETED)

### Completed Items:
1. **Players Table** - Added `contract_expiry` column (references season_number)
2. **Drafts Table** - Added `rookie_contract_years` column (default: 3)
3. **Contract Config Table** - Stores age-based contract lengths
   - Default config: 21-23 (5yr), 24-26 (4yr), 27-30 (3yr), 31+ (2yr)
4. **Compensation Chart Table** - Stores FA compensation bands by age/OVR
5. **Free Agency Periods Table** - Tracks auction periods per season
6. **Free Agency Bids Table** - Stores private bids (unique per team/player)
7. **Free Agency Results Table** - Tracks winning bids, matches, and compensation

### Files Modified:
- `bot.py` (lines 18-305) - All table schemas

---

## 🚧 Phase 2: Core Commands (IN PROGRESS)

### 2.1 `/viewfreeagents` Command
**Purpose:** View all players whose contracts expire this season

**Parameters:**
- `team` (optional) - Filter by team, defaults to all teams

**Display Format:**
```
Free Agents - Season 10
======================

Adelaide (3 players)
• John Smith (MID, 28, 85)
• Bob Jones (FWD, 25, 82)

Brisbane (2 players)
• ...
```

**Implementation Details:**
- Query players where `contract_expiry = current_season`
- Group by team
- Sort by team name, then player OVR descending
- Ephemeral response (user-only)

**Estimated Lines:** ~80 lines

---

### 2.2 `/freeagencyperiod` Command (Admin Only)
**Purpose:** Control free agency auction periods

**Parameters:**
- `action` (required) - Choice of:
  - "Start Bidding Period"
  - "Start Matching Period"
  - "End Matching Period"

**Workflow:**

#### Action: Start Bidding Period
1. Check no active period exists for current season
2. Get all free agents (contract_expiry = current_season)
3. Create period record with status='bidding'
4. Post announcement embed in all team channels
5. Embed shows:
   - Free agents available
   - Auction points (300)
   - How to place bids

**Implementation:** ~100 lines

#### Action: Start Matching Period
1. Verify bidding period exists and status='bidding'
2. **Calculate winning bids:**
   - For each free agent, find highest bid
   - Tiebreaker: lowest ladder position wins
   - Store in `free_agency_results` table
3. **Refund outbid teams:**
   - Teams whose bids were outbid get points back
   - Winning bidders do NOT get points back yet
4. Update period status to 'matching'
5. **Send DMs to teams with winning bids on their players:**
   - Shows each player with winning bid
   - Provides "Match Bids" button to open interactive UI

**Implementation:** ~200 lines

#### Action: End Matching Period
1. Verify matching period exists and status='matching'
2. **Process all unmatched results:**
   - Transfer players to winning teams
   - Assign new contracts based on age
   - Calculate compensation picks
   - Insert compensation picks into draft_picks table
3. **Auto re-sign players with no bids:**
   - Assign new contracts based on age
4. Update period status to 'completed'
5. Post results announcement in all team channels

**Implementation:** ~250 lines

**Total Estimated Lines:** ~550 lines

---

### 2.3 `/placebid` Command
**Purpose:** Place a bid on an opposition free agent

**Parameters:**
- `player` (required) - Autocomplete list of opposition free agents
- `amount` (required) - Bid amount (1-300)

**Validation:**
1. Check bidding period is active
2. Check player is not on user's team
3. Check player is actually a free agent
4. Calculate user's remaining points (300 - sum of active bids)
5. Check user has enough points

**Process:**
1. Insert or replace bid in `free_agency_bids` table
2. If replacing, refund old bid amount first
3. Show confirmation message (ephemeral)

**Display:**
```
✅ Bid Placed!

Player: John Smith (MID, 28, 85) - Adelaide
Bid: 150 points
Remaining: 150 points

View all bids: /auctionsmenu
```

**Estimated Lines:** ~120 lines

---

### 2.4 `/auctionsmenu` Command
**Purpose:** View bids, remaining points, and withdraw bids

**Display Format:**
```
Free Agency Auction - Season 10
================================

Your Team: Adelaide
Remaining Points: 120 / 300

Your Active Bids (3):
• John Smith (MID, 28, 85) - Brisbane - 100 pts [Withdraw]
• Bob Jones (FWD, 25, 82) - Carlton - 50 pts [Withdraw]
• Tim Davis (DEF, 24, 79) - Collingwood - 30 pts [Withdraw]

[Refresh]
```

**Interactive Elements:**
- Withdraw button for each bid (refunds points)
- Refresh button to update display
- Ephemeral response

**Implementation:** ~150 lines with View class

**Total Estimated Lines:** ~270 lines

---

## 🔜 Phase 3: Matching Interface

### 3.1 Matching View UI
**Purpose:** Interactive interface for teams to match winning bids

**Triggered By:** `/freeagencyperiod` "Start Matching Period" sends DM to teams

**Display Format:**
```
Your Free Agents - Matching Phase
==================================

You have 3 players with winning bids:

1. John Smith (MID, 28, 85)
   └─ Winning Bid: 150 pts (Brisbane)
   └─ Match? [Yes] [No]

2. Bob Jones (FWD, 25, 82)
   └─ Winning Bid: 100 pts (Carlton)
   └─ Match? [Yes] [No]

Remaining Points: 200 / 300
Points if matched: 50 / 300

[Confirm Matches]
```

**Features:**
- Toggle Yes/No for each player
- Live updating of remaining points
- Confirm button disabled until selections made
- Ephemeral response

**State Management:**
- Track which players are marked for matching
- Calculate running total of required points
- Validate sufficient points before allowing confirm

**Estimated Lines:** ~200 lines

---

## 🔜 Phase 4: Compensation Pick Logic

### 4.1 Compensation Band Calculation
**Purpose:** Determine compensation band from chart

**Input:** Player age + OVR
**Output:** Band number (1-5) or None

**Query:**
```sql
SELECT compensation_band FROM compensation_chart
WHERE min_age <= ? AND (max_age >= ? OR max_age IS NULL)
AND min_ovr <= ? AND (max_ovr >= ? OR max_ovr IS NULL)
```

**Estimated Lines:** ~30 lines (already implemented)

---

### 4.2 Compensation Pick Insertion
**Purpose:** Insert compensation picks into draft_picks table

**Band Placement Rules:**
1. **Band 1** (1st round) → After team's natural 1st round pick
2. **Band 2** (end of 1st) → At end of round 1, reverse ladder order
3. **Band 3** (2nd round) → After team's natural 2nd round pick
4. **Band 4** (end of 2nd) → At end of round 2, reverse ladder order
5. **Band 5** (3rd round) → After team's natural 3rd round pick

**Algorithm:**

```python
async def insert_compensation_pick(db, team_id, player_name, compensation_band, draft_id, season_number):
    """
    Insert a compensation pick for a team in the appropriate position
    """
    # Get team's ladder position for this season
    ladder_position = await get_team_ladder_position(db, team_id, season_number)

    if compensation_band in [1, 3, 5]:  # After natural pick
        round_num = {1: 1, 3: 2, 5: 3}[compensation_band]

        # Find team's natural pick in this round
        natural_pick = await db.execute(
            """SELECT pick_number FROM draft_picks
               WHERE draft_id = ? AND original_team_id = ? AND round_number = ?""",
            (draft_id, team_id, round_num)
        )
        natural_pick_num = natural_pick[0]

        # Shift all picks after this position up by 1
        await db.execute(
            """UPDATE draft_picks SET pick_number = pick_number + 1
               WHERE draft_id = ? AND pick_number > ?""",
            (draft_id, natural_pick_num)
        )

        # Insert compensation pick
        new_pick_num = natural_pick_num + 1

    elif compensation_band in [2, 4]:  # End of round, reverse ladder
        round_num = {2: 1, 4: 2}[compensation_band]

        # Find last pick in round
        last_pick = await db.execute(
            """SELECT MAX(pick_number) FROM draft_picks
               WHERE draft_id = ? AND round_number = ?""",
            (draft_id, round_num)
        )
        last_pick_num = last_pick[0]

        # Count how many end-of-round compo picks already exist
        existing_compo = await get_end_of_round_compo_count(db, draft_id, round_num)

        # Position based on reverse ladder order
        position_offset = calculate_reverse_ladder_offset(ladder_position, existing_compo)

        # Shift and insert
        new_pick_num = last_pick_num + position_offset + 1
        await db.execute(
            """UPDATE draft_picks SET pick_number = pick_number + 1
               WHERE draft_id = ? AND pick_number >= ?""",
            (draft_id, new_pick_num)
        )

    # Insert the compensation pick
    await db.execute(
        """INSERT INTO draft_picks
           (draft_id, draft_name, season_number, round_number, pick_number,
            pick_origin, original_team_id, current_team_id)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (draft_id, draft_name, season_number, round_num, new_pick_num,
         f"{player_name} Compensation", team_id, team_id)
    )
```

**Estimated Lines:** ~300 lines (including helper functions)

---

## 🔜 Phase 5: Draft & Import Updates

### 5.1 Update `/createdraft` Command
**Location:** `commands/draft_commands.py`

**Changes:**
- Add `rookie_contract_years` parameter (default: 3)
- Store in drafts table on creation

**Estimated Lines:** ~10 lines (modification)

---

### 5.2 Update Draft Selection Logic
**Location:** `commands/draft_commands.py` - Pick selection command

**Changes:**
When a player is drafted:
1. Get `rookie_contract_years` from draft
2. Calculate contract expiry: `draft.season_number + rookie_contract_years`
3. Update player's `contract_expiry` field

**Estimated Lines:** ~15 lines (modification)

---

### 5.3 Update `/importdata` Command
**Location:** `commands/admin_commands.py`

**Add Compensation Chart Tab:**
- Tab name: "Compensation Chart"
- Columns: `Min_Age`, `Max_Age`, `Min_OVR`, `Max_OVR`, `Band`
- Process: Clear existing chart, insert new rows

**Example Chart:**
| Min_Age | Max_Age | Min_OVR | Max_OVR | Band |
|---------|---------|---------|---------|------|
| 18      | 24      | 85      | 99      | 1    |
| 18      | 24      | 80      | 84      | 3    |
| 25      | 27      | 85      | 99      | 2    |
| ...     | ...     | ...     | ...     | ...  |

**Estimated Lines:** ~80 lines

---

## 📊 Implementation Summary

### Total Estimated Lines of Code:
- Phase 2 (Commands): ~820 lines
- Phase 3 (Matching UI): ~200 lines
- Phase 4 (Compensation): ~300 lines
- Phase 5 (Updates): ~105 lines
- **Total: ~1,425 lines**

### Files to be Created/Modified:
1. ✅ `bot.py` - Database schema (DONE)
2. ✅ `commands/free_agency_commands.py` - New file with base structure (DONE)
3. 🚧 `commands/free_agency_commands.py` - Add all commands
4. 🔜 `commands/draft_commands.py` - Update createdraft and selection
5. 🔜 `commands/admin_commands.py` - Update importdata

---

## 🎯 Suggested Implementation Order

1. **Commands First** (Phase 2)
   - `/viewfreeagents` - Simple query, good starting point
   - `/placebid` - Core bidding functionality
   - `/auctionsmenu` - View and withdraw bids
   - `/freeagencyperiod` - Complex orchestration command

2. **Matching Interface** (Phase 3)
   - Build interactive Discord UI
   - Test with sample data

3. **Compensation Logic** (Phase 4)
   - Implement pick insertion algorithm
   - Test with various scenarios

4. **Integration** (Phase 5)
   - Update draft commands
   - Update import command
   - End-to-end testing

---

## 🧪 Testing Checklist

### Unit Tests:
- [ ] Contract years calculation by age
- [ ] Compensation band lookup
- [ ] Remaining points calculation
- [ ] Winning bid determination (including tiebreaker)

### Integration Tests:
- [ ] Complete auction cycle (bid → match → assign)
- [ ] Compensation pick placement for all 5 bands
- [ ] Multiple teams bidding on same player
- [ ] Team matching with insufficient points
- [ ] Players with no bids auto re-signing

### Edge Cases:
- [ ] Starting auction with no free agents
- [ ] Team tries to bid on own player
- [ ] Bid amount equals remaining points exactly
- [ ] All teams bid same amount (ladder tiebreaker)
- [ ] Multiple compensation picks in same round
- [ ] Compensation for player with missing chart entry

---

## 📋 Database Migration Notes

**For Existing Database:**
Since the `contract_expiry` column is being added, you'll need to:

1. Add the column (handled automatically by CREATE IF NOT EXISTS)
2. Set default contract expiry for all existing players:
   ```sql
   UPDATE players
   SET contract_expiry = (SELECT season_number FROM seasons ORDER BY season_number DESC LIMIT 1) + 2
   WHERE contract_expiry IS NULL;
   ```

This will give all existing players a 2-year contract from the current season.

---

## ❓ Questions & Decisions Needed

None at this time - all requirements clarified.

---

## 🚀 Ready to Proceed?

Review this plan and let me know if you'd like me to:
1. Start implementing from the top (recommended order)
2. Implement specific sections first
3. Make any changes to the plan

Total estimated implementation time: 2-3 hours of focused coding.
