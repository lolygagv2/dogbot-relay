# Build 32 - App/Relay Team Issues

**Test Date:** 2026-01-30
**Tested By:** Morgan

This document contains issues that need to be addressed on the App or Relay server side.

---

## Issue 0: Login/Logout Flow Broken (HIGH PRIORITY)

### Symptom
1. User logs in successfully
2. After a few minutes, user signs out
3. Gets error message on robot (error audio plays)
4. Unable to re-login to app - app seems stuck
5. Requires app restart to login again
6. After restart and re-login, still gets error message from robot

### Expected Behavior
- User should be able to sign out and sign back in seamlessly
- Robot should gracefully handle user sign-out/sign-in transitions
- No lingering session state should block re-login

### Questions
1. Is the user session being properly cleared on logout?
2. Is there a stale token/session issue on the relay server?
3. What specific error message is displayed?

### Suggested Investigation
1. Check if relay is sending a "user_disconnected" event to robot on logout
2. Verify session cleanup on relay when user logs out
3. Check if robot needs to clear any state when user disconnects

---

## Issue 1: Music Upload Fails (HIGH PRIORITY)

### Symptom
- User initiates music file upload
- App shows "uploading..." indicator
- Almost immediately shows "disconnected/reconnecting"
- File not found after upload attempt

### Current Music Folder Structure (Robot)
```
VOICEMP3/songs/default/  - Default songs
VOICEMP3/songs/dog1/     - (legacy folder)
VOICEMP3/songs/user/     - User uploads folder
```

### Questions
1. What endpoint is being called for music upload?
2. Is there a file size limit that's being exceeded?
3. Is the WebSocket disconnecting during large file transfers?
4. Where is the file supposed to be saved?

### Suggested Fixes
1. **Simple approach:** Add music files directly to `VOICEMP3/songs/` base folder
2. Use chunked upload for large files to prevent timeout
3. Add delete capability:
   - Long-press music icon → show delete confirmation
   - Call `DELETE /songs/{filename}` endpoint
4. Consider progressive loading for song list

### Robot-Side Endpoint (If Needed)
```python
# Already exists: POST /audio/play/file
# May need: POST /audio/upload/song
# May need: DELETE /songs/{filename}
```

---

## Issue 3: Dog Profile Photo Refresh Issue (MEDIUM)

### Symptom
1. First "change photo" works perfectly - preview shows new photo
2. Second "change photo" in same session - uploads successfully but preview doesn't update
3. Requires app restart to see the new photo

### Expected Behavior
- Every photo change should immediately show the new photo in preview
- No app restart should be required

### Root Cause (Likely)
- iOS is caching the image URL
- Same URL returns cached image instead of new image

### Suggested Fixes
1. **Cache-busting:** Add timestamp query param to photo URL
   ```swift
   // Instead of:
   imageURL = "https://example.com/dogs/123/photo.jpg"
   // Use:
   imageURL = "https://example.com/dogs/123/photo.jpg?t=\(Date().timeIntervalSince1970)"
   ```

2. **Force reload:** After upload completes, force ImageView to reload
   ```swift
   imageView.image = nil  // Clear cache
   imageView.load(from: newURL)
   ```

3. **Alternative:** Use unique filenames for each upload
   ```
   photo_1706647800.jpg -> photo_1706647805.jpg
   ```

---

## Issue 6: Dogs Shared Across All Users (HIGH PRIORITY - Security)

### Symptom
All users can see all dogs in the system, not just their own dogs.

### Current Behavior
- User A creates dog "Fluffy"
- User B logs in and can see "Fluffy"
- User B can play User A's custom voice commands on the robot

### Expected Behavior
- Each user should only see their own dogs
- Each user should only access their dogs' custom audio
- Dogs should be scoped to user_id
- When robot connects, it should only receive dogs for the connected user

### Security Implications
- Privacy violation - users can see other users' dogs and photos
- Audio access - users can play other users' custom voice recordings
- Data leakage between households

### Suggested Database Changes
```sql
-- Dogs table should have user_id foreign key
ALTER TABLE dogs ADD COLUMN user_id UUID REFERENCES users(id);

-- All queries should filter by user
SELECT * FROM dogs WHERE user_id = $authenticated_user_id;
```

### API Changes Needed
1. **Get Dogs:** Only return dogs where `user_id = authenticated_user`
2. **Create Dog:** Set `user_id` to authenticated user
3. **Update/Delete Dog:** Verify ownership before allowing
4. **Get Profiles (for robot):** Only send dogs for the user who has the robot paired

---

## Issue 6b: Robot Pairing Error Message (LOW)

### Symptom
When User B tries to pair with a robot already paired to User A, they get a generic error.

### Expected Behavior
Error message should say "Robot unavailable" or "Robot paired to another user"

### Suggested Fix
```swift
// In pairing error handler:
if error.code == "robot_already_paired" {
    showAlert("Robot Unavailable",
              message: "This robot is paired to another user. Please contact the owner to unpair first.")
}
```

---

## Issue 7: User Persistence & Email Validation (MEDIUM)

### Questions
1. Are users stored server-side? (Assume yes, in Supabase)
2. Do users persist after app restart? (Need to verify)
3. Can we add email validation on signup?
4. Can we implement password reset flow?

### Desired Features

#### Email Verification on Signup
```
1. User enters email/password
2. Account created in "unverified" state
3. Verification email sent with link
4. User clicks link -> account verified
5. Only verified accounts can pair robots
```

#### Password Reset Flow
```
1. User taps "Forgot Password"
2. Enters email address
3. Reset email sent with secure link
4. User clicks link -> enters new password
5. Password updated, user can login
```

### Implementation Notes
- Supabase has built-in auth flows for this
- May need to configure email templates
- Consider rate limiting on password reset requests

---

## Robot-Side Changes Made (For Reference)

The following changes were made on the robot to support these features:

### 1. Dog Deletion Cleanup
When a dog is deleted from the app, the robot now has an endpoint to clean up associated resources:

**WebSocket Command:**
```json
{"command": "delete_dog", "dog_id": "1769381269569"}
```

**REST Endpoint:**
```
DELETE /dogs/{dog_id}
```

**What it cleans up:**
- `VOICEMP3/talks/dog_{id}/` directory (all custom voice files)

### 2. Mission Events Now Include Mission Name
All `mission_progress` and `mission_complete` events now include:
```json
{
    "event": "mission_progress",
    "mission_name": "sit",  // <-- NEW FIELD
    "status": "watching",
    "trick": "sit",
    "stage": 1,
    "total_stages": 2,
    ...
}
```

**App should display:** `mission_name` in the UI instead of "????"

---

## Testing Checklist for App Team

After implementing fixes, please verify:

- [ ] User can logout and login without restarting app
- [ ] Music uploads successfully and file is playable
- [ ] Dog photo updates show immediately without app restart
- [ ] Each user only sees their own dogs
- [ ] Robot pairing shows "Robot unavailable" for already-paired robots
- [ ] New users can sign up with email verification
- [ ] Password reset flow works
- [ ] When dog is deleted, robot cleans up voice files (`delete_dog` command sent)
- [ ] Mission progress shows correct mission name (not "????")

---

## Contact

For questions about robot-side implementation, contact the WIM-Z dev team.
