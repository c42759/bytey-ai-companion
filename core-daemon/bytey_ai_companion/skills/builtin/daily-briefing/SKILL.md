---
name: daily-briefing
description: Organize today's priorities, tasks, and reminders into a crisp morning briefing.
version: 1.0.0
slash_command: briefing
requires_confirmation: false
allowed_tools: []
---

# Daily Briefing

Help the user start their day with a focused, scannable overview:

1. **Gather Context**: If calendar, task, or reminder tools are available, check today's schedule and pending items; otherwise work from what the user shares.
2. **Structure the Briefing**:
   - **Top Priorities** (up to 3 must-do items)
   - **Schedule / Time Blocks** (chronological)
   - **Quick Wins / Follow-ups**
3. **Keep it concise**: Avoid filler so the briefing is easy to read in the companion drawer or hear via TTS.
