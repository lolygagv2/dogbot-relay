
ROBOT CLAUDE/ APP CLAUDE / RELAY CLAUDE



More failures, this time not a lot was fixed. I think these changes are a higher level of capability. Please work on this.



Add your own tags to this on which questions you feel comfortable with your code set and assignment (ROBOT, APP or RELAY) and comment in each section on the solution with 500 words or less and little code except references to python files.


Please review the logs/
Local time is 17:25- 17:45


Mission Mode still fails in mode status, AI use and menu selection.
1a. Mission Mode - Failure to stay in mission mode the entire time, it allows "idle" to be shown on the video screen and this disrupts the flow of the mission. Or it's lagging like minutes behind if it IS changing everytime.....

1b. Test flow of mission mode selection to mission mode -- make sure it's a source of truth.

1c. Nope, when i click on the mission menu, and go into sit training and click Play mission - the first time i do this it does NOT even start the mission, it says "mission started" popup status, but the screen shows no active mission (Box should have changed to a stop mission box). The "start mission" box is still there. I repeat this procedure, and this time the mission has started.... 

1d. I return to main menu and it says in the screen text "mission mode 1/2 - idle.  The status button says Mission mode though. But is it monitoring for the dog to start the mission? I'm not sure....
If I I click into drive mode from the state where the mission is supposed to start but not working well I get the same thing it says mission idle it does a SIT training at the top.
1e. Seems AI might not be working in this mission mode, but the idle/mission/not defined/not started issue seems to be a huge problem
1f. WHen i went into mission mode - it pulled up "idle" status on the screen - literally showing "mission - idle". This is broken....mission idle, and they're not getting detected at all like they're literally walking around watching the treats and there's no detection of the dog at all This is a total failure so the mission mode is still completely broken sadly

2. Just Ai dog detection complaint in general?
2a.AI-"dog detected" at 50-61%. Can we test this? See why it's failing so bad?

3a. Coach mode enabled Ai is now not showing ANY boxes around dog with any name?  is this true? how it works? Great no more bezik/elsa but it should say DOG and try to use ARUCO in all AI modes...

4a. MP3 Issue upload failed  - Test upload still fails, it allows the upload - says uploading and then nothing, I don't think it gets saved to the robot, at some point there's a missed connection.At 18:14 i tired to upload a 4MB song. What happened?

5. Coach mode is being tested now, lots of issues, but at least it works, unlike mission mode ---- we are waiting for the dog manual mode enabled 
5a. Weird it just went out of coach mode and went into manual mode when it went off screen or when it lost a connection and now it just speed off treats randomly at 537 or 1737 please verify why did it 
5b. Lack of audio for all commands - So it said "Bezik" but it did not say "SIT" -- another quick error fix, make it say sit. Same logic as coach mode, it should use the same sub-mode for both that has this logic no? that's a bigger challenge but we may do that ok? what do you think?
5c. Maybe easier fix again: This mode should NOT be interrupted by a timeout/disconnect from the app. There should be no timeeouts, please check to see which ones are present and address this. When i clicked "lock screen" on the app. Coach mode exited....
5d. major issue: There seems to be TWo different coach modes.. this is fucked up ie different screen settings for coach mode, you can get into "coach mode" from the main screen and it's just running in the background while showing you the main screen and no further information in the UI, just "coach mode" in the video.
Second coach mode -which seems to actually work (but extremely shitty version compared to the xbox controller version) - runs from the MIssion system.  This system should also be initiated by the "Coach mode" selector on the main screen. Just use this one for all coach mode selection, ie if you change to coach mode, change to this screen and run it from there.
5e. Bad mode logic: So it's in manual mode it's in coaching the screen the AI video screen has the text manual mode The status on the app says Coaching mode this is again the complete mismatch of status that we need to fix it's still here it's still a problem I'm in coach mode but since this failed status - it says idle - manual mode in the video feed.
5f. AI sucks now, models/methods/parameters not good, what the hell happened? is it lagged decisions or why doesn't' it behave and recognize the dogs like it does using coach mode on teh xbox controller style? Did we screw up the logic already?? wtf....
5g. AI logic sucks now to recognize dogs and tricks alike: Compared to traditional coach mode we used on the xbox controller. With dog elsa in view, it did not confirm elsa... it could not find her, but it confirmed Bezik quickly the first time.  Second time, it took over 1 minute!
However, now the dog is in field in the device, clearly visible, i mean clearly obvious dog in the frame and yet it's doing NOTHING not detecting him, not calling his name, this process seems way too long and error prone. NOw it sees the dog (1 minute or so later) as the dog has already "sit" a few times. and it says sit as the command.  So, dog is sitting and yet it says "NO". It does not detect dog sitting.. This is bad bug we have to fix this...It barely detected the dog, and the trick logic was flawed. I have no faith to even test ARUCO yet with dog recognition, fix this shit!



