# WIM-Z Relay Server - Development Protocol

## RULES FOR AI ASSISTANTS

### 1. Task Execution Mode
- Complete the CURRENT task fully before asking "what's next"
- Do not propose optional enhancements mid-task
- Do not ask "would you like me to test X?" unless testing is explicitly in the task

### 2. Task Definition Format
Every task must specify:
- [ ] Deliverable (file/function/feature)
- [ ] Acceptance criteria (how to verify it works)
- [ ] Dependencies (what must exist first)
- [ ] Estimated scope (S/M/L = small change, medium feature, large system)

### 3. Task Completion Protocol
When a task is done, respond with:
- Task complete: [brief description]
- Output: [file paths or test results]
- Next task from list: [task ID]

Do NOT:
- Suggest improvements to completed work
- Propose testing beyond what's specified
- Ask if user wants documentation/comments
- Request approval to move to next task (just move)

### 4. Rabbit Hole Prevention
If implementation requires >3 decisions not specified in task:
- STOP
- Document the decision points
- Ask user for direction
- Wait for response before continuing

### 5. Context Switching Rules
- Finish current file before starting another
- Finish current module before jumping to different layer
- Finish current phase before suggesting features from later phases

## FastAPI-Specific Guidelines

### API Development
- Always add proper Pydantic models for request/response
- Include OpenAPI documentation (FastAPI does this automatically)
- Use dependency injection for shared resources (auth, database, etc.)

### WebSocket Development
- Handle disconnection gracefully
- Log connection events for debugging
- Clean up resources on disconnect

### Testing
- Test endpoints with curl or httpie before committing
- Verify WebSocket connections work with a test client
- Check error handling paths

### Security
- Never commit secrets (use environment variables)
- Validate all input with Pydantic
- Use JWT validation on protected endpoints
