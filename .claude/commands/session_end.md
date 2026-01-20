# WIM-Z Relay Server - Session End Command

## Safe Shutdown Protocol

### PHASE 1: Inventory

**Files Modified This Session:**
```bash
git status --short
git diff --stat
```

Show:
- Lines added/removed per file
- New files created
- Files outside proper structure

### PHASE 2: Verification

**Check for issues:**
- Files outside `app/` directory (should be rare)
- Uncommitted secrets in `.env`
- Test files not cleaned up

### PHASE 3: Commit Planning

**If Uncommitted Changes Exist:**

Show proposed commit message:
```
Proposed commit message:

feat: Add WebRTC signaling message routing

Changes:
- Updated app/routers/websocket.py with webrtc_* handlers
- Added session tracking to connection_manager.py

Files changed: 2
Lines added: 87
Lines removed: 5

Approve this commit? (yes/no/edit)
```

**If No Changes:**
```
No uncommitted changes
Last commit: [commit message] ([time ago])
```

### PHASE 4: Session Summary

```
Session Summary
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Goal: [USER GOAL]

Completed:
  - [list of completed items]

Files Modified:
  - [X] new files
  - [Y] existing files modified
  - [Z] lines added/removed

Git Status:
  - Ready to commit: [X] files
  - Branch: [BRANCH NAME]

Next Steps:
  1. [next task]
  2. [next task]

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Everything look good? (yes/no)
```

### PHASE 5: Final Actions

**Commit Changes (If Approved):**
```bash
git add -A
git commit -m "feat: [description]"
git log --oneline -1
```

**Final Checklist:**
```
All changes committed (or intentionally skipped)
No secrets in committed files
Session complete

Safe to close. Goodbye!
```

---

## Usage
Call this command at the end of every Claude Code session:
```bash
/project:session-end
```

## CRITICAL RULES
- NEVER commit without showing user what will be committed
- NEVER delete files without explicit approval
- ALWAYS show session summary before closing