6.Scheduling test failure:- Test create schedule with each type via curl. Verify app can create schedules successfully   
6a. From APP, check the logs/ folder for the messages about this around 18:24-18:27 for today 1-31-2026
6b. The app sends a POST request via Dio (HTTP client) to /schedules. Here's the equivalent curl the app would send:                                                                                                                                                            
                                                                                                                                                                                                                                                                              
  curl -X POST http://<relay-host>/schedules \                                                                                                                                                                                                                                
    -H "Content-Type: application/json" \                                                                                                                                                                                                                                     
    -H "Authorization: Bearer <token>" \                                                                                                                                                                                                                                      
    -d '{                                                                                                                                                                                                                                                                     
      "schedule_id": "uuid-here",                                                                                                                                                                                                                                             
      "mission_name": "Basic Sit",                                                                                                                                                                                                                                            
      "dog_id": "dog-uuid-here",                                                                                                                                                                                                                                              
      "name": "",                                                                                                                                                                                                                                                             
      "type": "daily",                                                                                                                                                                                                                                                        
      "start_time": "09:00",                                                                                                                                                                                                                                                  
      "end_time": "13:00",                                                                                                                                                                                                                                                    
      "days_of_week": [],                                                                                                                                                                                                                                                     
      "enabled": true,                                                                                                                                                                                                                                                        
      "cooldown_hours": 24                                                                                                                                                                                                                                                    
    }'                                                                                                                                                                                                                                                                        
                                                                                                                                                                                                                                                                              
 6c. Key questions to debug:                                                                                                                                                                                                                                                     
                                                                                                                                                                                                                                                                              
  -Is the relay forwarding /schedules to the robot? The app hits the relay, which needs to proxy this to the robot.                                                                                                                                                         
  -What does the Robot API actually expect? Can you share the Robot's schedule endpoint signature?                                                                                                                                                                          
  -Are the field names matching? The app sends:                                                                                                                                                                                                                             
    - schedule_id (not id)                                                                                                                                                                                                                                                    
    - mission_name (not mission_id)                                                                                                                                                                                                                                           
    - start_time / end_time as "HH:MM" strings                                                                                                                                                                                                                                
    - days_of_week as string array ["monday", "tuesday"]                                                                                                                                                                                                                      
                                                                                                                                                                                                                                                                              
  6d. Let me check if there's a mismatch with what Robot Claude implemented. Robot CLAUDE: What error are you seeing exactly - 404, 422, 500?  

7a. Big new issue, there's an extreme lag still, ie the frame rates stream, but i find we're often 10-30 seconds behind real time on the video feed? pleaes identify and fix this cause...we can't have the video frames lag behind real time. DO we skip frames or what is the solution?  Right now i think it's just probably inefficiency in process or AI right? let's address that first. IN a way this is better than the video feed dropping out, but in long term as a use case it gives a false illusion of competency (no drops but inaccurate old video being shown as live for up to 30 seconds or longer lag)

---

## RELAY CLAUDE RESPONSES (Build 35)

### Issue 1a-1f (Mission Mode Status): NOT RELAY

**Analysis:** The relay correctly forwards `mode_changed` events from robot to app. Verified in `websocket.py:449-460`. The relay logs `[EVENT-OK] mode_changed delivered` or `[EVENT-FAIL]` if no app connected.

**Root cause is likely ROBOT side:** Robot may not be sending `mode_changed` events when entering/exiting mission mode, OR the robot is in `idle` sub-state during mission wait periods.

**Relay already does:**
- Forwards all events with `event` field to owner's apps
- Logs delivery confirmation for `mode_changed` events
- Adds timestamps and device_id if missing

**For ROBOT CLAUDE:** Verify robot sends `mode_changed` event when:
1. Mission starts → `mode: "mission"`
2. Mission waiting for dog → should still be `mode: "mission"`, not `idle`
3. Mission completes/stops → `mode: "idle"`

---

### Issue 4a (MP3 Upload Failed): PARTIALLY RELAY

**Fixed in Build 35:**
- Relay now handles `upload_complete`, `upload_error`, `upload_result` events from robot
- Logs `[UPLOAD] Success/Failed from DEVICE: filename`
- Forwards result to app with delivery verification

**What still needs to happen:**
1. **ROBOT** must send `upload_result` after processing upload_song command
2. **APP** must listen for `upload_complete` or `upload_result` event

**Relay config for large files:**
- `ws_max_message_size: 20MB`
- `ws_ping_timeout: 60s` (increased from 20s)

---

### Issue 5c (Coach Mode Exit on Screen Lock): FIXED

**Problem:** App screen lock caused WebSocket disconnect, triggering mode change.

**Fix applied:**
1. Increased `ws_ping_timeout` from 20s → 60s
2. Increased `ws_connection_timeout` from 60s → 120s
3. Increased grace period from 5min → 10min

**How grace period works:**
- App disconnects → grace period starts
- WebRTC sessions preserved for 10 minutes
- `user_disconnected` only sent to robot AFTER 10 minutes
- If app reconnects within 10 min → sessions restored, no robot notification

**For ROBOT CLAUDE:** Ensure robot doesn't exit coach mode on `webrtc_close` alone - only exit on `user_disconnected` event.

---

### Issue 6 (Schedule Endpoint Mismatch): FIXED

**Problem:** App called `/schedules` with different field names than relay expected.

**App sends:**
```json
{
  "schedule_id": "uuid",
  "mission_name": "Basic Sit",
  "start_time": "09:00",
  "end_time": "13:00",
  "days_of_week": ["monday", "tuesday"],
  "cooldown_hours": 24
}
```

**Fix applied:**
1. Added `/schedules` endpoint (in addition to `/missions/schedule`)
2. Models now accept both formats (app and relay)
3. Response includes both formats for compatibility
4. Database schema updated with `end_hour`, `end_minute`, `cooldown_hours`

**Both endpoints now work:**
- `POST /schedules` (what app calls)
- `POST /missions/schedule` (original)

---

### Issue 7a (Video Lag): NOT RELAY

**Analysis:** Video flows directly peer-to-peer via WebRTC between app and robot. The relay only forwards signaling messages (`webrtc_offer`, `webrtc_answer`, `webrtc_ice`). Video frames never pass through relay.

**For ROBOT CLAUDE:** Video lag is caused by:
1. AI processing blocking the encoder
2. Frame buffer growing when processing can't keep up
3. Need to drop frames when behind, not buffer them

**Recommendation:** Add frame skip logic when processing falls behind real-time.


